from sonyflake import SonyFlake
from datetime import datetime , UTC, timedelta
from utils.base62 import encode_base62
from models.domain import urldata
from core.database import add_to_db, get_long_url, is_long_url_exists, mark_url_banned, is_alias_exists
from core.logger import core.logger
# from utils.validations import check_safe_browsing
from core.redis_client import core.redis_client




custom_epoch = datetime(2014, 9, 1, 0, 0, 0, tzinfo=UTC)

generator =  SonyFlake(custom_epoch)



def get_short_url() -> str:
    unique_id = get_unique_id()
    short_url = encode_base62(unique_id)
    return short_url

def get_unique_id() -> str:
    return generator.next_id()


from typing import Optional

def add_url(
    long_url : str, 
    user_id: Optional[int] = None, 
    exp_time : Optional[int | datetime] = None,
    webhook_url: Optional[str] = None,
    ios_url: Optional[str] = None,
    android_url: Optional[str] = None,
    password_hash: Optional[str] = None,
    fallback_url: Optional[str] = None,
    activation_time: Optional[datetime] = None,
    custom_countdown_url: Optional[str] = None,
    domain: Optional[str] = None
):
    db_exp_time = None
    if isinstance(exp_time, datetime):
        db_exp_time = exp_time.astimezone(UTC).replace(tzinfo=None) if exp_time.tzinfo else exp_time
    elif isinstance(exp_time, int):
        db_exp_time = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=exp_time)
    exists = is_long_url_exists(
        long_url,
        user_id=user_id,
        domain=domain,
        adopted_exp_time=db_exp_time,
    )
    if exists:
        if user_id:
            try:
                redis_client.delete(exists)
            except Exception as re:
                logger.warning(f"Redis is Offline falling back to Database: {re}")
        return exists
    short_url = get_short_url()
    url = urldata(
        short_url=short_url,
        long_url=long_url,
        created_at=datetime.now(UTC).replace(tzinfo=None),
        click_count=0,
        user_id=user_id,
        exp_time=db_exp_time,
        webhook_url=webhook_url,
        ios_url=ios_url,
        android_url=android_url,
        password_hash=password_hash,
        fallback_url=fallback_url,
        activation_time=activation_time,
        custom_countdown_url=custom_countdown_url,
        domain=domain
    )
    
    # Calculate correct Redis cache TTL
    redis_ttl = 3600
    is_expired = False
    if db_exp_time:
        now_naive = datetime.now(UTC).replace(tzinfo=None)
        seconds_left = int((db_exp_time - now_naive).total_seconds())
        if seconds_left <= 0:
            is_expired = True
        else:
            redis_ttl = min(3600, seconds_left)

    is_dynamic = bool(webhook_url or ios_url or android_url or password_hash or fallback_url or activation_time or custom_countdown_url or domain)

    try:
        if is_expired:
            redis_client.set(short_url, "Expired", ex=3600)
        elif is_dynamic:
            redis_client.delete(short_url)
        else:
            redis_client.set(short_url, long_url, ex=redis_ttl)
    except Exception as re:
        logger.warning(f"Redis is Offline falling back to Database: {re}")
    add_to_db(url)
    return short_url


def add_custom_url(
    long_url, 
    custom_alias, 
    user_id: Optional[int] = None, 
    exp_time : Optional[int | datetime] = None,
    webhook_url: Optional[str] = None,
    ios_url: Optional[str] = None,
    android_url: Optional[str] = None,
    password_hash: Optional[str] = None,
    fallback_url: Optional[str] = None,
    activation_time: Optional[datetime] = None,
    custom_countdown_url: Optional[str] = None,
    domain: Optional[str] = None
):
    does_exists = is_alias_exists(custom_alias)
    if does_exists:
        return None
    else:
        db_exp_time = None
        if isinstance(exp_time, datetime):
            db_exp_time = exp_time.astimezone(UTC).replace(tzinfo=None) if exp_time.tzinfo else exp_time
        elif isinstance(exp_time, int):
            db_exp_time = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=exp_time)
        url = urldata(
            short_url=custom_alias,
            long_url=long_url,
            created_at=datetime.now(UTC).replace(tzinfo=None),
            click_count=0,
            user_id=user_id,
            exp_time=db_exp_time,
            webhook_url=webhook_url,
            ios_url=ios_url,
            android_url=android_url,
            password_hash=password_hash,
            fallback_url=fallback_url,
            activation_time=activation_time,
            custom_countdown_url=custom_countdown_url,
            domain=domain
        )
        
        # Calculate correct Redis cache TTL
        redis_ttl = 3600
        is_expired = False
        if db_exp_time:
            now_naive = datetime.now(UTC).replace(tzinfo=None)
            seconds_left = int((db_exp_time - now_naive).total_seconds())
            if seconds_left <= 0:
                is_expired = True
            else:
                redis_ttl = min(3600, seconds_left)

        is_dynamic = bool(webhook_url or ios_url or android_url or password_hash or fallback_url or activation_time or custom_countdown_url or domain)

        try:
            if is_expired:
                redis_client.set(custom_alias, "Expired", ex=3600)
            elif is_dynamic:
                redis_client.delete(custom_alias)
            else:
                redis_client.set(custom_alias, long_url, ex=redis_ttl)
        except Exception as re:
            logger.warning(f"Redis is Offline falling back to Database: {re}")
        add_to_db(url)
        return custom_alias


def ban_in_cache(short_url: str):
    redis_client.set(short_url, "BANNED", ex=3600)

import json
from sqlmodel import Session, select
from core.database import engine
from models.domain import urldata, Subscription, User

def get_user_tier(user_id: int, db_session: Session) -> str:
    redis_key = f"user_tier:{user_id}"
    try:
        cached_tier = redis_client.get(redis_key)
        if cached_tier:
            return cached_tier
    except Exception:
        pass

    statement = select(Subscription).where(Subscription.user_id == user_id)
    sub = db_session.exec(statement).first()

    if not sub:
        user = db_session.get(User, user_id)
        if not user:
            return "free"
        
        expires_at = user.plan_expires_at
        status = "expired"
        tier = user.tier
        
        if not expires_at:
            if user.tier in ("premium", "startup", "business"):
                expires_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30)
                status = "active"
            else:
                expires_at = datetime.now(UTC).replace(tzinfo=None)
                status = "expired"
                tier = "free"
        else:
            status = "active" if expires_at > datetime.now(UTC).replace(tzinfo=None) else "expired"
            
        sub = Subscription(
            user_id=user.id,
            tier=tier,
            current_period_start=user.created_at or datetime.now(UTC).replace(tzinfo=None),
            current_period_end=expires_at,
            relaxation_days_remaining=user.relaxation_days_remaining or 7,
            status=status
        )
        try:
            db_session.add(sub)
            db_session.commit()
            db_session.refresh(sub)
        except Exception:
            pass

    if sub.tier == "free":
        return "free"

    plan_expires = sub.current_period_end.replace(tzinfo=None) if sub.current_period_end.tzinfo else sub.current_period_end
    
    tier = sub.tier
    if datetime.now(UTC).replace(tzinfo=None) >= plan_expires:
        days_since_expiry = (datetime.now(UTC).replace(tzinfo=None) - plan_expires).days
        relaxation_remaining = sub.relaxation_days_remaining - days_since_expiry
        if relaxation_remaining <= 0 and sub.status != "expired":
            try:
                sub.status = "expired"
                user = db_session.get(User, user_id)
                if user:
                    user.tier = "free"
                    db_session.add(user)
                db_session.add(sub)
                db_session.commit()
            except Exception:
                pass
            tier = "free"

    try:
        redis_client.set(redis_key, tier, ex=3600)
    except Exception:
        pass

    return tier

def serve_url(short_url : str):
    cached = None
    try:
        start_time = datetime.now()
        cached = redis_client.get(short_url)
        redis_duration = (datetime.now() - start_time).total_seconds()
        logger.debug(f"Redis lookup duration for {short_url}: {redis_duration:.4f}s")
    except Exception as re:
        logger.warning(f"Redis Offline, falling back to database: {re}")
        
    if cached:
        logger.debug(f"Redirect cache hit for short_url: {short_url}")
        if cached.startswith("{") and cached.endswith("}"):
            try:
                data = json.loads(cached)
                return data.get("long_url"), data
            except Exception:
                pass
        return cached, None

    logger.debug(f"Redirect cache miss for short_url: {short_url}")
    logger.debug(f"Database lookup for short_url: {short_url}")
    
    db_start = datetime.now()
    try:
        with Session(engine) as session:
            statement = select(urldata).where(urldata.short_url == short_url)
            url = session.exec(statement).first()
            db_duration = (datetime.now() - db_start).total_seconds() * 1000
            logger.debug(f"DB query (url lookup) duration: {db_duration:.2f} ms")
    except Exception as e:
        logger.error(f"PostgreSQL lookup transaction failure for {short_url}: {e}")
        raise e
        
    if url is None:
        logger.info(f"URL resolution requested but code does not exist: {short_url}")
        return None, None
        
    is_expired = False
    if url.exp_time:
        exp_utc = url.exp_time.astimezone(UTC).replace(tzinfo=None) if url.exp_time.tzinfo else url.exp_time
        now_utc = datetime.now(UTC).replace(tzinfo=None)
        if exp_utc < now_utc:
            is_expired = True
            
    if is_expired:
        logger.debug(f"Accessing expired link: {short_url}")
        try:
            redis_client.set(short_url, "Expired", ex=3600)
        except Exception as re:
            logger.warning(f"Redis Offline: {re}")
        return "Expired", None
        
    if url.is_banned:
        logger.debug(f"Accessing banned link: {short_url}")
        try:
            redis_client.set(short_url, "BANNED", ex=3600)
        except Exception as re:
            logger.warning(f"Redis Offline: {re}")
        return "BANNED", None
        
    is_dynamic = bool(url.webhook_url or url.ios_url or url.android_url or url.password_hash or url.fallback_url or url.activation_time or url.custom_countdown_url or url.domain)
    
    redis_ttl = 3600
    if url.exp_time:
        exp_utc = url.exp_time.astimezone(UTC).replace(tzinfo=None) if url.exp_time.tzinfo else url.exp_time
        now_utc = datetime.now(UTC).replace(tzinfo=None)
        seconds_left = int((exp_utc - now_utc).total_seconds())
        if seconds_left > 0:
            redis_ttl = min(3600, seconds_left)
            
    if is_dynamic:
        logger.debug(f"Processing dynamic routing rules for short_url: {short_url}")
        with Session(engine) as session:
            tier = get_user_tier(url.user_id, session) if url.user_id else "free"
        cached_fields = {
            "long_url": url.long_url,
            "ios_url": url.ios_url,
            "android_url": url.android_url,
            "fallback_url": url.fallback_url,
            "activation_time": url.activation_time.isoformat() if url.activation_time else None,
            "exp_time": url.exp_time.isoformat() if url.exp_time else None,
            "password_hash": url.password_hash,
            "domain": url.domain,
            "user_id": url.user_id,
            "tier": tier
        }
        try:
            redis_client.set(short_url, json.dumps(cached_fields), ex=3600)
        except Exception as re:
            logger.warning(f"Redis Offline: {re}")
        return "DYNAMIC", cached_fields

    try:
        redis_client.set(short_url, url.long_url, ex=redis_ttl)
    except Exception as re:
        logger.warning(f"Redis Offline: {re}")
        
    return url.long_url, None


