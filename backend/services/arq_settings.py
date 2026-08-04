import os
from arq.connections import RedisSettings

REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

# Configuration tuning variables
CLICK_FLUSH_INTERVAL = int(os.getenv("CLICK_FLUSH_INTERVAL", 30))
GEO_CACHE_TTL = int(os.getenv("GEO_CACHE_TTL", 604800))  # 7 days
CLICK_BATCH_SIZE = int(os.getenv("CLICK_BATCH_SIZE", 1000))
REDIS_LOCK_TIMEOUT = int(os.getenv("REDIS_LOCK_TIMEOUT", 60))
GEO_IP_TIMEOUT = float(os.getenv("GEO_IP_TIMEOUT", 2.0))
WEBHOOK_TIMEOUT = float(os.getenv("WEBHOOK_TIMEOUT", 5.0))

redis_settings = RedisSettings(host=REDIS_HOST, port=REDIS_PORT)
