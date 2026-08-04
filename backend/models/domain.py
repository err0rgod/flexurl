from sqlmodel import SQLModel, Field, Field as SQLField
from typing import Optional
from datetime import datetime, UTC



class User(SQLModel, table=True):
    """
    Represents a registered user in the FlexURL platform.
    Integrates with both local database session management and Firebase Auth records.
    """
    __tablename__ = "users"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    full_name: str  # Stores display name / chosen unique username
    oauth_provider: str  # Authentication provider (e.g., 'firebase', 'google')
    oauth_id: str  # Remote UID from the OAuth provider (e.g., Firebase UID)
    created_at: datetime
    tier: str = Field(default="free")  # Account tier: 'free' or 'premium'
    plan_expires_at: Optional[datetime] = Field(default=None, nullable=True)
    relaxation_days_remaining: int = Field(default=7, nullable=False)
    has_used_trial: bool = Field(default=False)

class urldata(SQLModel, table=True):
    """
    Stores individual shortened URLs, click counts, status, ownership, 
    and custom expiration times.
    """
    __tablename__ = "urldata"

    short_url: str = Field(primary_key=True, index=True, nullable=False, max_length=20, unique=True)  # SonyFlake base62 or custom alias
    long_url: str = Field(nullable=False)  # Destination URL
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC).replace(tzinfo=None))
    click_count: int = Field(default=0)
    is_banned: bool = Field(default=False)  # Flagged by Google Safe Browsing API check
    user_id: Optional[int] = Field(default=None, foreign_key="users.id")  # Owner identifier
    exp_time: Optional[datetime] = Field(default=None)  # Auto-expiration datetime
    webhook_url: Optional[str] = Field(default=None)  # Premium: destination for redirection payloads
    ios_url: Optional[str] = Field(default=None)  # Premium: targeted redirect destination for iOS users
    android_url: Optional[str] = Field(default=None)  # Premium: targeted redirect destination for Android users
    password_hash: Optional[str] = Field(default=None)  # Premium: secure link access password hash
    fallback_url: Optional[str] = Field(default=None)  # Premium: fallback destination URL after expiration
    activation_time: Optional[datetime] = Field(default=None)  # Premium: scheduled activation time
    custom_countdown_url: Optional[str] = Field(default=None)  # Premium: custom countdown destination URL
    domain: Optional[str] = Field(default=None, nullable=True)  # Premium: selected custom domain for link

class clicklog(SQLModel, table=True):
    """
    Logs visitor redirection events for real-time analytics reports.
    Contains user-agent parser metrics (IP, country, browser, device, referer).
    """
    __tablename__ = "clicklog"

    id: int = Field(primary_key=True, nullable=False, index=True)
    short_url: str = Field(nullable=False, foreign_key="urldata.short_url")  # Reference to shortened link
    clicked_at: datetime = Field(nullable=False, default_factory=lambda: datetime.now(UTC).replace(tzinfo=None))
    ip_address: str = Field(nullable=False, default="NA")
    country: str = Field(nullable=False, default="NA")
    city: str = Field(nullable=False, default="NA")  # Premium: visitor city name
    browser: str = Field(nullable=False, default="NA")
    device: str = Field(nullable=False, default="NA")
    referer: str = Field(nullable=False, default="NA")
    is_bot: bool = Field(default=False)  # Premium: flag to filter crawler traffic


class CustomDomain(SQLModel, table=True):
    """
    Stores custom domains integrated by premium users for branded redirections.
    """
    __tablename__ = "custom_domains"

    id: Optional[int] = Field(default=None, primary_key=True)
    domain_name: str = Field(unique=True, index=True, nullable=False)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC).replace(tzinfo=None))
    user_id: int = Field(foreign_key="users.id", nullable=False)
    is_verified: bool = Field(default=False)
    cloudflare_id: Optional[str] = Field(default=None, nullable=True)


from pydantic import BaseModel, Field

class URLRequest(BaseModel):
    long_url: str = Field(..., max_length=2048)
    webhook_url: Optional[str] = Field(None, max_length=2048)
    ios_url: Optional[str] = Field(None, max_length=2048)
    android_url: Optional[str] = Field(None, max_length=2048)
    password: Optional[str] = Field(None, max_length=255)
    fallback_url: Optional[str] = Field(None, max_length=2048)
    activation_time: Optional[str] = Field(None)
    custom_countdown_url: Optional[str] = Field(None, max_length=2048)
    domain: Optional[str] = Field(None, max_length=255)


class URLEditRequest(BaseModel):
    long_url: Optional[str] = Field(None, max_length=2048)
    webhook_url: Optional[str] = Field(None, max_length=2048)
    ios_url: Optional[str] = Field(None, max_length=2048)
    android_url: Optional[str] = Field(None, max_length=2048)
    password: Optional[str] = Field(None, max_length=255)
    fallback_url: Optional[str] = Field(None, max_length=2048)
    activation_time: Optional[str] = Field(None)
    custom_countdown_url: Optional[str] = Field(None, max_length=2048)
    exp_time: Optional[str] = Field(None)


class Subscription(SQLModel, table=True):
    """
    Decoupled table tracking user subscription cycles, billing status,
    and automatic dunning/email-state stages.
    """
    __tablename__ = "subscriptions"
    
    id: Optional[int] = SQLField(default=None, primary_key=True)
    user_id: int = SQLField(foreign_key="users.id", index=True, unique=True, ondelete="CASCADE")
    tier: str = SQLField(default="free") # free, startup, business
    status: str = SQLField(default="active") # active, relaxation, expired
    current_period_start: datetime = SQLField(default_factory=datetime.utcnow)
    current_period_end: datetime
    relaxation_days_remaining: int = SQLField(default=7, nullable=False)
    
    dunning_warn_sent: bool = SQLField(default=False)
    dunning_expired_sent: bool = SQLField(default=False)
    dunning_ended_sent: bool = SQLField(default=False)
    is_trial: bool = SQLField(default=False)


class QuoteRequest(BaseModel):
    business_name: str = Field(..., max_length=255)
    primary_contact: str = Field(..., max_length=255)
    alternate_contact: Optional[str] = Field(None, max_length=255)
    cloud_provider: Optional[str] = Field(None, max_length=50)
    demand_desc: str = Field(..., max_length=5000)


class SupportTicketRequest(BaseModel):
    name: str = Field(..., max_length=255)
    email: str = Field(..., max_length=255)
    subject: str = Field(..., max_length=255)
    message: str = Field(..., max_length=5000)


class PaymentOrderRequest(BaseModel):
    plan: str


class PaymentVerifyRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class CustomDomainRequest(BaseModel):
    domain_name: str


class ApiKey(SQLModel, table=True):
    """
    Stores hashed developer API keys for programmatic access.
    """
    __tablename__ = "api_keys"
    
    id: Optional[int] = SQLField(default=None, primary_key=True)
    key_hash: str = SQLField(index=True, unique=True, nullable=False)
    name: str = SQLField(max_length=255, nullable=False)
    user_id: int = SQLField(foreign_key="users.id", nullable=False)
    created_at: datetime = SQLField(default_factory=lambda: datetime.now(UTC).replace(tzinfo=None))
    is_active: bool = SQLField(default=True)


class APIKeyCreateRequest(BaseModel):
    name: str = Field(..., max_length=255)


class DeveloperURLRequest(BaseModel):
    long_url: str = Field(..., max_length=2048)
    custom_alias: Optional[str] = Field(None, max_length=20)
    webhook_url: Optional[str] = Field(None, max_length=2048)
    ios_url: Optional[str] = Field(None, max_length=2048)
    android_url: Optional[str] = Field(None, max_length=2048)
    password: Optional[str] = Field(None, max_length=255)
    fallback_url: Optional[str] = Field(None, max_length=2048)
    activation_time: Optional[str] = Field(None)
    custom_countdown_url: Optional[str] = Field(None, max_length=2048)
    domain: Optional[str] = Field(None, max_length=255)


class DeveloperBatchURLRequest(BaseModel):
    links: list[DeveloperURLRequest]