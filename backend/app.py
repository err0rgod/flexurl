import sys
import os
import json
import asyncio
import hashlib
from html import escape

# Ensure the backend directory is in python path for local imports
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.append(BASE_DIR)

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks, Response, Depends
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from utils.short_url_gen import add_url, serve_url, ban_in_cache, add_custom_url, get_user_tier, redis_client
from core.database import mark_url_banned, init_db, add_clicklog, engine
from utils.validations import is_valid_url, check_safe_browsing, is_valid_custom_alias
from utils.ratelimit import RateLimiterStore
from api.routes.auth import router as auth_router
from api.routes.quotation import process_quotation
from services.cloudflare_saas import CloudflareSaaSManager
from utils.expiration_policy import calculate_link_expiration
from models.domain import (
    clicklog, urldata, User, CustomDomain, URLRequest, URLEditRequest,
    QuoteRequest, SupportTicketRequest, PaymentOrderRequest, PaymentVerifyRequest,
    CustomDomainRequest, ApiKey, APIKeyCreateRequest, DeveloperURLRequest,
    DeveloperBatchURLRequest
)
from utils.analytics_parser import parse_referer, parse_user_agent, get_ip_country, get_ip_location, check_is_bot
from typing import Optional
from datetime import datetime, UTC
import time
import jwt
from sqlmodel import Session, select
from sqlalchemy import func, text
from contextlib import asynccontextmanager
from core.logger import core.logger
from services.report_scheduler import daily_report_scheduler_loop

JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")


# Auth dependency helpers
def get_optional_user_id(request: Request) -> Optional[int]:
    token = request.cookies.get("session_token")
    if not token:
        return None
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=["HS256"])
        user_id = payload.get("user_id")
        if user_id is not None:
            return int(user_id)
    except jwt.PyJWTError:
        pass
    return None


def get_required_user_id(request: Request) -> int:
    user_id = get_optional_user_id(request)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user_id


async def get_developer_user_id(request: Request) -> int:
    import hashlib
    # 1. Extract API Key
    auth_header = request.headers.get("Authorization")
    raw_key = None
    if auth_header and auth_header.startswith("Bearer "):
        raw_key = auth_header[7:]
    else:
        raw_key = request.headers.get("X-API-Key")
        
    if not raw_key:
        raise HTTPException(status_code=401, detail="Missing API Key in Authorization header or X-API-Key header")
        
    # 2. Hash raw key
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    
    # 3. Check Redis Cache
    from utils.short_url_gen import core.redis_client
    try:
        cached = redis_client.get(f"api_key:{key_hash}")
        if cached:
            val = cached.decode() if isinstance(cached, bytes) else cached
            parts = val.split(":")
            if len(parts) == 2:
                u_id, tier = int(parts[0]), parts[1]
                if tier in ("premium", "startup", "business"):
                    # 4. Developer Rate Limiting
                    rate_limit = 60
                    if tier == "startup":
                        rate_limit = 180
                    elif tier == "business":
                        rate_limit = 600
                        
                    rate_key = f"api_limit:{u_id}:{datetime.now(UTC).minute}"
                    current_reqs = redis_client.incr(rate_key)
                    if current_reqs == 1:
                        redis_client.expire(rate_key, 60)
                    if current_reqs > rate_limit:
                        raise HTTPException(
                            status_code=429,
                            detail=f"API Rate limit exceeded ({rate_limit} requests/minute). Please upgrade or reduce request frequency."
                        )
                    return u_id
                raise HTTPException(status_code=403, detail="Developer API access requires a premium subscription.")
    except HTTPException:
        raise
    except Exception as re:
        logger.warning(f"Redis offline for API key check: {re}")
        
    # 5. Check PostgreSQL DB
    with Session(engine) as db_session:
        from models.domain import ApiKey
        statement = select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.is_active == True)
        api_key_entry = db_session.exec(statement).first()
        if not api_key_entry:
            raise HTTPException(status_code=401, detail="Invalid or revoked API Key")
            
        user = db_session.get(User, api_key_entry.user_id)
        if not user:
            raise HTTPException(status_code=401, detail="User associated with key not found")
            
        tier = get_user_tier(user.id, db_session)
        if tier not in ("premium", "startup", "business"):
            raise HTTPException(status_code=403, detail="Developer API access requires a premium subscription.")
            
        # 6. Cache in Redis
        try:
            redis_client.set(f"api_key:{key_hash}", f"{user.id}:{tier}", ex=86400)
        except Exception as re:
            logger.warning(f"Failed to cache API key: {re}")
            
        # 7. Check Developer Rate Limiting (DB hit path)
        rate_limit = 60
        if tier == "startup":
            rate_limit = 180
        elif tier == "business":
            rate_limit = 600
            
        rate_key = f"api_limit:{user.id}:{datetime.now(UTC).minute}"
        try:
            current_reqs = redis_client.incr(rate_key)
            if current_reqs == 1:
                redis_client.expire(rate_key, 60)
            if current_reqs > rate_limit:
                raise HTTPException(
                    status_code=429,
                    detail=f"API Rate limit exceeded ({rate_limit} requests/minute). Please upgrade or reduce request frequency."
                )
        except HTTPException:
            raise
        except Exception as re:
            logger.warning(f"Failed to check developer rate limits: {re}")
            
        return user.id


# get_user_tier is imported from short_url_gen

import razorpay
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")

razorpay_client = None
if RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET:
    razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

FRONTEND_DIR = os.path.join(os.path.dirname(BASE_DIR), "frontend")

init_db() #for initialising table structure

def compile_tailwind():
    """
    Checks if tailwind config and input CSS exist, and runs npx @tailwindcss/cli
    to compile static/output.css in the background. Fails silently if Node/npx is not installed.
    """
    try:
        import subprocess
        # Paths
        input_css_dir = os.path.join(FRONTEND_DIR, "static")
        input_css = os.path.join(input_css_dir, "input.css")
        output_css = os.path.join(input_css_dir, "output.css")
        config_path = os.path.join(os.path.dirname(BASE_DIR), "tailwind.config.js")

        # 1. Create input.css if it doesn't exist
        if not os.path.exists(input_css):
            with open(input_css, "w", encoding="utf-8") as f:
                f.write("@tailwind base;\n@tailwind components;\n@tailwind utilities;\n")

        # 2. Create tailwind.config.js if it doesn't exist
        if not os.path.exists(config_path):
            with open(config_path, "w", encoding="utf-8") as f:
                f.write("""module.exports = {
  content: ["./frontend/**/*.html"],
  darkMode: 'class',
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'sans-serif'],
        mono: ['Fira Code', 'monospace'],
      }
    }
  },
  plugins: [],
}""")

        # 3. Compile using @tailwindcss/cli
        logger.info("Auto-compiling Tailwind CSS...")
        cmd = ["npx", "@tailwindcss/cli", "-i", input_css, "-o", output_css, "--minify"]
        subprocess.run(cmd, shell=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info("Tailwind CSS compilation complete.")
    except Exception as e:
        logger.warning(f"Tailwind CSS automatic compilation skipped (Node/npx not available or failed): {e}")

# Cached in-memory HTML templates
HTML_TEMPLATES = {}

def load_html_templates():
    global HTML_TEMPLATES
    for name in ["banned.html", "expired.html", "countdown.html", "password_gate.html"]:
        path = os.path.join(FRONTEND_DIR, name)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    HTML_TEMPLATES[name] = f.read()
                logger.info(f"Loaded HTML template: {name}")
            except Exception as e:
                logger.error(f"Failed to load template {name}: {e}")
        else:
            logger.warning(f"HTML template not found: {path}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Application starting up...")
    
    # 1. Initialize ARQ redis connection pool
    try:
        from arq import create_pool
        from services.arq_settings import redis_settings
        app.state.arq_pool = await create_pool(redis_settings)
        logger.info("ARQ Redis pool connection initialized.")
    except Exception as e:
        logger.critical(f"ARQ Redis unavailable at startup: {e}")
        
    # 2. Cache HTML templates
    load_html_templates()
    
    # 3. Auto-compile Tailwind CSS in the background on startup
    await asyncio.to_thread(compile_tailwind)
    
    logger.info("Launching background daily report scheduler...")
    scheduler_task = asyncio.create_task(daily_report_scheduler_loop())
    yield
    logger.info("Application shutting down...")
    if hasattr(app.state, "arq_pool") and app.state.arq_pool:
        await app.state.arq_pool.close()
        logger.info("ARQ Redis pool closed.")
        
    logger.info("Canceling background report scheduler task...")
    scheduler_task.cancel()
    try:
        await scheduler_task
    except asyncio.CancelledError:
        pass
    logger.info("Application shutdown completed.")



app = FastAPI(lifespan=lifespan)

from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory=os.path.join(FRONTEND_DIR, "static")), name="static")

app.include_router(auth_router)

def get_client_ip(request: Request) -> str:
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip
    x_forwarded = request.headers.get("x-forwarded-for")
    if x_forwarded:
        return x_forwarded.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"


BADGE_COUNTER_ALIASES = {
    "links": "links_created",
    "links-created": "links_created",
    "links_created": "links_created",
    "visits": "visitors",
    "visitors": "visitors",
    "redirect": "redirects",
    "redirects": "redirects",
}

BADGE_DEFAULT_LABELS = {
    "links_created": "links created",
    "visitors": "visitors",
    "redirects": "redirects",
}


def compact_badge_number(value: int) -> str:
    for suffix, divisor in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if value >= divisor:
            compact = value / divisor
            return f"{compact:.1f}{suffix}" if compact < 10 else f"{compact:.0f}{suffix}"
    return str(value)


def normalize_badge_color(value: str, fallback: str) -> str:
    color = value.strip().lstrip("#")
    if len(color) in (3, 6) and all(char in "0123456789abcdefABCDEF" for char in color):
        return f"#{color}"
    named_colors = {
        "black": "#111827",
        "green": "#16a34a",
        "blue": "#2563eb",
        "red": "#dc2626",
        "gray": "#64748b",
        "slate": "#334155",
        "orange": "#ea580c",
        "purple": "#7c3aed",
    }
    return named_colors.get(value.strip().lower(), fallback)


def badge_text_width(value: str) -> int:
    return max(36, len(value) * 7 + 18)


def render_counter_badge(label: str, value: str, left_color: str, right_color: str) -> str:
    label_width = badge_text_width(label)
    value_width = badge_text_width(value)
    total_width = label_width + value_width
    safe_label = escape(label)
    safe_value = escape(value)

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{total_width}" height="20" role="img" aria-label="{safe_label}: {safe_value}">
  <title>{safe_label}: {safe_value}</title>
  <linearGradient id="s" x2="0" y2="100%">
    <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>
    <stop offset="1" stop-opacity=".1"/>
  </linearGradient>
  <clipPath id="r">
    <rect width="{total_width}" height="20" rx="3" fill="#fff"/>
  </clipPath>
  <g clip-path="url(#r)">
    <rect width="{label_width}" height="20" fill="{left_color}"/>
    <rect x="{label_width}" width="{value_width}" height="20" fill="{right_color}"/>
    <rect width="{total_width}" height="20" fill="url(#s)"/>
  </g>
  <g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" text-rendering="geometricPrecision" font-size="110">
    <text aria-hidden="true" x="{label_width * 5}" y="150" fill="#010101" fill-opacity=".3" transform="scale(.1)" textLength="{(label_width - 10) * 10}">{safe_label}</text>
    <text x="{label_width * 5}" y="140" transform="scale(.1)" fill="#fff" textLength="{(label_width - 10) * 10}">{safe_label}</text>
    <text aria-hidden="true" x="{(label_width + value_width / 2) * 10}" y="150" fill="#010101" fill-opacity=".3" transform="scale(.1)" textLength="{(value_width - 10) * 10}">{safe_value}</text>
    <text x="{(label_width + value_width / 2) * 10}" y="140" transform="scale(.1)" fill="#fff" textLength="{(value_width - 10) * 10}">{safe_value}</text>
  </g>
</svg>
"""


limiter = RateLimiterStore(max_tokens=60, refill_rate=60, interval=60)


@app.middleware("http")
async def rate_limit_middleware(request : Request, call_next):
    client_ip = get_client_ip(request)
    bucket = limiter.get_bucket(client_ip)
    if not bucket.allow_request():
        retry_after = bucket.get_reset_time()- time.time()
        accept_header = request.headers.get("accept", "")
        
        if "text/html" in accept_header:
            return HTMLResponse(
                content=f"<html><body><h1>Too Many Requests</h1><p>Please try again after {retry_after:.0f} seconds.</p></body></html>",
                status_code=429
            )
        
        return JSONResponse(
            status_code=429,
            content={"detail": f"Too many requests. Please try again after {retry_after:.0f} seconds."},
            headers={
                "Retry-After":str(max(1, int(retry_after))),
                "X-RateLimit-Limit":str(bucket.max_tokens),
                "X-RateLimit-Remaining":str(bucket.get_remaining()),
                "X-RateLimit-Reset": str(int(bucket.get_reset_time()))
            },
        )
    
    # if valid return normal
    response = await call_next(request)
    response.headers["X-RateLimit-Limit"]=str(bucket.max_tokens)
    response.headers["X-RateLimit-Remaining"]=str(bucket.get_remaining())
    response.headers["X-RateLimit-Reset"]=str(bucket.get_reset_time())
    return response


# checks if the url is safe or not, if not marks them as banned in the Database & when a user visits a banned url he see's a banned url page
async def background_safe_browsing_check(short_url: str, long_url: str):
    is_safe = await check_safe_browsing(long_url)
    if not is_safe:
        mark_url_banned(short_url)
        ban_in_cache(short_url)

# serving essesntial SEO files
@app.get("/robots.txt")
async def robots():
    with open(os.path.join(FRONTEND_DIR, "robots.txt"), encoding="utf-8") as f:
        return Response(content=f.read(), media_type="text/plain")
@app.get("/sitemap.xml")
async def sitemap():
    with open(os.path.join(FRONTEND_DIR, "sitemap.xml"), encoding="utf-8") as f:
        return Response(content=f.read(), media_type="application/xml")
@app.get("/security.txt")
@app.get("/.well-known/security.txt")
async def security():
    with open(os.path.join(FRONTEND_DIR, "security.txt"), encoding="utf-8") as f:
        return Response(content=f.read(), media_type="text/plain")


@app.get("/badge/{counter}.svg")
async def counter_badge(
    counter: str,
    left_text: Optional[str] = None,
    left_color: str = "black",
    right_color: str = "green",
    units: str = "compact",
):
    counter_name = BADGE_COUNTER_ALIASES.get(counter.strip().lower())
    if not counter_name:
        raise HTTPException(status_code=404, detail="Unknown badge counter")
    if units not in ("compact", "raw"):
        raise HTTPException(status_code=400, detail="units must be compact or raw")

    with Session(engine) as db_session:
        value = db_session.execute(
            text("SELECT value FROM public.badge_counters WHERE name = :name"),
            {"name": counter_name},
        ).scalar_one_or_none()

    count = int(value or 0)
    label = (left_text or BADGE_DEFAULT_LABELS[counter_name])[:40]
    badge_value = compact_badge_number(count) if units == "compact" else str(count)
    svg = render_counter_badge(
        label=label,
        value=badge_value,
        left_color=normalize_badge_color(left_color, "#111827"),
        right_color=normalize_badge_color(right_color, "#16a34a"),
    )
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={
            "Cache-Control": "public, max-age=300",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/sad_meme.mp4")
async def sad_meme():
    from fastapi.responses import FileResponse
    return FileResponse(os.path.join(FRONTEND_DIR, "static", "sad_meme.mp4"), media_type="video/mp4")


@app.get("/dancing_meme.mp4")
async def dancing_meme():
    from fastapi.responses import FileResponse
    return FileResponse(os.path.join(FRONTEND_DIR, "static", "dancing_meme.mp4"), media_type="video/mp4")


# main website returns the index page
@app.get("/", response_class=HTMLResponse)
async def index():  
    with open(os.path.join(FRONTEND_DIR, "index.html"), encoding="utf-8") as f:
        return f.read()

# essential legal files
@app.get("/privacy", response_class=HTMLResponse)
async def privacy():
    with open(os.path.join(FRONTEND_DIR, "privacy.html"), encoding="utf-8") as f:
        return f.read()
@app.get("/terms", response_class=HTMLResponse)
async def terms():
    with open(os.path.join(FRONTEND_DIR, "terms.html"), encoding="utf-8") as f:
        return f.read()


# login endpoint 
@app.get("/login", response_class=HTMLResponse)
async def login():
    with open(os.path.join(FRONTEND_DIR, "login.html"), encoding="utf-8") as f:
        return f.read()

# quotes page for custom quotes from startups and teams
@app.get("/quotes", response_class=HTMLResponse)
async def quotes():
    with open(os.path.join(FRONTEND_DIR, "contact.html"), encoding="utf-8") as f:
        return f.read()


# support page route
@app.get("/support", response_class=HTMLResponse)
async def support():
    with open(os.path.join(FRONTEND_DIR, "support.html"), encoding="utf-8") as f:
        return f.read()
# post endpoint for sending the details to admin
@app.post("/contact-sales")
async def contact_sales(quote: QuoteRequest, background_tasks: BackgroundTasks):
    try:
        # Process quotation asynchronously in background tasks (file write + Resend API post)
        background_tasks.add_task(process_quotation, quote.model_dump())
        return {"status": "success", "message": "Quotation submitted successfully"}
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to process quotation request")


# documentation page route
@app.get("/documentation", response_class=HTMLResponse)
async def documentation():
    with open(os.path.join(FRONTEND_DIR, "documentation.html"), encoding="utf-8") as f:
        return f.read()


# post endpoint for support tickets
@app.post("/api/support")
async def create_support_ticket(ticket: SupportTicketRequest, background_tasks: BackgroundTasks):
    try:
        from api.routes.support_ticket import process_support_ticket
        background_tasks.add_task(process_support_ticket, ticket.model_dump())
        return {"status": "success", "message": "Support ticket submitted successfully"}
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to process support ticket")

# record_analytics is processed by the background arq worker in workers.py


@app.post("/api/payments/create-order")
async def create_payment_order(req_data: PaymentOrderRequest, request: Request, user_id: int = Depends(get_required_user_id)):
        
    if not razorpay_client:
        raise HTTPException(status_code=500, detail="Razorpay client not configured")
        
    country = request.headers.get("CF-IPCountry", "IN")
    is_india = (country == "IN")
    
    try:
        if is_india:
            amount = 249900 if req_data.plan == "business" else 79900
            currency = "INR"
        else:
            amount = 2900 if req_data.plan == "business" else 1100
            currency = "USD"
            
        order_data = {
            "amount": amount,
            "currency": currency,
            "receipt": f"receipt_{user_id}_{int(time.time())}"
        }
        order = razorpay_client.order.create(data=order_data)
        return {
            "order_id": order["id"],
            "amount": order["amount"],
            "currency": order["currency"],
            "key_id": RAZORPAY_KEY_ID
        }
    except Exception as e:
        logger.error(f"Error creating Razorpay order: {e}")
        raise HTTPException(status_code=500, detail="Failed to initiate payment")


@app.post("/api/payments/create-trial-order")
async def create_trial_order(request: Request, user_id: int = Depends(get_required_user_id)):
    if not razorpay_client:
        raise HTTPException(status_code=500, detail="Razorpay client not configured")
        
    with Session(engine) as db_session:
        user = db_session.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if user.has_used_trial:
            raise HTTPException(status_code=400, detail="You have already used your trial option.")
            
    country = request.headers.get("CF-IPCountry", "IN")
    is_india = (country == "IN")
    
    try:
        amount = 900 if is_india else 10  # ₹9.00 or $0.10 USD
        currency = "INR" if is_india else "USD"
        
        order_data = {
            "amount": amount,
            "currency": currency,
            "receipt": f"trial_{user_id}_{int(time.time())}"
        }
        order = razorpay_client.order.create(data=order_data)
        return {
            "order_id": order["id"],
            "amount": order["amount"],
            "currency": order["currency"],
            "key_id": RAZORPAY_KEY_ID
        }
    except Exception as e:
        logger.error(f"Error creating Razorpay trial order: {e}")
        raise HTTPException(status_code=500, detail="Failed to initiate trial payment")


@app.post("/api/payments/verify")
async def verify_payment(req_data: PaymentVerifyRequest, user_id: int = Depends(get_required_user_id)):
        
    if not razorpay_client:
        raise HTTPException(status_code=500, detail="Razorpay client not configured")
        
    try:
        params = {
            'razorpay_order_id': req_data.razorpay_order_id,
            'razorpay_payment_id': req_data.razorpay_payment_id,
            'razorpay_signature': req_data.razorpay_signature
        }
        razorpay_client.utility.verify_payment_signature(params)
    except Exception as e:
        logger.error(f"Signature verification failed: {e}")
        raise HTTPException(status_code=400, detail="Invalid payment signature")
        
    with Session(engine) as db_session:
        user = db_session.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        try:
            order_info = razorpay_client.order.fetch(req_data.razorpay_order_id)
            amount = order_info.get("amount", 79900)
            currency = order_info.get("currency", "INR")
        except Exception:
            amount = 79900
            currency = "INR"
            
        if currency == "USD":
            is_trial_payment = (amount == 10)
            target_tier = "business" if amount == 2900 else "startup"
        else:
            is_trial_payment = (amount == 900)
            target_tier = "business" if amount == 249900 else "startup"
        
        user.tier = target_tier
        if is_trial_payment:
            user.has_used_trial = True
            
        from models.domain import Subscription
        statement = select(Subscription).where(Subscription.user_id == user_id)
        sub = db_session.exec(statement).first()
        
        from datetime import datetime, UTC, timedelta
        now = datetime.now(UTC).replace(tzinfo=None)
        
        duration_days = 14 if is_trial_payment else 30
        
        if not sub:
            sub = Subscription(
                user_id=user_id,
                tier=target_tier,
                current_period_start=now,
                current_period_end=now + timedelta(days=duration_days),
                relaxation_days_remaining=7,
                status="active",
                is_trial=is_trial_payment
            )
        else:
            sub.tier = target_tier
            sub.status = "active"
            sub.is_trial = is_trial_payment
            sub.relaxation_days_remaining = 7
            sub.dunning_warn_sent = False
            sub.dunning_expired_sent = False
            sub.dunning_ended_sent = False
            if not is_trial_payment and sub.current_period_end and sub.current_period_end > now:
                sub.current_period_end = sub.current_period_end + timedelta(days=duration_days)
            else:
                sub.current_period_end = now + timedelta(days=duration_days)
                
        user.plan_expires_at = sub.current_period_end
        user.relaxation_days_remaining = 7
        
        db_session.add(sub)
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
        try:
            from utils.short_url_gen import core.redis_client
            redis_client.delete(f"user_tier:{user_id}")
        except Exception:
            pass
        
    return {"status": "success", "message": f"Successfully upgraded to {user.tier}!"}


# analytics endpoint for specific short url only accessed by owner
@app.get("/api/analytics/{short_url}")
async def get_url_analytics(short_url: str, user_id: int = Depends(get_required_user_id)):

    with Session(engine) as db_session:
        # Check if URL exists and user owns it
        statement = select(urldata).where(urldata.short_url == short_url)
        url_entry = db_session.exec(statement).first()
        if not url_entry:
            raise HTTPException(status_code=404, detail="Short URL not found")
        if url_entry.user_id is not None and url_entry.user_id != user_id:
            raise HTTPException(status_code=403, detail="Forbidden")

        # Check if user is premium
        tier = get_user_tier(user_id, db_session)
        if tier not in ("premium", "startup", "business"):
            raise HTTPException(
                status_code=403, 
                detail="Analytics are a premium feature. Please upgrade your subscription."
            )

        # Clicks by date
        by_date_query = db_session.exec(
            select(func.date(clicklog.clicked_at), func.count(clicklog.id))
            .where(clicklog.short_url == short_url)
            .group_by(func.date(clicklog.clicked_at))
            .order_by(func.date(clicklog.clicked_at))
        ).all()
        by_date = [{"date": str(row[0]), "clicks": row[1]} for row in by_date_query]

        # Clicks by browser
        by_browser_query = db_session.exec(
            select(clicklog.browser, func.count(clicklog.id))
            .where(clicklog.short_url == short_url)
            .group_by(clicklog.browser)
        ).all()
        by_browser = [{"browser": row[0], "clicks": row[1]} for row in by_browser_query]

        # Clicks by device
        by_device_query = db_session.exec(
            select(clicklog.device, func.count(clicklog.id))
            .where(clicklog.short_url == short_url)
            .group_by(clicklog.device)
        ).all()
        by_device = [{"device": row[0], "clicks": row[1]} for row in by_device_query]

        # Clicks by country
        by_country_query = db_session.exec(
            select(clicklog.country, func.count(clicklog.id))
            .where(clicklog.short_url == short_url)
            .group_by(clicklog.country)
        ).all()
        by_country = [{"country": row[0], "clicks": row[1]} for row in by_country_query]

        # Clicks by referrer
        by_referer_query = db_session.exec(
            select(clicklog.referer, func.count(clicklog.id))
            .where(clicklog.short_url == short_url)
            .group_by(clicklog.referer)
        ).all()
        by_referer = [{"referer": row[0], "clicks": row[1]} for row in by_referer_query]

        # Total clicks
        total_clicks = db_session.exec(
            select(func.count(clicklog.id)).where(clicklog.short_url == short_url)
        ).one()

        # User premium state check
        user = db_session.get(User, user_id)
        is_premium = (user.tier in ("premium", "startup", "business")) if user else False

        by_city = []
        bot_clicks = 0
        recent_clicks = []
        if is_premium:
            # Premium: City-level Geolocation Breakdown
            by_city_query = db_session.exec(
                select(clicklog.city, func.count(clicklog.id))
                .where(clicklog.short_url == short_url)
                .group_by(clicklog.city)
                .order_by(func.count(clicklog.id).desc())
            ).all()
            by_city = [{"city": row[0], "clicks": row[1]} for row in by_city_query]

            # Premium: Bot clicks
            bot_clicks = db_session.exec(
                select(func.count(clicklog.id))
                .where(clicklog.short_url == short_url)
                .where(clicklog.is_bot == True)
            ).one()

            # Premium: 5 most recent click logs
            recent_logs = db_session.exec(
                select(clicklog)
                .where(clicklog.short_url == short_url)
                .order_by(clicklog.clicked_at.desc())
                .limit(5)
            ).all()
            for log in recent_logs:
                recent_clicks.append({
                    "clicked_at": log.clicked_at.isoformat() if log.clicked_at else None,
                    "ip_address": log.ip_address,
                    "country": log.country,
                    "city": log.city,
                    "browser": log.browser,
                    "device": log.device,
                    "referer": log.referer,
                    "is_bot": log.is_bot
                })

        return {
            "short_url": url_entry.short_url,
            "long_url": url_entry.long_url,
            "domain": url_entry.domain,
            "created_at": url_entry.created_at.isoformat() if url_entry.created_at else None,
            "total_clicks": total_clicks,
            "by_date": by_date,
            "by_browser": by_browser,
            "by_device": by_device,
            "by_country": by_country,
            "by_referer": by_referer,
            "by_city": by_city,
            "bot_clicks": bot_clicks,
            "recent_clicks": recent_clicks,
            "is_premium": is_premium
        }

# dashboard endpoint for users who have account auth required
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(user_id: Optional[int] = Depends(get_optional_user_id)):
    if not user_id:
        return RedirectResponse(url="/login")
        
    with open(os.path.join(FRONTEND_DIR, "dashboard.html"), encoding="utf-8") as f:
        return f.read()


# gives users recent clicks auth required
@app.get("/api/user/recent-clicks")
async def get_recent_clicks(limit: int = 10, user_id: int = Depends(get_required_user_id)):
    # Constrain limit to prevent database memory exhaustion
    limit = max(1, min(limit, 100))

    with Session(engine) as db_session:
        user_links_stmt = select(urldata.short_url).where(urldata.user_id == user_id)
        user_short_urls = db_session.exec(user_links_stmt).all()
        if not user_short_urls:
            return []

        stmt = (
            select(clicklog)
            .where(clicklog.short_url.in_(user_short_urls))
            .order_by(clicklog.clicked_at.desc())
            .limit(limit)
        )
        logs = db_session.exec(stmt).all()
        
        result = []
        for log in logs:
            result.append({
                "id": log.id,
                "short_url": log.short_url,
                "clicked_at": log.clicked_at.isoformat() if log.clicked_at else None,
                "ip_address": log.ip_address,
                "country": log.country,
                "browser": log.browser,
                "device": log.device,
                "referer": log.referer
            })
        return result

# --- Custom Domains Endpoints ---

@app.get("/api/domains")
async def get_user_domains(user_id: int = Depends(get_required_user_id)):

    with Session(engine) as db_session:
        tier = get_user_tier(user_id, db_session)
        is_locked = (tier == "free")
            
        statement = select(CustomDomain).where(CustomDomain.user_id == user_id).order_by(CustomDomain.created_at.desc())
        domains = db_session.exec(statement).all()
        return [
            {
                "id": dom.id,
                "domain_name": dom.domain_name,
                "is_verified": dom.is_verified,
                "created_at": dom.created_at.isoformat() if dom.created_at else None,
                "is_locked": is_locked
            }
            for dom in domains
        ]


@app.get("/api/domains/check-allowed")
async def check_allowed_domain(domain: str):
    with Session(engine) as db_session:
        dom_stmt = select(CustomDomain).where(CustomDomain.domain_name == domain)
        dom_entry = db_session.exec(dom_stmt).first()
        if dom_entry:
            return Response(status_code=200)
    return Response(status_code=400)


@app.post("/api/domains")
async def create_user_domain(req_data: CustomDomainRequest, user_id: int = Depends(get_required_user_id)):

    domain_name = req_data.domain_name.strip().lower()
    if not domain_name:
        raise HTTPException(status_code=400, detail="Domain name cannot be empty")
        
    import re
    if not re.match(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$", domain_name):
        raise HTTPException(status_code=400, detail="Invalid domain name format")

    with Session(engine) as db_session:
        user = db_session.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # Determine limits
        tier = get_user_tier(user_id, db_session)
        if tier not in ("premium", "startup", "business"):
            raise HTTPException(status_code=403, detail="Upgrade to premium to integrate custom domains")

        limit = 1 if tier == "startup" else 5

        # Check current domain count
        count_statement = select(func.count(CustomDomain.id)).where(CustomDomain.user_id == user_id)
        current_count = db_session.exec(count_statement).one()

        if current_count >= limit:
            raise HTTPException(status_code=400, detail=f"Domain limit reached ({limit} max) for your tier")

        # Check duplicate domain
        dup_statement = select(CustomDomain).where(CustomDomain.domain_name == domain_name)
        existing = db_session.exec(dup_statement).first()
        if existing:
            raise HTTPException(status_code=400, detail="This domain has already been added")

        # Register domain in Cloudflare
        cf_manager = CloudflareSaaSManager()
        cf_result = cf_manager.register_custom_domain(domain_name)
        if cf_result.get("status") == "error":
            raise HTTPException(status_code=400, detail=cf_result.get("message"))

        new_domain = CustomDomain(
            domain_name=domain_name,
            user_id=user_id,
            is_verified=False,
            created_at=datetime.now(UTC).replace(tzinfo=None),
            cloudflare_id=cf_result.get("hostname_id")
        )
        db_session.add(new_domain)
        db_session.commit()
        db_session.refresh(new_domain)

        return {
            "id": new_domain.id,
            "domain_name": new_domain.domain_name,
            "is_verified": new_domain.is_verified,
            "created_at": new_domain.created_at.isoformat() if new_domain.created_at else None,
            "cloudflare_id": new_domain.cloudflare_id
        }


@app.post("/api/domains/{domain_id}/verify")
async def verify_user_domain(domain_id: int, user_id: int = Depends(get_required_user_id)):

    import socket
    with Session(engine) as db_session:
        domain = db_session.get(CustomDomain, domain_id)
        if not domain:
            raise HTTPException(status_code=404, detail="Domain not found")
        if domain.user_id != user_id:
            raise HTTPException(status_code=403, detail="Forbidden")

        if domain.is_verified:
            return {
                "id": domain.id,
                "domain_name": domain.domain_name,
                "is_verified": True,
                "message": "Domain is already verified"
            }

        # Check DNS resolution
        is_verified = False

        # 1. Check via Cloudflare Custom Hostname API
        if domain.cloudflare_id:
            try:
                cf_manager = CloudflareSaaSManager()
                cf_status = cf_manager.get_custom_domain_status(domain.cloudflare_id)
                if cf_status.get("status") == "success" and cf_status.get("hostname_status") == "active":
                    is_verified = True
            except Exception as e:
                logger.error(f"Failed to check domain status via Cloudflare: {e}")

        # 2. Fallback to local DNS resolution check if Cloudflare check was not conclusive
        if not is_verified:
            try:
                domain_ip = socket.gethostbyname(domain.domain_name)
                
                main_ips = []
                try:
                    import httpx
                    async with httpx.AsyncClient(timeout=2.0) as client:
                        resp = await client.get("https://dns.google/resolve?name=flexurl.app")
                        if resp.status_code == 200:
                            data = resp.json()
                            answers = data.get("Answer", [])
                            main_ips = [ans["data"] for ans in answers if ans.get("type") == 1]
                except Exception:
                    pass

                if not main_ips:
                    try:
                        main_ips = [socket.gethostbyname("flexurl.app")]
                    except Exception:
                        main_ips = []

                valid_ips = set(main_ips) | {"127.0.0.1", "localhost", "testserver"}
                if domain_ip in valid_ips:
                    is_verified = True
            except Exception:
                pass

        if is_verified:
            domain.is_verified = True
            db_session.add(domain)
            db_session.commit()
            db_session.refresh(domain)
            return {
                "id": domain.id,
                "domain_name": domain.domain_name,
                "is_verified": True,
                "message": "Domain successfully verified!"
            }
        else:
            return {
                "id": domain.id,
                "domain_name": domain.domain_name,
                "is_verified": False,
                "message": "DNS records propagation pending. Please check again in a few minutes."
            }


@app.delete("/api/domains/{domain_id}")
async def delete_user_domain(domain_id: int, user_id: int = Depends(get_required_user_id)):

    with Session(engine) as db_session:
        domain = db_session.get(CustomDomain, domain_id)
        if not domain:
            raise HTTPException(status_code=404, detail="Domain not found")
        if domain.user_id != user_id:
            raise HTTPException(status_code=403, detail="Forbidden")

        if domain.cloudflare_id:
            cf_manager = CloudflareSaaSManager()
            cf_manager.remove_custom_domain(domain.cloudflare_id)

        db_session.delete(domain)
        db_session.commit()
        try:
            from utils.short_url_gen import core.redis_client
            redis_client.delete(f"dom_owner:{domain.domain_name}")
        except Exception:
            pass
        return {"status": "success", "message": "Domain removed"}


# lists all the links of the user auth required
@app.get("/api/user/links")
async def get_user_links(user_id: int = Depends(get_required_user_id)):

    with Session(engine) as db_session:
        statement = select(urldata).where(urldata.user_id == user_id).order_by(urldata.created_at.desc())
        links = db_session.exec(statement).all()

        result = []
        for link in links:
            result.append({
                "short_url": link.short_url,
                "long_url": link.long_url,
                "domain": link.domain,
                "created_at": link.created_at.isoformat() if link.created_at else None,
                "click_count": link.click_count,
                "is_banned": link.is_banned,
                "exp_time": link.exp_time.isoformat() if link.exp_time else None,
                "webhook_url": link.webhook_url,
                "ios_url": link.ios_url,
                "android_url": link.android_url,
                "fallback_url": link.fallback_url,
                "has_password": bool(link.password_hash),
                "activation_time": link.activation_time.isoformat() if link.activation_time else None,
                "custom_countdown_url": link.custom_countdown_url
            })
        return result

# export url analytics
@app.get("/api/analytics/{short_url}/export")
async def export_analytics_csv(short_url: str, user_id: int = Depends(get_required_user_id)):
    import csv
    import io
    from fastapi.responses import StreamingResponse

    with Session(engine) as db_session:
        # Check if URL exists and user owns it
        statement = select(urldata).where(urldata.short_url == short_url)
        url_entry = db_session.exec(statement).first()
        if not url_entry:
            raise HTTPException(status_code=404, detail="Short URL not found")
        if url_entry.user_id is not None and url_entry.user_id != user_id:
            raise HTTPException(status_code=403, detail="Forbidden")

        # Verify user has premium access
        tier = get_user_tier(user_id, db_session)
        if tier not in ("premium", "startup", "business"):
            raise HTTPException(status_code=403, detail="CSV export is a Premium feature")

        # Fetch click log entries sorted by click date
        statement = select(clicklog).where(clicklog.short_url == short_url).order_by(clicklog.clicked_at.desc())
        logs = db_session.exec(statement).all()

        output = io.StringIO()
        writer = csv.writer(output)
        
        # Header row
        writer.writerow([
            "Clicked At (UTC)", 
            "IP Address", 
            "Country", 
            "City", 
            "Browser", 
            "Device", 
            "Referer", 
            "Is Bot"
        ])
        
        for log in logs:
            writer.writerow([
                log.clicked_at.isoformat() if log.clicked_at else "NA",
                log.ip_address,
                log.country,
                log.city,
                log.browser,
                log.device,
                log.referer,
                "Yes" if log.is_bot else "No"
            ])
            
        output.seek(0)
        return StreamingResponse(
            io.StringIO(output.getvalue()), 
            media_type="text/csv", 
            headers={"Content-Disposition": f"attachment; filename={short_url}_analytics.csv"}
        )

@app.post("/api/user/toggle-tier")
async def toggle_user_tier(user_id: int = Depends(get_required_user_id)):
    
    with Session(engine) as db_session:
        user = db_session.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Enforce that only the admin email can manually toggle tiers
        admin_email = os.getenv("ADMIN_EMAIL")
        if not admin_email or user.email != admin_email:
            raise HTTPException(
                status_code=403, 
                detail="Only the administrator can manually toggle tiers."
            )
        
        # Toggle tier
        from datetime import datetime, UTC, timedelta
        from models.domain import Subscription
        now = datetime.now(UTC).replace(tzinfo=None)
        
        statement = select(Subscription).where(Subscription.user_id == user_id)
        sub = db_session.exec(statement).first()
        
        if user.tier == "free":
            user.tier = "startup"
            if not sub:
                sub = Subscription(
                    user_id=user_id,
                    tier="startup",
                    current_period_start=now,
                    current_period_end=now + timedelta(days=30),
                    relaxation_days_remaining=7,
                    status="active"
                )
            else:
                sub.tier = "startup"
                sub.status = "active"
                sub.current_period_end = now + timedelta(days=30)
                sub.relaxation_days_remaining = 7
        elif user.tier == "startup":
            user.tier = "business"
            if not sub:
                sub = Subscription(
                    user_id=user_id,
                    tier="business",
                    current_period_start=now,
                    current_period_end=now + timedelta(days=30),
                    relaxation_days_remaining=7,
                    status="active"
                )
            else:
                sub.tier = "business"
                sub.status = "active"
                if sub.current_period_end and sub.current_period_end > now:
                    sub.current_period_end = sub.current_period_end + timedelta(days=30)
                else:
                    sub.current_period_end = now + timedelta(days=30)
                sub.relaxation_days_remaining = 7
        else:
            user.tier = "free"
            if sub:
                sub.tier = "free"
                sub.status = "expired"
                sub.current_period_end = now
                sub.relaxation_days_remaining = 0
                
        user.plan_expires_at = sub.current_period_end if sub else None
        user.relaxation_days_remaining = sub.relaxation_days_remaining if sub else 7
        
        if sub:
            db_session.add(sub)
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
        try:
            from utils.short_url_gen import core.redis_client
            redis_client.delete(f"user_tier:{user_id}")
        except Exception:
            pass
        return {"status": "success", "tier": user.tier}


# link editing endpoint (premium only)
@app.patch("/api/links/{short_url}")
async def edit_link(short_url: str, edit_data: URLEditRequest, user_id: int = Depends(get_required_user_id)):

    from datetime import datetime, UTC
    with Session(engine) as db_session:
        user = db_session.get(User, user_id)
        if not user or user.tier not in ("premium", "startup", "business"):
            raise HTTPException(status_code=403, detail="Editing links is a premium-only feature.")

        # Get link
        statement = select(urldata).where(urldata.short_url == short_url)
        url_entry = db_session.exec(statement).first()
        if not url_entry:
            raise HTTPException(status_code=404, detail="Short URL not found")
        if url_entry.user_id != user_id:
            raise HTTPException(status_code=403, detail="Forbidden")

        # Validate long_url if changing
        if edit_data.long_url is not None:
            if not await is_valid_url(edit_data.long_url):
                raise HTTPException(status_code=400, detail="Invalid, insecure, or private long URL")
            url_entry.long_url = edit_data.long_url

        # Edit other fields
        if edit_data.webhook_url is not None:
            if edit_data.webhook_url != "":
                if not await is_valid_url(edit_data.webhook_url):
                    raise HTTPException(status_code=400, detail="Invalid, insecure, or private webhook URL")
                url_entry.webhook_url = edit_data.webhook_url
            else:
                url_entry.webhook_url = None

        if edit_data.ios_url is not None:
            if edit_data.ios_url != "":
                if not await is_valid_url(edit_data.ios_url):
                    raise HTTPException(status_code=400, detail="Invalid, insecure, or private iOS URL")
                url_entry.ios_url = edit_data.ios_url
            else:
                url_entry.ios_url = None

        if edit_data.android_url is not None:
            if edit_data.android_url != "":
                if not await is_valid_url(edit_data.android_url):
                    raise HTTPException(status_code=400, detail="Invalid, insecure, or private Android URL")
                url_entry.android_url = edit_data.android_url
            else:
                url_entry.android_url = None

        if edit_data.fallback_url is not None:
            if edit_data.fallback_url != "":
                if not await is_valid_url(edit_data.fallback_url):
                    raise HTTPException(status_code=400, detail="Invalid, insecure, or private fallback URL")
                url_entry.fallback_url = edit_data.fallback_url
            else:
                url_entry.fallback_url = None

        if edit_data.custom_countdown_url is not None:
            if edit_data.custom_countdown_url != "":
                if not await is_valid_url(edit_data.custom_countdown_url):
                    raise HTTPException(status_code=400, detail="Invalid, insecure, or private custom countdown URL")
                url_entry.custom_countdown_url = edit_data.custom_countdown_url
            else:
                url_entry.custom_countdown_url = None

        if edit_data.password is not None:
            if edit_data.password == "REMOVE":
                url_entry.password_hash = None
            elif edit_data.password != "":
                import hashlib
                salt = "flexurl_salt_secure_2026"
                url_entry.password_hash = hashlib.sha256((edit_data.password + salt).encode('utf-8')).hexdigest()

        if edit_data.activation_time is not None:
            if edit_data.activation_time != "":
                try:
                    val = edit_data.activation_time
                    if val.endswith('Z'):
                        val = val[:-1] + '+00:00'
                    dt = datetime.fromisoformat(val)
                    url_entry.activation_time = dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt
                except ValueError:
                    raise HTTPException(status_code=400, detail="Invalid activation time format")
            else:
                url_entry.activation_time = None

        if edit_data.exp_time is not None:
            if edit_data.exp_time != "":
                try:
                    val = edit_data.exp_time
                    if val.endswith('Z'):
                        val = val[:-1] + '+00:00'
                    dt = datetime.fromisoformat(val)
                    url_entry.exp_time = dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt
                except ValueError:
                    raise HTTPException(status_code=400, detail="Invalid expiration time format")
            else:
                url_entry.exp_time = None

        db_session.add(url_entry)
        db_session.commit()
        db_session.refresh(url_entry)

        # Invalidate/Update Redis cache
        try:
            from utils.short_url_gen import core.redis_client
            is_dynamic = bool(url_entry.webhook_url or url_entry.ios_url or url_entry.android_url or url_entry.password_hash or url_entry.fallback_url or url_entry.activation_time or url_entry.custom_countdown_url)
            
            is_expired = False
            if url_entry.exp_time:
                exp_utc = url_entry.exp_time.astimezone(UTC).replace(tzinfo=None) if url_entry.exp_time.tzinfo else url_entry.exp_time
                now_utc = datetime.now(UTC).replace(tzinfo=None)
                if exp_utc < now_utc:
                    is_expired = True

            redis_client.delete(short_url)
            logger.debug(f"Redis cache invalidated on edit for short_url: {short_url}")
        except Exception as e:
            logger.warning(f"Failed to invalidate Redis cache on edit: {e}")

    return {"status": "success", "message": "Short URL updated successfully"}


# link deletion endpoint
@app.delete("/api/links/{short_url}")
async def delete_link(short_url: str, user_id: int = Depends(get_required_user_id)):

    with Session(engine) as db_session:
        statement = select(urldata).where(urldata.short_url == short_url)
        url_entry = db_session.exec(statement).first()
        if not url_entry:
            raise HTTPException(status_code=404, detail="Short URL not found")
        if url_entry.user_id is not None and url_entry.user_id != user_id:
            raise HTTPException(status_code=403, detail="Forbidden")

        # Delete any associated clicklog entries first to prevent foreign key violations
        delete_logs_stmt = db_session.exec(
            select(clicklog).where(clicklog.short_url == short_url)
        ).all()
        for log in delete_logs_stmt:
            db_session.delete(log)

        db_session.delete(url_entry)
        db_session.commit()

        # Delete from Redis
        try:
            from utils.short_url_gen import core.redis_client
            redis_client.delete(short_url)
        except Exception:
            pass

        return {"status": "success", "message": "Link deleted successfully"}

# user account deletion endpoint
@app.delete("/api/user/account")
async def delete_user_account(user_id: int = Depends(get_required_user_id)):

    with Session(engine) as db_session:
        # Fetch user details
        user = db_session.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # Delete all links owned by this user
        links = db_session.exec(select(urldata).where(urldata.user_id == user_id)).all()
        for link in links:
            # Delete associated clicklogs
            clicklogs = db_session.exec(select(clicklog).where(clicklog.short_url == link.short_url)).all()
            for log in clicklogs:
                db_session.delete(log)
            db_session.delete(link)
            # Remove link from Redis
            try:
                from utils.short_url_gen import core.redis_client
                redis_client.delete(link.short_url)
            except Exception:
                pass

        # Store Firebase info before deletion
        oauth_provider = user.oauth_provider
        oauth_id = user.oauth_id

        # Delete user from local database
        db_session.delete(user)
        db_session.commit()

        # Delete from Firebase Auth if firebase UID is available
        if oauth_provider == "firebase" and oauth_id:
            try:
                from firebase_admin import api.routes.auth as firebase_auth
                firebase_auth.delete_user(oauth_id)
            except Exception as e:
                logger.warning(f"Failed to delete user from Firebase Auth: {e}")

    response = JSONResponse(content={"status": "success", "message": "Account deleted successfully"})
    response.delete_cookie(key="session_token")
    return response


@app.get("/analytics/{short_url}", response_class=HTMLResponse)
async def analytics(short_url: str, user_id: Optional[int] = Depends(get_optional_user_id)):
    if not user_id:
        return RedirectResponse(url="/login")

    with Session(engine) as db_session:
        # Check if URL exists and user owns it
        statement = select(urldata).where(urldata.short_url == short_url)
        url_entry = db_session.exec(statement).first()
        if not url_entry:
            raise HTTPException(status_code=404, detail="Short URL not found")
        if url_entry.user_id is not None and url_entry.user_id != user_id:
            raise HTTPException(status_code=403, detail="Forbidden")

        # Check if user is premium
        is_premium = get_user_tier(user_id, db_session) in ("premium", "startup", "business")

        by_date = []
        by_browser = []
        by_device = []
        by_country = []
        by_referer = []
        total_clicks = 0

        if is_premium:
            # Clicks by date
            by_date_query = db_session.exec(
                select(func.date(clicklog.clicked_at), func.count(clicklog.id))
                .where(clicklog.short_url == short_url)
                .group_by(func.date(clicklog.clicked_at))
                .order_by(func.date(clicklog.clicked_at))
            ).all()
            by_date = [{"date": str(row[0]), "clicks": row[1]} for row in by_date_query]

            # Clicks by browser
            by_browser_query = db_session.exec(
                select(clicklog.browser, func.count(clicklog.id))
                .where(clicklog.short_url == short_url)
                .group_by(clicklog.browser)
            ).all()
            by_browser = [{"browser": row[0], "clicks": row[1]} for row in by_browser_query]

            # Clicks by device
            by_device_query = db_session.exec(
                select(clicklog.device, func.count(clicklog.id))
                .where(clicklog.short_url == short_url)
                .group_by(clicklog.device)
            ).all()
            by_device = [{"device": row[0], "clicks": row[1]} for row in by_device_query]

            # Clicks by country
            by_country_query = db_session.exec(
                select(clicklog.country, func.count(clicklog.id))
                .where(clicklog.short_url == short_url)
                .group_by(clicklog.country)
            ).all()
            by_country = [{"country": row[0], "clicks": row[1]} for row in by_country_query]

            # Clicks by referrer
            by_referer_query = db_session.exec(
                select(clicklog.referer, func.count(clicklog.id))
                .where(clicklog.short_url == short_url)
                .group_by(clicklog.referer)
            ).all()
            by_referer = [{"referer": row[0], "clicks": row[1]} for row in by_referer_query]

            # Total clicks
            total_clicks = db_session.exec(
                select(func.count(clicklog.id)).where(clicklog.short_url == short_url)
            ).one()


        # User premium state check
        is_premium = get_user_tier(user_id, db_session) in ("premium", "startup", "business")

        by_city = []
        bot_clicks = 0
        recent_clicks = []
        if is_premium:
            # Premium: City-level Geolocation Breakdown
            by_city_query = db_session.exec(
                select(clicklog.city, func.count(clicklog.id))
                .where(clicklog.short_url == short_url)
                .group_by(clicklog.city)
                .order_by(func.count(clicklog.id).desc())
            ).all()
            by_city = [{"city": row[0], "clicks": row[1]} for row in by_city_query]

            # Premium: Bot clicks
            bot_clicks = db_session.exec(
                select(func.count(clicklog.id))
                .where(clicklog.short_url == short_url)
                .where(clicklog.is_bot == True)
            ).one()

            # Premium: 5 most recent click logs
            recent_logs = db_session.exec(
                select(clicklog)
                .where(clicklog.short_url == short_url)
                .order_by(clicklog.clicked_at.desc())
                .limit(5)
            ).all()
            for log in recent_logs:
                recent_clicks.append({
                    "clicked_at": log.clicked_at.isoformat() if log.clicked_at else None,
                    "ip_address": log.ip_address,
                    "country": log.country,
                    "city": log.city,
                    "browser": log.browser,
                    "device": log.device,
                    "referer": log.referer,
                    "is_bot": log.is_bot
                })

        analytics_data = {
            "short_url": url_entry.short_url,
            "long_url": url_entry.long_url,
            "created_at": url_entry.created_at.isoformat() if url_entry.created_at else None,
            "total_clicks": total_clicks,
            "by_date": by_date,
            "by_browser": by_browser,
            "by_device": by_device,
            "by_country": by_country,
            "by_referer": by_referer,
            "by_city": by_city,
            "bot_clicks": bot_clicks,
            "recent_clicks": recent_clicks,
            "is_premium": is_premium
        }

    with open(os.path.join(FRONTEND_DIR, "analytics.html"), encoding="utf-8") as f:
        html_content = f.read()

    # Inject statistics data safely preventing JSON script tag injection XSS
    json_str = json.dumps(analytics_data).replace("</", r"<\/").replace("<script", r"<\script")
    injected_js = f"window.__INITIAL_DATA__ = {json_str};"
    html_content = html_content.replace("window.__INITIAL_DATA__ = null;", injected_js)

    return HTMLResponse(content=html_content)


# main redirection endpoint accepts short urls and cheks if they exists or not and redirects them 
@app.get("/{short_url}")
async def get_short_give_long(short_url: str, request : Request):
    host = request.headers.get("host", "").lower().split(":")[0]
    is_custom_domain = not (host == "flexurl.app" or host.endswith(".flexurl.app") or host in ("localhost", "127.0.0.1", "testserver"))

    domain_user_id = None
    if is_custom_domain:
        redis_key = f"dom_owner:{host}"
        try:
            cached_val = redis_client.get(redis_key)
        except Exception:
            cached_val = None

        if cached_val is not None:
            if cached_val == "NOT_FOUND":
                raise HTTPException(status_code=404, detail="Custom domain not registered")
            domain_user_id = int(cached_val)
        else:
            with Session(engine) as db_session:
                dom_stmt = select(CustomDomain).where(CustomDomain.domain_name == host)
                dom_entry = db_session.exec(dom_stmt).first()
                if not dom_entry:
                    try:
                        redis_client.set(redis_key, "NOT_FOUND", ex=300)
                    except Exception:
                        pass
                    raise HTTPException(status_code=404, detail="Custom domain not registered")
                domain_user_id = dom_entry.user_id
                try:
                    redis_client.set(redis_key, str(domain_user_id), ex=3600)
                except Exception:
                    pass

    try:
        long_url, cached_data = serve_url(short_url)
    except Exception:
        raise HTTPException(status_code=503, detail="Service temporary unavailable")

    if long_url == "BANNED":
        logger.debug(f"Accessing banned link: {short_url}")
        content = HTML_TEMPLATES.get("banned.html", "This URL is banned.")
        return HTMLResponse(content=content, status_code=403)
        
    if long_url == "Expired":
        logger.debug(f"Accessing expired link: {short_url}")
        content = HTML_TEMPLATES.get("expired.html", "This URL has expired.")
        return HTMLResponse(content=content, status_code=410)
        
    # Premium dynamic routing fallback check
    if long_url == "DYNAMIC" or long_url is None or is_custom_domain:
        if cached_data:
            url_entry = cached_data
        else:
            with Session(engine) as db_session:
                statement = select(urldata).where(urldata.short_url == short_url)
                url_db = db_session.exec(statement).first()
                if not url_db:
                    raise HTTPException(status_code=404, detail="Short URL not found")
                
                user_tier = get_user_tier(url_db.user_id, db_session) if url_db.user_id else "free"
                url_entry = {
                    "long_url": url_db.long_url,
                    "ios_url": url_db.ios_url,
                    "android_url": url_db.android_url,
                    "fallback_url": url_db.fallback_url,
                    "activation_time": url_db.activation_time.isoformat() if url_db.activation_time else None,
                    "exp_time": url_db.exp_time.isoformat() if url_db.exp_time else None,
                    "password_hash": url_db.password_hash,
                    "domain": url_db.domain,
                    "user_id": url_db.user_id,
                    "tier": user_tier
                }

        if domain_user_id is not None and url_entry.get("user_id") != domain_user_id:
            raise HTTPException(status_code=404, detail="Short URL not found on this domain")

        if url_entry.get("domain") and url_entry.get("domain") != "flexurl.app":
            if is_custom_domain and host != url_entry.get("domain"):
                raise HTTPException(status_code=404, detail="Short URL not found on this domain")
        else:
            if is_custom_domain:
                raise HTTPException(status_code=404, detail="Short URL not found on this domain")
            
        user_tier = url_entry.get("tier", "free")
        is_premium_owned = user_tier in ("premium", "startup", "business")

        # 0. Premium Scheduled Activation check
        if is_premium_owned and url_entry.get("activation_time"):
            activation_dt = datetime.fromisoformat(url_entry.get("activation_time"))
            activation_utc = activation_dt.astimezone(UTC).replace(tzinfo=None) if activation_dt.tzinfo else activation_dt
            now_utc = datetime.now(UTC).replace(tzinfo=None)
            if now_utc < activation_utc:
                if url_entry.get("custom_countdown_url"):
                    return RedirectResponse(url_entry.get("custom_countdown_url"), status_code=302)
                else:
                    html_content = HTML_TEMPLATES.get("countdown.html", "")
                    activation_iso = activation_utc.isoformat() + "Z"
                    html_content = html_content.replace("window.__ACTIVATION_TIME__ = null;", f"window.__ACTIVATION_TIME__ = '{activation_iso}';")
                    return HTMLResponse(content=html_content)

        # 1. Expiration check with premium fallback
        is_expired = False
        if url_entry.get("exp_time"):
            exp_dt = datetime.fromisoformat(url_entry.get("exp_time"))
            exp_utc = exp_dt.astimezone(UTC).replace(tzinfo=None) if exp_dt.tzinfo else exp_dt
            now_utc = datetime.now(UTC).replace(tzinfo=None)
            if exp_utc < now_utc:
                is_expired = True
                
        if is_expired:
            if url_entry.get("fallback_url") and url_entry.get("user_id"):
                if is_premium_owned:
                    logger.debug(f"Applying fallback redirect for short_url: {short_url} -> {url_entry.get('fallback_url')}")
                    return RedirectResponse(url_entry.get("fallback_url"), status_code=302)
            
            content = HTML_TEMPLATES.get("expired.html", "This URL has expired.")
            return HTMLResponse(content=content, status_code=410)
                
        # 3. Password check
        if url_entry.get("password_hash") and url_entry.get("user_id"):
            if is_premium_owned:
                cookie_name = f"auth_link_{short_url}"
                auth_cookie = request.cookies.get(cookie_name)
                if auth_cookie != url_entry.get("password_hash"):
                    logger.debug(f"Accessing password protected link: {short_url}")
                    content = HTML_TEMPLATES.get("password_gate.html", "")
                    return HTMLResponse(content=content)
                        
        # 4. OS targeting
        target_url = url_entry.get("long_url")
        if url_entry.get("user_id"):
            if is_premium_owned:
                user_agent = request.headers.get("user-agent", "").lower()
                if "iphone" in user_agent or "ipad" in user_agent or "ipod" in user_agent:
                    if url_entry.get("ios_url"):
                        target_url = url_entry.get("ios_url")
                elif "android" in user_agent:
                    if url_entry.get("android_url"):
                        target_url = url_entry.get("android_url")
                        
        client_ip = get_client_ip(request)
        user_agent = request.headers.get("user-agent", "")
        referer = request.headers.get("referer", "Direct")
        
        event = {
            "short_url": short_url,
            "ip": client_ip,
            "user_agent": user_agent,
            "referer": referer,
            "timestamp": datetime.now(UTC).isoformat()
        }
        
        try:
            start_time = datetime.now()
            if hasattr(request.app.state, "arq_pool") and request.app.state.arq_pool:
                await request.app.state.arq_pool.enqueue_job("record_analytics", event)
                duration = (datetime.now() - start_time).total_seconds()
                logger.debug(f"Queue enqueue duration: {duration:.4f}s")
                logger.debug(f"Analytics enqueued for short_url: {short_url}")
            else:
                logger.warning("ARQ pool not initialized, skipped enqueueing.")
        except Exception as eq_err:
            logger.warning(f"Failed to enqueue analytics: {eq_err}")

        logger.debug(f"Redirect completed for short_url: {short_url} -> {target_url}")
        return RedirectResponse(target_url, status_code=302)

    if long_url:
        client_ip = get_client_ip(request)
        user_agent = request.headers.get("user-agent", "")
        referer = request.headers.get("referer", "Direct")
        
        event = {
            "short_url": short_url,
            "ip": client_ip,
            "user_agent": user_agent,
            "referer": referer,
            "timestamp": datetime.now(UTC).isoformat()
        }
        
        try:
            start_time = datetime.now()
            if hasattr(request.app.state, "arq_pool") and request.app.state.arq_pool:
                await request.app.state.arq_pool.enqueue_job("record_analytics", event)
                duration = (datetime.now() - start_time).total_seconds()
                logger.debug(f"Queue enqueue duration: {duration:.4f}s")
                logger.debug(f"Analytics enqueued for short_url: {short_url}")
            else:
                logger.warning("ARQ pool not initialized, skipped enqueueing.")
        except Exception as eq_err:
            logger.warning(f"Failed to enqueue analytics: {eq_err}")

        logger.debug(f"Redirect completed for short_url: {short_url} -> {long_url}")
        return RedirectResponse(long_url, status_code=302)

    raise HTTPException(status_code=404, detail="Short URL not found")


# main url shortening feature
@app.post("/shorten")
async def add_long_give_short(request: URLRequest, req: Request, background_tasks: BackgroundTasks, custom_alias: Optional[str] = None , exp_time: Optional[int] = None, user_id: Optional[int] = Depends(get_optional_user_id)):
    long_url = request.long_url
    
    if not await is_valid_url(long_url):
        raise HTTPException(status_code=400, detail="Invalid, insecure, or private URL")
        
    if custom_alias:
        if not is_valid_custom_alias(custom_alias):
            raise HTTPException(
                status_code=400, 
                detail="Custom alias must be 3-20 characters long, contain only letters, numbers, dashes or underscores, and cannot be a reserved system route."
            )
        from core.database import is_alias_exists
        if is_alias_exists(custom_alias):
            raise HTTPException(
                status_code=400,
                detail="Custom alias is already in use. Please choose a different one."
            )
        
    # Extract user_id if user is authenticated
    user_tier = "free"
        
    from datetime import datetime, UTC, timedelta

    if user_id:
        with Session(engine) as db_session:
            db_user = db_session.get(User, user_id)
            if db_user:
                user_tier = db_user.tier

    if user_tier not in ("premium", "startup", "business") and (request.activation_time or request.custom_countdown_url):
        raise HTTPException(
            status_code=400,
            detail="Scheduled URL activation is a premium-only feature."
        )

    computed_exp_time = calculate_link_expiration(
        user_tier=user_tier,
        is_authenticated=bool(user_id),
        requested_hours=exp_time,
    )

    # Enforce creation quotas for free/anonymous users.
    if user_tier == "free":
        # Limit checks for registered users
        if user_id:
            with Session(engine) as db_session:
                one_day_ago = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
                thirty_days_ago = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=30)
                
                daily_count = db_session.exec(
                    select(func.count(urldata.short_url))
                    .where(urldata.user_id == user_id)
                    .where(urldata.created_at >= one_day_ago)
                ).one()
                
                if daily_count >= 10:
                    raise HTTPException(
                        status_code=400,
                        detail="Daily limit reached. Free accounts are limited to 10 URLs per day."
                    )
                
                monthly_count = db_session.exec(
                    select(func.count(urldata.short_url))
                    .where(urldata.user_id == user_id)
                    .where(urldata.created_at >= thirty_days_ago)
                ).one()
                
                if monthly_count >= 100:
                    raise HTTPException(
                        status_code=400,
                        detail="Monthly limit reached. Free accounts are limited to 100 URLs per month."
                    )
        else:
            # Limit checks for anonymous guest users using Redis
            client_ip = req.client.host
            forwarded = req.headers.get("X-Forwarded-For")
            if forwarded:
                client_ip = forwarded.split(",")[0].strip()
                
            from utils.short_url_gen import core.redis_client
            redis_key = f"anon_limit:{client_ip}"
            try:
                current_count = redis_client.incr(redis_key)
                if current_count == 1:
                    redis_client.expire(redis_key, 86400) # 24 hours
                
                if current_count > 5:
                    raise HTTPException(
                        status_code=400,
                        detail="Anonymous URL creation limit reached (5 per day). Please create a free account to shorten more links."
                    )
            except Exception as e:
                if isinstance(e, HTTPException):
                    raise e
                # Fallback gracefully if Redis fails
                pass
    try:
        # Extract premium parameters if the user is premium
        is_premium_user = user_tier in ("premium", "startup", "business")
        webhook_url = request.webhook_url if is_premium_user else None
        ios_url = request.ios_url if is_premium_user else None
        android_url = request.android_url if is_premium_user else None
        fallback_url = request.fallback_url if is_premium_user else None
        
        activation_time = None
        if request.activation_time and is_premium_user:
            try:
                dt = datetime.fromisoformat(request.activation_time)
                activation_time = dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid activation time format. Must be ISO datetime string.")
                
        custom_countdown_url = request.custom_countdown_url if is_premium_user else None

        # SSRF Protection: Validate premium parameters if supplied
        if webhook_url and not await is_valid_url(webhook_url):
            raise HTTPException(status_code=400, detail="Invalid, insecure, or private webhook URL")
        if ios_url and not await is_valid_url(ios_url):
            raise HTTPException(status_code=400, detail="Invalid, insecure, or private iOS redirect URL")
        if android_url and not await is_valid_url(android_url):
            raise HTTPException(status_code=400, detail="Invalid, insecure, or private Android redirect URL")
        if fallback_url and not await is_valid_url(fallback_url):
            raise HTTPException(status_code=400, detail="Invalid, insecure, or private fallback URL")
        if custom_countdown_url and not await is_valid_url(custom_countdown_url):
            raise HTTPException(status_code=400, detail="Invalid, insecure, or private custom countdown URL")

        password_hash = None
        if request.password and is_premium_user:
            import hashlib
            salt = "flexurl_salt_secure_2026"
            password_hash = hashlib.sha256((request.password + salt).encode('utf-8')).hexdigest()

        selected_domain = None
        if request.domain and request.domain != "flexurl.app":
            if not is_premium_user:
                raise HTTPException(status_code=400, detail="Custom domain integration is a premium-only feature.")
            with Session(engine) as db_session:
                stmt = select(CustomDomain).where(CustomDomain.domain_name == request.domain).where(CustomDomain.user_id == user_id).where(CustomDomain.is_verified == True)
                dom_entry = db_session.exec(stmt).first()
                if not dom_entry:
                    raise HTTPException(status_code=400, detail="Domain is either unverified or does not belong to you.")
            selected_domain = request.domain

        if custom_alias:
            short_code = add_custom_url(
                long_url, custom_alias, user_id=user_id, exp_time=computed_exp_time,
                webhook_url=webhook_url, ios_url=ios_url, android_url=android_url,
                password_hash=password_hash, fallback_url=fallback_url,
                activation_time=activation_time, custom_countdown_url=custom_countdown_url,
                domain=selected_domain
            )
        else:
            short_code = add_url(
                long_url, user_id=user_id, exp_time=computed_exp_time,
                webhook_url=webhook_url, ios_url=ios_url, android_url=android_url,
                password_hash=password_hash, fallback_url=fallback_url,
                activation_time=activation_time, custom_countdown_url=custom_countdown_url,
                domain=selected_domain
            )
        if not short_code:
            raise HTTPException(status_code=500, detail="if this executes, the error is in short url.")
    except HTTPException as he:
        raise he
    except Exception:
         raise HTTPException(status_code=500, detail="Failed to generate short URL, Possibly the short url is already in use.")
    
    # Trigger Safe Browsing check in the background
    if short_code:
        background_tasks.add_task(background_safe_browsing_check, short_code, long_url)
    else:
        raise HTTPException(status_code=500,detail="Invalid Shorten URL.")
    # Construct the full short URL using the request base URL or the custom domain
    if selected_domain:
        full_short_url = f"https://{selected_domain}/{short_code}"
    else:
        base_url = str(req.base_url).rstrip("/")
        full_short_url = f"{base_url}/{short_code}"
    
    return {"short_url": full_short_url}


@app.post("/{short_url}")
async def post_password_gate(short_url: str, request: Request):
    # Parse URL encoded form parameters
    body = await request.body()
    from urllib.parse import parse_qs
    params = parse_qs(body.decode("utf-8"))
    submitted_pass = params.get("password", [None])[0]
    
    with Session(engine) as db_session:
        statement = select(urldata).where(urldata.short_url == short_url)
        url_entry = db_session.exec(statement).first()
        if not url_entry:
            raise HTTPException(status_code=404, detail="Short URL not found")
            
        import hashlib
        salt = "flexurl_salt_secure_2026"
        hashed = hashlib.sha256(((submitted_pass or "") + salt).encode('utf-8')).hexdigest()
        
        if hashed == url_entry.password_hash:
            response = RedirectResponse(url=f"/{short_url}", status_code=303)
            response.set_cookie(key=f"auth_link_{short_url}", value=hashed, max_age=3600, httponly=True, samesite="lax")
            return response
        else:
            return RedirectResponse(url=f"/{short_url}?error=1", status_code=303)


# Developer API credentials endpoints
@app.get("/api/developer/keys")
async def get_api_keys(user_id: int = Depends(get_required_user_id)):
    with Session(engine) as db_session:
        from models.domain import ApiKey
        statement = select(ApiKey).where(ApiKey.user_id == user_id, ApiKey.is_active == True)
        keys = db_session.exec(statement).all()
        return [
            {
                "id": k.id,
                "name": k.name,
                "created_at": k.created_at.isoformat() if k.created_at else None
            }
            for k in keys
        ]


@app.post("/api/developer/keys")
async def create_api_key(request: APIKeyCreateRequest, user_id: int = Depends(get_required_user_id)):
    import secrets
    with Session(engine) as db_session:
        tier = get_user_tier(user_id, db_session)
        if tier not in ("premium", "startup", "business"):
            raise HTTPException(status_code=403, detail="Developer API keys require a premium subscription")
            
        raw_key = f"flx_{secrets.token_hex(24)}"
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        
        from models.domain import ApiKey
        new_key = ApiKey(
            key_hash=key_hash,
            name=request.name,
            user_id=user_id
        )
        db_session.add(new_key)
        db_session.commit()
        db_session.refresh(new_key)
        
        return {
            "status": "success",
            "key_name": new_key.name,
            "raw_key": raw_key,
            "created_at": new_key.created_at.isoformat()
        }


@app.delete("/api/developer/keys/{key_id}")
async def revoke_api_key(key_id: int, user_id: int = Depends(get_required_user_id)):
    with Session(engine) as db_session:
        from models.domain import ApiKey
        key_entry = db_session.get(ApiKey, key_id)
        if not key_entry or key_entry.user_id != user_id:
            raise HTTPException(status_code=404, detail="API Key not found")
            
        key_entry.is_active = False
        db_session.add(key_entry)
        db_session.commit()
        
        from utils.short_url_gen import core.redis_client
        try:
            redis_client.delete(f"api_key:{key_entry.key_hash}")
        except Exception:
            pass
            
        return {"status": "success", "message": "API Key revoked successfully"}


# Programmatic Link Shortening endpoints
@app.post("/api/developer/links")
async def developer_shorten_link(
    request: DeveloperURLRequest,
    req: Request,
    background_tasks: BackgroundTasks,
    user_id: int = Depends(get_developer_user_id)
):
    long_url = request.long_url
    if not await is_valid_url(long_url):
        raise HTTPException(status_code=400, detail="Invalid, insecure, or private URL")
        
    custom_alias = request.custom_alias
    if custom_alias:
        if not is_valid_custom_alias(custom_alias):
            raise HTTPException(
                status_code=400, 
                detail="Custom alias must be 3-20 characters long and contain only letters, numbers, dashes, or underscores."
            )
        from core.database import is_alias_exists
        if is_alias_exists(custom_alias):
            raise HTTPException(status_code=400, detail="Custom alias is already in use")
            
    activation_time = None
    if request.activation_time:
        try:
            dt = datetime.fromisoformat(request.activation_time)
            activation_time = dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid activation time format. Must be ISO string.")
            
    for url_val in (request.webhook_url, request.ios_url, request.android_url, request.fallback_url, request.custom_countdown_url):
        if url_val and not await is_valid_url(url_val):
            raise HTTPException(status_code=400, detail="Invalid, insecure, or private URL in parameters")
            
    password_hash = None
    if request.password:
        salt = "flexurl_salt_secure_2026"
        password_hash = hashlib.sha256((request.password + salt).encode('utf-8')).hexdigest()
        
    selected_domain = None
    if request.domain and request.domain != "flexurl.app":
        with Session(engine) as db_session:
            stmt = select(CustomDomain).where(CustomDomain.domain_name == request.domain).where(CustomDomain.user_id == user_id).where(CustomDomain.is_verified == True)
            dom_entry = db_session.exec(stmt).first()
            if not dom_entry:
                raise HTTPException(status_code=400, detail="Domain is either unverified or does not belong to you.")
        selected_domain = request.domain
        
    from utils.short_url_gen import add_custom_url, add_url
    if custom_alias:
        short_code = add_custom_url(
            long_url, custom_alias, user_id=user_id, exp_time=None,
            webhook_url=request.webhook_url, ios_url=request.ios_url, android_url=request.android_url,
            password_hash=password_hash, fallback_url=request.fallback_url,
            activation_time=activation_time, custom_countdown_url=request.custom_countdown_url,
            domain=selected_domain
        )
    else:
        short_code = add_url(
            long_url, user_id=user_id, exp_time=None,
            webhook_url=request.webhook_url, ios_url=request.ios_url, android_url=request.android_url,
            password_hash=password_hash, fallback_url=request.fallback_url,
            activation_time=activation_time, custom_countdown_url=request.custom_countdown_url,
            domain=selected_domain
        )
        
    background_tasks.add_task(background_safe_browsing_check, short_code, long_url)
    
    domain_host = selected_domain if selected_domain else req.headers.get("Host", "flexurl.app")
    proto = "https" if req.headers.get("X-Forwarded-Proto") == "https" else req.url.scheme
    full_short_url = f"{proto}://{domain_host}/{short_code}"
    
    return {
        "status": "success",
        "short_url": full_short_url,
        "short_code": short_code,
        "long_url": long_url,
        "created_at": datetime.now(UTC).isoformat()
    }


@app.post("/api/developer/links/batch")
async def developer_batch_shorten(
    request: DeveloperBatchURLRequest,
    req: Request,
    background_tasks: BackgroundTasks,
    user_id: int = Depends(get_developer_user_id)
):
    if len(request.links) > 50:
        raise HTTPException(status_code=400, detail="Batch size cannot exceed 50 links per request.")
        
    results = []
    for link_req in request.links:
        try:
            long_url = link_req.long_url
            if not await is_valid_url(long_url):
                results.append({"status": "error", "long_url": long_url, "error": "Invalid, insecure, or private URL"})
                continue
                
            custom_alias = link_req.custom_alias
            if custom_alias:
                if not is_valid_custom_alias(custom_alias):
                    results.append({"status": "error", "long_url": long_url, "error": "Invalid custom alias format"})
                    continue
                from core.database import is_alias_exists
                if is_alias_exists(custom_alias):
                    results.append({"status": "error", "long_url": long_url, "error": "Custom alias is already in use"})
                    continue
                    
            activation_time = None
            if link_req.activation_time:
                try:
                    dt = datetime.fromisoformat(link_req.activation_time)
                    activation_time = dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt
                except ValueError:
                    results.append({"status": "error", "long_url": long_url, "error": "Invalid activation time format"})
                    continue
                    
            invalid_param = False
            for url_val in (link_req.webhook_url, link_req.ios_url, link_req.android_url, link_req.fallback_url, link_req.custom_countdown_url):
                if url_val and not await is_valid_url(url_val):
                    results.append({"status": "error", "long_url": long_url, "error": "Invalid URL in premium parameters"})
                    invalid_param = True
                    break
            if invalid_param:
                continue
                
            password_hash = None
            if link_req.password:
                salt = "flexurl_salt_secure_2026"
                password_hash = hashlib.sha256((link_req.password + salt).encode('utf-8')).hexdigest()
                
            selected_domain = None
            if link_req.domain and link_req.domain != "flexurl.app":
                with Session(engine) as db_session:
                    stmt = select(CustomDomain).where(CustomDomain.domain_name == link_req.domain).where(CustomDomain.user_id == user_id).where(CustomDomain.is_verified == True)
                    dom_entry = db_session.exec(stmt).first()
                    if not dom_entry:
                        results.append({"status": "error", "long_url": long_url, "error": "Domain is unverified or does not belong to you"})
                        continue
                selected_domain = link_req.domain
                
            from utils.short_url_gen import add_custom_url, add_url
            if custom_alias:
                short_code = add_custom_url(
                    long_url, custom_alias, user_id=user_id, exp_time=None,
                    webhook_url=link_req.webhook_url, ios_url=link_req.ios_url, android_url=link_req.android_url,
                    password_hash=password_hash, fallback_url=link_req.fallback_url,
                    activation_time=activation_time, custom_countdown_url=link_req.custom_countdown_url,
                    domain=selected_domain
                )
            else:
                short_code = add_url(
                    long_url, user_id=user_id, exp_time=None,
                    webhook_url=link_req.webhook_url, ios_url=link_req.ios_url, android_url=link_req.android_url,
                    password_hash=password_hash, fallback_url=link_req.fallback_url,
                    activation_time=activation_time, custom_countdown_url=link_req.custom_countdown_url,
                    domain=selected_domain
                )
                
            background_tasks.add_task(background_safe_browsing_check, short_code, long_url)
            
            domain_host = selected_domain if selected_domain else req.headers.get("Host", "flexurl.app")
            proto = "https" if req.headers.get("X-Forwarded-Proto") == "https" else req.url.scheme
            full_short_url = f"{proto}://{domain_host}/{short_code}"
            
            results.append({
                "status": "success",
                "short_url": full_short_url,
                "short_code": short_code,
                "long_url": long_url,
                "created_at": datetime.now(UTC).isoformat()
            })
        except Exception as err:
            results.append({"status": "error", "long_url": link_req.long_url, "error": str(err)})
            
    return {"results": results}
