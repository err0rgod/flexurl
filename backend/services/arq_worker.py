import asyncio
import os
import uuid
import httpx
from datetime import datetime, UTC
from sqlalchemy import text
from sqlmodel import Session, select
from arq import cron

from core.logger import core.logger
from core.database import engine, add_clicklog
from models.domain import urldata, clicklog, User
from utils.analytics_parser import parse_referer, parse_user_agent, check_is_bot
from services.arq_settings import (
    redis_settings, CLICK_FLUSH_INTERVAL, GEO_CACHE_TTL,
    REDIS_LOCK_TIMEOUT, GEO_IP_TIMEOUT, WEBHOOK_TIMEOUT
)
from core.redis_client import core.redis_client

# Global HTTP client to reuse connections
http_client = None

async def startup(ctx):
    global http_client
    http_client = httpx.AsyncClient()
    logger.info("ARQ worker initialized successfully.")
    logger.info(
        f"Configuration summary: CLICK_FLUSH_INTERVAL={CLICK_FLUSH_INTERVAL}s, "
        f"GEO_CACHE_TTL={GEO_CACHE_TTL}s, REDIS_LOCK_TIMEOUT={REDIS_LOCK_TIMEOUT}s, "
        f"GEO_IP_TIMEOUT={GEO_IP_TIMEOUT}s, WEBHOOK_TIMEOUT={WEBHOOK_TIMEOUT}s"
    )
    logger.info("Startup complete - Worker HTTP client initialized")

async def shutdown(ctx):
    global http_client
    if http_client:
        await http_client.aclose()
    logger.info("ARQ worker shutdown complete.")

async def get_ip_location_cached(ip: str) -> tuple[str, str]:
    if ip in ("127.0.0.1", ":1", "localhost") or ip.startswith("192.168") or ip.startswith("10."):
        logger.debug(f"Local IP bypass: {ip}")
        return "Local/Internal", "Local/Internal"
    
    geo_key = f"geo:{ip}"
    
    try:
        start_time = datetime.now()
        cached_geo = redis_client.get(geo_key)
        redis_duration = (datetime.now() - start_time).total_seconds() * 1000
        logger.debug(f"Redis GET (Geo lookup): {redis_duration:.2f} ms")
        
        if cached_geo:
            logger.debug(f"Geo lookup cache hit for IP: {ip}")
            parts = cached_geo.split("|")
            if len(parts) == 2:
                return parts[0], parts[1]
    except Exception as e:
        logger.warning(f"Failed to read Geo cache for {ip}: {e}")
        
    logger.debug(f"Geo lookup cache miss for IP: {ip}")
    url = f"http://ip-api.com/json/{ip}"
    try:
        start_time = datetime.now()
        response = await http_client.get(url, timeout=GEO_IP_TIMEOUT)
        duration = (datetime.now() - start_time).total_seconds() * 1000
        logger.debug(f"Geo lookup API request duration: {duration:.2f} ms")
        
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "success":
                country = data.get("country", "Unknown")
                city = data.get("city", "Unknown")
                
                try:
                    redis_client.set(geo_key, f"{country}|{city}", ex=GEO_CACHE_TTL)
                    logger.debug(f"Geo lookup cached for IP: {ip}")
                except Exception as cache_err:
                    logger.warning(f"Failed to write Geo cache for {ip}: {cache_err}")
                return country, city
            else:
                logger.warning(f"Geo lookup API status error for IP {ip}: {data.get('message')}")
    except httpx.TimeoutException:
        logger.warning(f"GeoIP timeout occurred for IP: {ip}")
    except Exception as err:
        logger.warning(f"Geo lookup failure for IP {ip}: {err}")
        
    return "Unknown", "Unknown"

async def record_analytics(ctx, event: dict):
    start_time = datetime.now()
    short_url = event.get("short_url")
    ip_address = event.get("ip")
    user_agent = event.get("user_agent", "")
    referer = event.get("referer", "Direct")
    
    logger.debug(f"Worker job started: record_analytics for {short_url}")
    
    # 1. Geo-IP lookup (best effort)
    country, city = "Unknown", "Unknown"
    try:
        country, city = await get_ip_location_cached(ip_address)
    except Exception as e:
        logger.warning(f"Geo lookup step failed for {short_url}: {e}")

    # 2. Parsing Metadata (best effort)
    browser, device = "Unknown", "Unknown"
    clean_referer = "Direct/Email"
    is_bot = False
    try:
        browser, device = parse_user_agent(user_agent)
        clean_referer = parse_referer(referer)
        is_bot = check_is_bot(user_agent)
    except Exception as e:
        logger.warning(f"Parsing Metadata step failed for {short_url}: {e}")
        
    log = clicklog(
        short_url=short_url,
        ip_address=ip_address,
        country=country,
        city=city,
        browser=browser,
        device=device,
        referer=clean_referer,
        is_bot=is_bot
    )
    clicked_at_dt = log.clicked_at or datetime.now(UTC)
    
    # 3. Write Click Log to DB (best effort)
    try:
        add_clicklog(log)
        logger.debug(f"Click log written to DB for {short_url}")
    except Exception as e:
        logger.exception(f"Failed to write click log to DB for {short_url}: {e}")
        
    # 4. Increment Click Count in Redis (best effort, atomic)
    try:
        click_key = f"clicks:{short_url}"
        redis_client.incr(click_key)
        logger.debug(f"Incremented click count in Redis for {short_url}")
    except Exception as e:
        logger.warning(f"Failed to increment click count in Redis for {short_url}: {e}")
        
    # 5. Send Premium Webhook (best effort)
    try:
        with Session(engine) as db_session:
            db_start = datetime.now()
            statement = select(urldata).where(urldata.short_url == short_url)
            url_entry = db_session.exec(statement).first()
            db_duration = (datetime.now() - db_start).total_seconds() * 1000
            logger.debug(f"PostgreSQL select duration in worker: {db_duration:.2f} ms")
            
            if url_entry and url_entry.webhook_url and url_entry.user_id:
                user = db_session.get(User, url_entry.user_id)
                if user and user.tier in ("premium", "startup", "business"):
                    webhook_payload = {
                        "short_url": url_entry.short_url,
                        "long_url": url_entry.long_url,
                        "clicked_at": clicked_at_dt.isoformat(),
                        "ip_address": ip_address,
                        "country": country,
                        "city": city,
                        "browser": browser,
                        "device": device,
                        "referer": clean_referer,
                        "is_bot": is_bot
                    }
                    try:
                        wh_start = datetime.now()
                        response = await http_client.post(url_entry.webhook_url, json=webhook_payload, timeout=WEBHOOK_TIMEOUT)
                        wh_duration = (datetime.now() - wh_start).total_seconds() * 1000
                        logger.debug(f"Webhook dispatch duration: {wh_duration:.2f} ms")
                        
                        if response.status_code >= 400:
                            logger.warning(f"Webhook delivery failed with status {response.status_code} for {short_url}")
                        else:
                            logger.debug(f"Webhook successfully delivered to {url_entry.webhook_url}")
                    except httpx.TimeoutException:
                        logger.warning(f"Webhook timeout occurred for short_url: {short_url}")
                    except Exception as web_err:
                        logger.warning(f"Webhook post failure for {short_url}: {web_err}")
    except Exception as e:
        logger.warning(f"Webhook processing stage failed for {short_url}: {e}")
        
    job_duration = (datetime.now() - start_time).total_seconds() * 1000
    logger.info(f"Job finished: record_analytics for {short_url} (duration: {job_duration:.2f} ms)")

async def flush_clicks(ctx):
    """
    Periodically flushes accumulated Redis click counters to PostgreSQL in bulk
    using a crash-safe read-commit-decrby algorithm to prevent data loss.
    A distributed Redis lock ensures that concurrent workers do not run this task together.
    """
    lock_key = "lock:flush_clicks"
    token = str(uuid.uuid4())
    
    # Acquire lock atomically (SET NX EX)
    try:
        acquired = redis_client.set(lock_key, token, nx=True, ex=REDIS_LOCK_TIMEOUT)
        if not acquired:
            logger.debug("Another worker is already flushing click counters. Exiting flush_clicks cron job.")
            return
    except Exception as e:
        logger.warning(f"Failed to acquire Redis lock for click flush: {e}")
        return

    start_time = datetime.now()
    logger.debug("Distributed lock acquired. Click flush started.")
    
    try:
        keys = redis_client.keys("clicks:*")
        if not keys:
            logger.debug("No click counters to flush")
            return
            
        batch = {}
        for key in keys:
            val = redis_client.get(key)
            if val:
                count = int(val)
                if count > 0:
                    short_url = key.split(":", 1)[1]
                    batch[short_url] = count
                    
        if not batch:
            return
            
        # 1. Update the database. If this fails, the exception is caught,
        # transaction is rolled back, and counts in Redis remain unchanged.
        try:
            db_start = datetime.now()
            with Session(engine) as session:
                for short_url, increment in batch.items():
                    session.execute(
                        text("UPDATE urldata SET click_count = click_count + :increment WHERE short_url = :short_url"),
                        {"increment": increment, "short_url": short_url}
                    )
                session.commit()
            db_duration = (datetime.now() - db_start).total_seconds() * 1000
            logger.debug(f"PostgreSQL update duration: {db_duration:.2f} ms")
        except Exception as db_err:
            logger.error(f"PostgreSQL transaction failure during click flush: {db_err}")
            raise db_err
            
        # 2. Database write succeeded. Decrement the counters in Redis
        # by the exact amount we successfully committed to the database.
        pipe = redis_client.pipeline()
        for short_url, increment in batch.items():
            pipe.decrby(f"clicks:{short_url}", increment)
        pipe.execute()
        
        # 3. Clean up keys that have reached 0 to prevent memory leak
        for short_url in batch.keys():
            key = f"clicks:{short_url}"
            new_val = redis_client.get(key)
            if new_val is not None and int(new_val) <= 0:
                redis_client.delete(key)
                
        flush_duration = (datetime.now() - start_time).total_seconds() * 1000
        logger.info(f"Click counter flushed successfully. Batch size: {len(batch)} (duration: {flush_duration:.2f} ms)")
        
    except Exception as e:
        logger.error(f"Failed to flush click counters to database: {e}", exc_info=True)
    finally:
        # Release lock safely using a Lua script to verify token ownership
        release_script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        try:
            released = redis_client.register_script(release_script)(keys=[lock_key], args=[token])
            if released:
                logger.debug("Distributed lock released successfully.")
            else:
                logger.warning("Failed to release distributed lock: Lock may have expired or was modified by another worker.")
        except Exception as lock_err:
            logger.warning(f"Error occurred while releasing Redis lock: {lock_err}")

class WorkerSettings:
    functions = [record_analytics]
    cron_jobs = [
        cron(flush_clicks, second=set(range(0, 60, CLICK_FLUSH_INTERVAL)))
    ]
    redis_settings = redis_settings
    on_startup = startup
    on_shutdown = shutdown
