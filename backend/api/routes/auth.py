from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, JSONResponse
import jwt
import time
from core.database import get_user_by_email, create_user, engine
from models.domain import User
from sqlmodel import Session, select
import os
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, auth
from pydantic import BaseModel

load_dotenv()
router = APIRouter()

JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
FIREBASE_ADMIN_SDK_JSON = os.getenv("FIREBASE_ADMIN_SDK_JSON")

from core.logger import logger

# Initialize Firebase Admin SDK if not already initialized
if not firebase_admin._apps:
    try:
        cred_path = FIREBASE_ADMIN_SDK_JSON
        if not os.path.isabs(cred_path):
            # auth.py is in backend/api/routes, so root is 3 levels up
            parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
            cred_path = os.path.join(parent_dir, cred_path)
            
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred)
        logger.info("Firebase Admin SDK initialized successfully.")
    except Exception as e:
        logger.error(f"Error initializing Firebase Admin: {e}")


class SessionRequest(BaseModel):
    idToken: str


@router.get("/auth/check-username")
async def check_username(username: str):
    username = username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username cannot be empty")
    with Session(engine) as session:
        stmt = select(User).where(User.full_name == username)
        existing = session.exec(stmt).first()
        if existing:
            return {"available": False}
        return {"available": True}


@router.post("/auth/session")
async def create_session(data: SessionRequest):
    id_token = data.idToken
    try:
        # Verify the ID token sent by the client
        decoded_token = auth.verify_id_token(id_token)
        uid = decoded_token.get("uid")
        email = decoded_token.get("email")
        full_name = decoded_token.get("name", "User")
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid Firebase ID token: {str(e)}")

    if not email:
        raise HTTPException(status_code=400, detail="Email is required but not provided by Firebase account.")

    # Find or create user in local Postgres DB
    user = get_user_by_email(email)
    if not user:
        # Check if username (full_name) is taken
        with Session(engine) as session:
            stmt = select(User).where(User.full_name == full_name)
            existing_username = session.exec(stmt).first()
            if existing_username:
                provider = decoded_token.get("firebase", {}).get("sign_in_provider")
                if provider == "google.com":
                    import random
                    full_name = f"{full_name}{random.randint(100, 999)}"
                else:
                    raise HTTPException(status_code=400, detail="This username is already taken. Please choose another one.")

        user = create_user(
            email=email,
            full_name=full_name,
            oauth_provider="firebase",
            oauth_id=uid
        )
    elif user.oauth_provider != "firebase":
        # Link account to Firebase provider
        with Session(engine) as session:
            db_user = session.get(User, user.id)
            if db_user:
                db_user.oauth_provider = "firebase"
                db_user.oauth_id = uid
                session.add(db_user)
                session.commit()
                session.refresh(db_user)
                user = db_user

    # Generate session JWT
    payload = {
        "user_id" : user.id,
        "email" : user.email,
        "exp": time.time() + 604800 # 7 days
    }
    session_token = jwt.encode(payload, JWT_SECRET_KEY, algorithm="HS256")

    # Set JWT in httpOnly cookie
    json_response = JSONResponse(content={
        "logged_in": True,
        "user": {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name
        }
    })
    json_response.set_cookie(
        key="session_token",
        value=session_token,
        httponly=True,
        max_age=604800,
        samesite="lax",
        secure=False
    )
    return json_response


@router.get("/auth/logout")
async def logout():
    response = RedirectResponse(url="/")
    response.delete_cookie(key="session_token")
    return response


def get_subscription_details(user: User) -> dict:
    """
    Computes effective tier and subscription warnings dynamically.
    Heals/seeds the subscription table from legacy User fields if missing.
    """
    from sqlmodel import Session, select
    from core.database import engine
    from models.domain import Subscription
    from datetime import datetime, UTC
    
    now = datetime.now(UTC).replace(tzinfo=None)
    
    with Session(engine) as session:
        statement = select(Subscription).where(Subscription.user_id == user.id)
        sub = session.exec(statement).first()
        
        # Self-healing migration
        if not sub:
            from datetime import timedelta
            expires_at = user.plan_expires_at
            status = "expired"
            tier = user.tier
            
            if not expires_at:
                if user.tier in ("premium", "startup", "business"):
                    expires_at = now + timedelta(days=30)
                    status = "active"
                else:
                    expires_at = now
                    status = "expired"
                    tier = "free"
            else:
                status = "active" if expires_at > now else "expired"
                
            sub = Subscription(
                user_id=user.id,
                tier=tier,
                current_period_start=user.created_at or now,
                current_period_end=expires_at,
                relaxation_days_remaining=user.relaxation_days_remaining or 7,
                status=status
            )
            session.add(sub)
            session.commit()
            session.refresh(sub)
            
        if sub.tier == "free":
            return {
                "status": "none",
                "effective_tier": "free",
                "days_remaining": 0,
                "relaxation_remaining": 0,
                "plan_expires_at": None,
                "is_trial": False,
                "has_used_trial": user.has_used_trial
            }

        plan_expires_at = sub.current_period_end.replace(tzinfo=None) if sub.current_period_end.tzinfo else sub.current_period_end

        if now < plan_expires_at:
            delta = plan_expires_at - now
            days_remaining = int(delta.total_seconds() / 86400) + (1 if delta.total_seconds() % 86400 > 0 else 0)
            status = "expiring_soon" if days_remaining <= 5 else "active"
            return {
                "status": status,
                "effective_tier": sub.tier,
                "days_remaining": max(0, days_remaining),
                "relaxation_remaining": sub.relaxation_days_remaining,
                "plan_expires_at": plan_expires_at.isoformat(),
                "is_trial": sub.is_trial,
                "has_used_trial": user.has_used_trial
            }
        else:
            delta_expiry = now - plan_expires_at
            days_since_expiry = int(delta_expiry.total_seconds() / 86400)
            relaxation_remaining = max(0, sub.relaxation_days_remaining - days_since_expiry)
            
            if relaxation_remaining > 0:
                return {
                    "status": "relaxation",
                    "effective_tier": "free",
                    "days_remaining": 0,
                    "relaxation_remaining": relaxation_remaining,
                    "plan_expires_at": plan_expires_at.isoformat(),
                    "is_trial": sub.is_trial,
                    "has_used_trial": user.has_used_trial
                }
            else:
                return {
                    "status": "expired",
                    "effective_tier": "free",
                    "days_remaining": 0,
                    "relaxation_remaining": 0,
                    "plan_expires_at": plan_expires_at.isoformat(),
                    "is_trial": sub.is_trial,
                    "has_used_trial": user.has_used_trial
                }


@router.get("/auth/me")
async def get_me(request: Request):
    """
    Check if user is authenticated.
    """
    token = request.cookies.get("session_token")
    if not token:
         return {"logged_in": False, "country": request.headers.get("CF-IPCountry", "IN")}
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=["HS256"])
        email = payload.get("email")

        user = get_user_by_email(email)
        if not user:
            return {"logged_in": False, "country": request.headers.get("CF-IPCountry", "IN")}
        
        sub_details = get_subscription_details(user)
        return {
            "logged_in": True,
            "user" : {
                "id" : user.id,
                "email" : user.email,
                "full_name" : user.full_name,
                "tier": sub_details["effective_tier"],
                "created_at": user.created_at.isoformat() if user.created_at else None,
                "subscription_status": sub_details["status"],
                "days_remaining": sub_details["days_remaining"],
                "relaxation_remaining": sub_details["relaxation_remaining"],
                "plan_expires_at": sub_details["plan_expires_at"],
                "is_trial": sub_details.get("is_trial", False),
                "has_used_trial": sub_details.get("has_used_trial", False),
                "country": request.headers.get("CF-IPCountry", "IN")
            }
        }
    except jwt.PyJWTError:
        return {"logged_in": False, "country": request.headers.get("CF-IPCountry", "IN")}