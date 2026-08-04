import os 
from dotenv import load_dotenv
from sqlmodel import create_engine,SQLModel, Session, select
from typing import Optional
from models.domain import urldata, User, clicklog, CustomDomain, ApiKey
from datetime import datetime, UTC


load_dotenv()

DB_PATH = os.getenv("DB_PATH")

engine = create_engine(
    DB_PATH,
    pool_size=20,
    max_overflow=10,
    pool_recycle=1800,
    pool_pre_ping=True
)

def init_db():
    """
    Initializes the PostgreSQL database schema by creating all tables defined 
    in models.py (User, urldata, clicklog) if they do not already exist.
    """
    try:
        SQLModel.metadata.create_all(engine)
    except Exception:
        pass
    # Drop the unique constraint on long_url if it exists to allow duplicate long_urls (e.g. for custom aliases/premium target configurations)
    from sqlalchemy import text
    with Session(engine) as session:
        try:
            session.exec(text("ALTER TABLE urldata DROP CONSTRAINT IF EXISTS urldata_long_url_key;"))
            session.exec(text("ALTER TABLE urldata ADD COLUMN IF NOT EXISTS activation_time TIMESTAMP WITHOUT TIME ZONE;"))
            session.exec(text("ALTER TABLE urldata ADD COLUMN IF NOT EXISTS custom_countdown_url VARCHAR;"))
            session.exec(text("ALTER TABLE urldata ADD COLUMN IF NOT EXISTS domain VARCHAR;"))
            session.exec(text("ALTER TABLE custom_domains ADD COLUMN IF NOT EXISTS cloudflare_id VARCHAR;"))
            session.exec(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS plan_expires_at TIMESTAMP WITHOUT TIME ZONE;"))
            session.exec(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS relaxation_days_remaining INTEGER DEFAULT 7;"))
            session.exec(text("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                    tier VARCHAR DEFAULT 'free',
                    status VARCHAR DEFAULT 'active',
                    current_period_start TIMESTAMP WITHOUT TIME ZONE,
                    current_period_end TIMESTAMP WITHOUT TIME ZONE,
                    relaxation_days_remaining INTEGER DEFAULT 7,
                    dunning_warn_sent BOOLEAN DEFAULT FALSE,
                    dunning_expired_sent BOOLEAN DEFAULT FALSE,
                    dunning_ended_sent BOOLEAN DEFAULT FALSE
                );
            """))
            session.exec(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS has_used_trial BOOLEAN DEFAULT FALSE;"))
            session.exec(text("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS is_trial BOOLEAN DEFAULT FALSE;"))
            session.commit()
        except Exception:
            pass


def add_to_db(data: urldata):
    """
    Persists a new urldata shortened link record to the PostgreSQL database.
    """
    with Session(engine) as session:
        session.add(data)
        session.commit()


def is_alias_exists(custom_alias: str) -> bool:
    """
    Queries the database to check if a specific custom short URL code / alias 
    is already claimed.
    
    Args:
        custom_alias (str): The short code alias to check.
        
    Returns:
        bool: True if alias is already in use, False otherwise.
    """
    with Session(engine) as session:
        statement = select(urldata).where(urldata.short_url == custom_alias)
        url = session.exec(statement).first()
        return True if url else False


def get_long_url(short_url: str) -> Optional[str]:
    """
    Resolves a short URL code to its destination (long URL) and logs a click.
    Also handles link expiration and safety ban statuses.
    
    Args:
        short_url (str): The short code to resolve.
        
    Returns:
        Optional[str]: The resolved destination URL, "Expired", "BANNED", or None.
    """
    with Session(engine) as session:
        statement = select(urldata).where(urldata.short_url == short_url)
        url = session.exec(statement).first()
        if url is None:
            return None
        if url.exp_time:
            exp_utc = url.exp_time.astimezone(UTC).replace(tzinfo=None) if url.exp_time.tzinfo else url.exp_time
            now_utc = datetime.now(UTC).replace(tzinfo=None)
            if exp_utc < now_utc:
                return "Expired"
        if url.is_banned:
            return "BANNED"
            
        # Increment redirection count
        url.click_count += 1
        session.add(url)
        session.commit()
        session.refresh(url)
        return url.long_url


def mark_url_banned(short_url: str):
    """
    Flags a shortened URL as banned in the database.
    Banned links are replaced with safety landing pages upon access.
    """
    with Session(engine) as session:
        statement = select(urldata).where(urldata.short_url == short_url)
        url = session.exec(statement).first()
        if url:
            url.is_banned = True
            session.add(url)
            session.commit()
    

def is_long_url_exists(
    long_url: str,
    user_id: Optional[int] = None,
    domain: Optional[str] = None,
    adopted_exp_time: Optional[datetime] = None,
) -> Optional[str]:
    """
    Checks if a long destination URL has already been shortened.
    If the link exists and is anonymous, optionally associates it with the authenticated user.
    
    Args:
        long_url (str): The destination URL.
        user_id (Optional[int]): The ID of the authenticated user to associate.
        domain (Optional[str]): The custom domain chosen.
        adopted_exp_time (Optional[datetime]): Expiration to apply when an anonymous link is adopted.
        
    Returns:
        Optional[str]: The short URL code if exists, None otherwise.
    """
    with Session(engine) as session:
        # Check if this user has already shortened this destination URL
        statement = select(urldata).where(urldata.long_url == long_url).where(urldata.user_id == user_id).where(urldata.domain == domain)
        results = session.exec(statement).first()
        if results is not None:
            return results.short_url
            
        # If logged in, check if there is an anonymous link we can adopt
        if user_id and domain is None:
            statement = select(urldata).where(urldata.long_url == long_url).where(urldata.user_id == None).where(urldata.domain == None)
            results = session.exec(statement).first()
            if results is not None:
                results.user_id = user_id
                results.exp_time = adopted_exp_time
                session.add(results)
                session.commit()
                return results.short_url
                
        return None
        

def get_user_by_email(email: str) -> Optional[User]:
    """
    Retrieves a registered User record by their email address.
    """
    with Session(engine) as session:
        statement = select(User).where(email == User.email)
        return session.exec(statement).first()
    

def create_user(email: str, full_name: str, oauth_provider: str, oauth_id: str) -> User:
    """
    Creates and registers a new User account in the PostgreSQL database.
    Defaults the user's tier to 'free'.
    """
    with Session(engine) as session:
        user = User(
            email=email,
            full_name=full_name,
            oauth_id=oauth_id,
            oauth_provider=oauth_provider,
            created_at=datetime.now(UTC).replace(tzinfo=None),
            tier="free"
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user
    

def add_clicklog(log: clicklog):
    """
    Asynchronously registers visitor metrics (User Agent details, IP, referer) 
    in the database to populate the analytics dashboards.
    """
    with Session(engine) as session:
        session.add(log)
        session.commit()
