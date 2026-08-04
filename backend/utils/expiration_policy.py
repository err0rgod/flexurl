from datetime import UTC, datetime, timedelta
from typing import Optional


FREE_MAX_EXPIRATION = timedelta(days=15)


def calculate_link_expiration(
    user_tier: str,
    is_authenticated: bool,
    requested_hours: Optional[int],
    now: Optional[datetime] = None,
) -> Optional[datetime]:
    """Return the stored expiration timestamp for a newly created link."""
    current_time = now or datetime.now(UTC).replace(tzinfo=None)

    if user_tier != "free":
        return current_time + timedelta(hours=requested_hours) if requested_hours else None

    maximum_expiration = current_time + FREE_MAX_EXPIRATION
    if requested_hours:
        requested_expiration = current_time + timedelta(hours=requested_hours)
        return min(requested_expiration, maximum_expiration)

    return None if is_authenticated else maximum_expiration
