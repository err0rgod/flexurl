import time
from core.redis_client import redis_client
from core.logger import logger

class TokenBucket:
    """
    A Redis-backed, distributed implementation of the Token Bucket algorithm.
    Synchronizes tokens across multi-worker deployments and automatically
    expires inactive rate-limiting keys to prevent memory leakage.
    """
    def __init__(self, max_tokens: int, refill_rate: int, interval: int, key: str) -> None:
        self.max_tokens = max_tokens
        self.refill_rate = refill_rate
        self.interval = interval
        self.key = key
        
        self.tokens_key = f"rl:tokens:{key}"
        self.time_key = f"rl:time:{key}"
        self.lock_key = f"rl:lock:{key}"

    def _get_redis_data(self, now: float) -> tuple[float, float]:
        try:
            tokens_val = redis_client.get(self.tokens_key)
            time_val = redis_client.get(self.time_key)
            if tokens_val is None or time_val is None:
                return float(self.max_tokens), now
            return float(tokens_val), float(time_val)
        except Exception as e:
            logger.warning(f"Redis read failed in rate limiter: {e}")
            return float(self.max_tokens), now

    def _set_redis_data(self, tokens: float, last_refill: float) -> None:
        try:
            # Auto-expire keys after two cycles of inactivity to prevent memory leakage
            expire_time = self.interval * 2
            pipe = redis_client.pipeline()
            pipe.set(self.tokens_key, str(tokens), ex=expire_time)
            pipe.set(self.time_key, str(last_refill), ex=expire_time)
            pipe.execute()
        except Exception as e:
            logger.warning(f"Redis write failed in rate limiter: {e}")

    def allow_request(self, tokens: int = 1) -> bool:
        now = time.time()
        try:
            with redis_client.lock(self.lock_key, timeout=1.0):
                current_tokens, last_refill = self._get_redis_data(now)
                
                elapsed = now - last_refill
                if elapsed >= self.interval:
                    num_refills = int(elapsed // self.interval)
                    current_tokens = min(
                        float(self.max_tokens),
                        current_tokens + num_refills * self.refill_rate
                    )
                    last_refill += num_refills * self.interval
                
                if current_tokens >= tokens:
                    current_tokens -= tokens
                    self._set_redis_data(current_tokens, last_refill)
                    return True
                else:
                    # Sync refill timestamp even on blocked request
                    if elapsed >= self.interval:
                        self._set_redis_data(current_tokens, last_refill)
                    return False
        except Exception as e:
            logger.error(f"Rate limit lock/check failed: {e}. Allowing request as safety fallback.")
            return True

    def get_remaining(self) -> int:
        now = time.time()
        try:
            with redis_client.lock(self.lock_key, timeout=1.0):
                current_tokens, last_refill = self._get_redis_data(now)
                elapsed = now - last_refill
                if elapsed >= self.interval:
                    num_refills = int(elapsed // self.interval)
                    current_tokens = min(
                        float(self.max_tokens),
                        current_tokens + num_refills * self.refill_rate
                    )
                return int(current_tokens)
        except Exception:
            return self.max_tokens

    def get_reset_time(self) -> int:
        now = time.time()
        try:
            with redis_client.lock(self.lock_key, timeout=1.0):
                _, last_refill = self._get_redis_data(now)
                return int(last_refill + self.interval)
        except Exception:
            return int(now + self.interval)


class RateLimiterStore:
    """
    Factory for retrieving Redis-backed TokenBucket instances for specific keys.
    """
    def __init__(self, max_tokens: int, refill_rate: int, interval: int) -> None:
        self.max_tokens = max_tokens
        self.refill_rate = refill_rate
        self.interval = interval

    def get_bucket(self, key: str) -> TokenBucket:
        return TokenBucket(
            max_tokens=self.max_tokens,
            refill_rate=self.refill_rate,
            interval=self.interval,
            key=key
        )