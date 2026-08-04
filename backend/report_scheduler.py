import os
import asyncio
import datetime
import httpx
import redis
from datetime import UTC, timedelta
from sqlmodel import Session, select, func
from database import engine
from models import clicklog, urldata, Subscription, User
from short_url_gen import redis_client
from logger import logger, log_file

# Fetch configuration parameters
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
TARGET_EMAIL = os.getenv("ADMIN_EMAIL")

# Per-day claim key lives long enough to cover the full UTC day + restarts.
# Using a dedicated Redis client with sane timeouts so the once-daily job is
# not flaky under the 100ms timeouts used by the hot-path redis_client.
DAILY_JOB_KEY_PREFIX = "daily_report_sent:"
DAILY_JOB_TTL_SECONDS = 60 * 60 * 48  # 48 hours

_scheduler_redis = redis.Redis(
    host=os.getenv("REDIS_HOST", "127.0.0.1"),
    port=int(os.getenv("REDIS_PORT", 6379)),
    decode_responses=True,
    socket_connect_timeout=2.0,
    socket_timeout=2.0,
)


def _daily_job_redis_key(current_date: str) -> str:
    return f"{DAILY_JOB_KEY_PREFIX}{current_date}"


def _daily_job_claim_file(current_date: str) -> str:
    """Per-date claim file used when Redis is unavailable (same host only)."""
    return os.path.join(os.path.dirname(log_file), f"daily_report_claimed_{current_date}")


def try_claim_daily_job(current_date: str) -> bool:
    """
    Atomically claim the right to run the daily report + dunning job for this UTC date.

    Primary: Redis SET NX (safe across Gunicorn/Uvicorn workers and hosts sharing Redis).
    Fallback: exclusive file create (safe across workers on one machine if Redis is down).

    Returns True only for the single winner that should send.
    """
    # If a prior run on this host already claimed via file (e.g. Redis was down),
    # do not re-claim through Redis when it comes back.
    claim_path = _daily_job_claim_file(current_date)
    if os.path.exists(claim_path):
        logger.info(f"Daily report job for {current_date} already claimed locally; skipping.")
        return False

    redis_key = _daily_job_redis_key(current_date)

    try:
        claimed = _scheduler_redis.set(
            redis_key,
            "1",
            nx=True,
            ex=DAILY_JOB_TTL_SECONDS,
        )
        if claimed:
            # Best-effort local marker so a later Redis outage still sees "already done".
            try:
                fd = os.open(claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w") as f:
                    f.write(current_date)
            except FileExistsError:
                # Another local worker won the file race; we still hold Redis so we run.
                pass
            except Exception as fe:
                logger.warning(f"Claimed daily job in Redis but failed to write local marker: {fe}")
            logger.info(f"Claimed daily report job for {current_date} via Redis.")
            return True

        logger.info(f"Daily report job for {current_date} already claimed by another worker; skipping.")
        return False
    except Exception as re:
        logger.warning(f"Redis claim failed for daily report ({re}); falling back to local file claim.")
        return _try_claim_daily_job_file(current_date)


def _try_claim_daily_job_file(current_date: str) -> bool:
    """Exclusive local file claim (O_CREAT|O_EXCL). Only correct on a single host."""
    path = _daily_job_claim_file(current_date)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(current_date)
        logger.info(f"Claimed daily report job for {current_date} via local file fallback.")
        return True
    except FileExistsError:
        logger.info(f"Daily report job for {current_date} already claimed locally; skipping.")
        return False
    except Exception as fe:
        logger.error(f"Failed local file claim for daily report: {fe}")
        # Fail closed: better to skip than multi-send if we cannot coordinate.
        return False


def release_daily_job_claim(current_date: str) -> None:
    """
    Release the claim so another poll in the midnight window can retry after a send failure.
    """
    try:
        _scheduler_redis.delete(_daily_job_redis_key(current_date))
    except Exception as re:
        logger.warning(f"Failed to release Redis daily report claim for {current_date}: {re}")

    path = _daily_job_claim_file(current_date)
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as fe:
        logger.warning(f"Failed to release local daily report claim file for {current_date}: {fe}")

def get_log_counts_24h() -> dict:
    """
    Parses the local application log file to count entries by log level (INFO,
    WARNING, ERROR, CRITICAL) written within the last 24 hours.
    """
    counts = {"INFO": 0, "WARNING": 0, "ERROR": 0, "CRITICAL": 0}
    if not os.path.exists(log_file):
        return counts

    now = datetime.datetime.now()
    cutoff = now - timedelta(hours=24)

    try:
        with open(log_file, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split(" - ")
                if len(parts) >= 3:
                    ts_str = parts[0].split(",")[0]
                    try:
                        ts = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                        if ts >= cutoff:
                            level = parts[2].upper()
                            if level in counts:
                                counts[level] += 1
                    except Exception:
                        pass
    except Exception as e:
        logger.error(f"Failed to parse daily logs: {e}")
    return counts

def generate_and_send_report() -> bool:
    """
    Queries database metrics for unique visitors, link redirects, and link creations
    along with log occurrences in the last 24 hours, dispatches an HTML report to the admin.

    Returns True if the job completed (sent or intentionally skipped), False if a
    retriable failure occurred (caller should release the daily claim).
    """
    logger.info("Starting production daily analytics compilation...")
    
    now = datetime.datetime.now(UTC)
    last_24h = now - timedelta(hours=24)
    
    unique_visitors = 0
    total_clicks = 0
    new_links = 0
    
    try:
        with Session(engine) as session:
            # Query unique visitors (distinct ip_address)
            stmt_visitors = select(func.count(func.distinct(clicklog.ip_address))).where(clicklog.clicked_at >= last_24h)
            unique_visitors = session.exec(stmt_visitors).first() or 0

            # Query total clicks in the last 24 hours
            stmt_clicks = select(func.count(clicklog.id)).where(clicklog.clicked_at >= last_24h)
            total_clicks = session.exec(stmt_clicks).first() or 0

            # Query new links created in the last 24 hours
            stmt_links = select(func.count(urldata.short_url)).where(urldata.created_at >= last_24h)
            new_links = session.exec(stmt_links).first() or 0
    except Exception as e:
        logger.error(f"Database query failed during daily report compilation: {e}")
        
    log_counts = get_log_counts_24h()
    
    if not RESEND_API_KEY:
        logger.warning("RESEND_API_KEY is not defined in the environment. Skipping daily report dispatch.")
        # Completed (nothing to send) so we do not thrash every 15 minutes without config.
        return True
        
    if not TARGET_EMAIL:
        logger.warning("ADMIN_EMAIL is not defined in the environment. Skipping daily report dispatch.")
        return True

    url = "https://api.resend.com/emails"
    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # Beautiful responsive HTML email template for the executive report
    html_content = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 600px; margin: auto; padding: 24px; border: 1px solid #e2e8f0; border-radius: 12px; background-color: #ffffff;">
        <h2 style="color: #4f46e5; border-bottom: 2px solid #f1f5f9; padding-bottom: 12px; margin-top: 0; margin-bottom: 16px; font-size: 20px; font-weight: 700; tracking-tight: -0.025em;">
            FlexURL Daily System Summary
        </h2>
        <p style="color: #64748b; font-size: 13px; line-height: 1.5; margin-bottom: 24px;">
            This report summarizes database activity and runtime server logs for the 24-hour window ending at {now.strftime('%Y-%m-%d %H:%M:%S')} UTC.
        </p>
        
        <h3 style="color: #1e293b; font-size: 14px; font-weight: 600; margin-top: 0; margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.05em;">Link Traffic & Analytics</h3>
        <table style="width: 100%; border-collapse: collapse; margin-bottom: 28px;">
            <tr style="background-color: #f8fafc;">
                <td style="padding: 12px; border: 1px solid #e2e8f0; font-size: 13px; font-weight: 500; color: #475569;">New Links Created</td>
                <td style="padding: 12px; border: 1px solid #e2e8f0; font-size: 13px; color: #1e293b; text-align: right; font-weight: 600;">{new_links}</td>
            </tr>
            <tr>
                <td style="padding: 12px; border: 1px solid #e2e8f0; font-size: 13px; font-weight: 500; color: #475569;">Total Link Redirects (Clicks)</td>
                <td style="padding: 12px; border: 1px solid #e2e8f0; font-size: 13px; color: #1e293b; text-align: right; font-weight: 600;">{total_clicks}</td>
            </tr>
            <tr style="background-color: #f8fafc;">
                <td style="padding: 12px; border: 1px solid #e2e8f0; font-size: 13px; font-weight: 500; color: #475569;">Unique Visitors (IPs)</td>
                <td style="padding: 12px; border: 1px solid #e2e8f0; font-size: 13px; color: #1e293b; text-align: right; font-weight: 600;">{unique_visitors}</td>
            </tr>
        </table>
        
        <h3 style="color: #1e293b; font-size: 14px; font-weight: 600; margin-top: 0; margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.05em;">System Diagnostic Logs</h3>
        <table style="width: 100%; border-collapse: collapse; margin-bottom: 24px;">
            <thead>
                <tr style="background-color: #f1f5f9;">
                    <th style="padding: 12px; border: 1px solid #e2e8f0; text-align: left; font-size: 12px; font-weight: bold; color: #475569; text-transform: uppercase;">Severity Level</th>
                    <th style="padding: 12px; border: 1px solid #e2e8f0; text-align: right; font-size: 12px; font-weight: bold; color: #475569; text-transform: uppercase;">Count</th>
                </tr>
            </thead>
            <tbody>
                <tr>
                    <td style="padding: 12px; border: 1px solid #e2e8f0; font-size: 13px; color: #0d9488; font-weight: 600;">INFO</td>
                    <td style="padding: 12px; border: 1px solid #e2e8f0; font-size: 13px; color: #334155; text-align: right;">{log_counts['INFO']}</td>
                </tr>
                <tr style="background-color: #fffbeb;">
                    <td style="padding: 12px; border: 1px solid #e2e8f0; font-size: 13px; color: #d97706; font-weight: 600;">WARNING</td>
                    <td style="padding: 12px; border: 1px solid #e2e8f0; font-size: 13px; color: #334155; text-align: right;">{log_counts['WARNING']}</td>
                </tr>
                <tr style="background-color: #fef2f2;">
                    <td style="padding: 12px; border: 1px solid #e2e8f0; font-size: 13px; color: #dc2626; font-weight: 600;">ERROR</td>
                    <td style="padding: 12px; border: 1px solid #e2e8f0; font-size: 13px; color: #334155; text-align: right;">{log_counts['ERROR']}</td>
                </tr>
                <tr style="background-color: #fef2f2; border-top: 2px solid #dc2626;">
                    <td style="padding: 12px; border: 1px solid #e2e8f0; font-size: 13px; color: #991b1b; font-weight: 600;">CRITICAL</td>
                    <td style="padding: 12px; border: 1px solid #e2e8f0; font-size: 13px; color: #334155; text-align: right;">{log_counts['CRITICAL']}</td>
                </tr>
            </tbody>
        </table>
        
        <p style="color: #94a3b8; font-size: 11px; margin-top: 32px; border-top: 1px solid #f1f5f9; padding-top: 12px; text-align: center; line-height: 1.5;">
            Generated automatically by the FlexURL daily analytics dispatch agent.
        </p>
    </div>
    """
    
    payload = {
        "from": "FlexURL Reports <onboarding@resend.dev>",
        "to": [TARGET_EMAIL],
        "subject": f"FlexURL Executive Summary - {now.strftime('%Y-%m-%d')}",
        "html": html_content
    }
    
    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info("Daily executive summary report email dispatched successfully via Resend API.")
        return True
    except Exception as e:
        logger.error(f"Failed to dispatch daily executive report email via Resend API: {e}")
        return False


def send_dunning_email(email: str, subject: str, html_body: str):
    if not RESEND_API_KEY:
        logger.warning(f"RESEND_API_KEY is not defined in the environment. Skipping email dispatch to {email}.")
        return

    url = "https://api.resend.com/emails"
    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "from": "FlexURL Subscriptions <onboarding@resend.dev>",
        "to": [email],
        "subject": subject,
        "html": html_body
    }
    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info(f"Dunning email '{subject}' dispatched to {email} successfully.")
    except Exception as e:
        logger.error(f"Failed to dispatch dunning email to {email}: {e}")


def process_subscription_dunning_checks(now_utc: datetime.datetime):
    logger.info("Executing daily subscription dunning and expiration checks...")
    now_naive = now_utc.replace(tzinfo=None)
    
    with Session(engine) as session:
        statement = select(Subscription).where(Subscription.tier != "free")
        subscriptions = session.exec(statement).all()
        
        for sub in subscriptions:
            user = session.get(User, sub.user_id)
            if not user:
                continue
                
            expires_at = sub.current_period_end.replace(tzinfo=None) if sub.current_period_end.tzinfo else sub.current_period_end
            
            # 1. Expiring Soon Warning (5 days left)
            if expires_at > now_naive:
                days_left = (expires_at - now_naive).days
                if days_left <= 5 and not sub.dunning_warn_sent:
                    subject = f"Your FlexURL plan expires in {days_left} days"
                    html_content = f"""
                    <div style="font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 550px; margin: auto; padding: 30px; border: 1px solid #e2e8f0; border-radius: 16px; background-color: #ffffff;">
                        <div style="text-align: center; margin-bottom: 24px;">
                            <span style="background-color: #fffbeb; color: #d97706; font-size: 10px; font-weight: bold; text-transform: uppercase; letter-spacing: 0.05em; padding: 4px 12px; border-radius: 9999px;">Subscription Warning</span>
                        </div>
                        <h2 style="color: #1e293b; font-size: 18px; font-weight: 800; text-align: center; margin-top: 0;">Your FlexURL subscription is expiring soon</h2>
                        <p style="color: #475569; font-size: 13px; line-height: 1.6; margin-top: 16px; text-align: center;">
                            Your premium subscription ({sub.tier.upper()}) will expire in {days_left} days. To ensure uninterrupted access to custom domains, password-protected redirects, and detailed analytics, please renew your subscription.
                        </p>
                        <div style="text-align: center; margin: 28px 0;">
                            <a href="https://flexurl.app/dashboard" style="background-color: #4f46e5; color: #ffffff; padding: 12px 24px; border-radius: 8px; font-size: 13px; font-weight: bold; text-decoration: none; display: inline-block;">Renew Subscription</a>
                        </div>
                        <p style="color: #64748b; font-size: 11px; text-align: center; border-top: 1px solid #f1f5f9; padding-top: 16px; margin-top: 24px;">
                            If you have already renewed, please ignore this email.
                        </p>
                    </div>
                    """
                    send_dunning_email(user.email, subject, html_content)
                    sub.dunning_warn_sent = True
                    session.add(sub)
                    
            # 2. Expired & Relaxation Started (expired today / now >= expires_at)
            elif now_naive >= expires_at and sub.status == "active":
                sub.status = "relaxation"
                subject = "Your FlexURL subscription has expired - Grace Period Active"
                html_content = """
                <div style="font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 550px; margin: auto; padding: 30px; border: 1px solid #e2e8f0; border-radius: 16px; background-color: #ffffff;">
                    <div style="text-align: center; margin-bottom: 24px;">
                        <span style="background-color: #fef2f2; color: #dc2626; font-size: 10px; font-weight: bold; text-transform: uppercase; letter-spacing: 0.05em; padding: 4px 12px; border-radius: 9999px;">Grace Period Active</span>
                    </div>
                    <h2 style="color: #1e293b; font-size: 18px; font-weight: 800; text-align: center; margin-top: 0;">Your plan has expired</h2>
                    <p style="color: #475569; font-size: 13px; line-height: 1.6; margin-top: 16px; text-align: center;">
                        Your premium plan has expired. We have automatically moved your links to a 7-day relaxation grace period to keep them working. However, management options (creating links, viewing analytics, custom domains editing) are now restricted.
                    </p>
                    <p style="color: #475569; font-size: 13px; line-height: 1.6; text-align: center; font-weight: bold;">
                        Upgrade in the next 7 days to restore full access and prevent links and domains from being locked.
                    </p>
                    <div style="text-align: center; margin: 28px 0;">
                        <a href="https://flexurl.app/dashboard" style="background-color: #dc2626; color: #ffffff; padding: 12px 24px; border-radius: 8px; font-size: 13px; font-weight: bold; text-decoration: none; display: inline-block;">Restore Plan</a>
                    </div>
                </div>
                """
                send_dunning_email(user.email, subject, html_content)
                sub.dunning_expired_sent = True
                
                try:
                    redis_client.delete(f"user_tier:{user.id}")
                except Exception:
                    pass
                session.add(sub)
                
            # 3. Relaxation Ended & Downgraded (now >= expires_at + 7 days)
            elif now_naive >= (expires_at + timedelta(days=7)) and sub.status == "relaxation":
                sub.status = "expired"
                sub.tier = "free"
                user.tier = "free"
                
                subject = "Your FlexURL subscription has been downgraded"
                html_content = """
                <div style="font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 550px; margin: auto; padding: 30px; border: 1px solid #e2e8f0; border-radius: 16px; background-color: #ffffff;">
                    <div style="text-align: center; margin-bottom: 24px;">
                        <span style="background-color: #f1f5f9; color: #64748b; font-size: 10px; font-weight: bold; text-transform: uppercase; letter-spacing: 0.05em; padding: 4px 12px; border-radius: 9999px;">Downgraded</span>
                    </div>
                    <h2 style="color: #1e293b; font-size: 18px; font-weight: 800; text-align: center; margin-top: 0;">Grace period ended: Account Downgraded</h2>
                    <p style="color: #475569; font-size: 13px; line-height: 1.6; margin-top: 16px; text-align: center;">
                        The 7-day grace period for your subscription has ended. Your account has been downgraded to the Free Tier.
                    </p>
                    <p style="color: #475569; font-size: 13px; line-height: 1.6; text-align: center;">
                        Your custom domains and advanced links have been locked. They are not deleted and your data remains safe, but they are disabled in your workspace. You can restore access immediately by upgrading.
                    </p>
                    <div style="text-align: center; margin: 28px 0;">
                        <a href="https://flexurl.app/dashboard" style="background-color: #4f46e5; color: #ffffff; padding: 12px 24px; border-radius: 8px; font-size: 13px; font-weight: bold; text-decoration: none; display: inline-block;">Upgrade Now</a>
                    </div>
                </div>
                """
                send_dunning_email(user.email, subject, html_content)
                sub.dunning_ended_sent = True
                
                try:
                    redis_client.delete(f"user_tier:{user.id}")
                except Exception:
                    pass
                session.add(sub)
                session.add(user)
                
        session.commit()


async def daily_report_scheduler_loop():
    """
    Runs an infinite async loop that checks the time every 15 minutes.
    Dispatches a daily status email at midnight UTC once daily.

    Dedup is enforced by try_claim_daily_job() (Redis SET NX, file fallback) so
    multiple Gunicorn/Uvicorn workers cannot each send the executive summary.
    """
    logger.info("Starting daily executive report background scheduler loop...")
    while True:
        await asyncio.sleep(900)
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            if now.hour == 0:
                current_date = now.strftime("%Y-%m-%d")

                # Claim BEFORE sending. Only one worker across the fleet wins.
                if not try_claim_daily_job(current_date):
                    continue

                try:
                    report_ok = await asyncio.to_thread(generate_and_send_report)
                    await asyncio.to_thread(process_subscription_dunning_checks, now)
                    if not report_ok:
                        # Allow another poll in hour 0 to retry a failed Resend call.
                        logger.warning(
                            f"Daily report send failed for {current_date}; releasing claim for retry."
                        )
                        release_daily_job_claim(current_date)
                except Exception as job_ex:
                    logger.error(f"Daily report job crashed for {current_date}: {job_ex}")
                    release_daily_job_claim(current_date)
        except Exception as ex:
            logger.critical(f"Unhandled error in daily report scheduler loop: {ex}")
