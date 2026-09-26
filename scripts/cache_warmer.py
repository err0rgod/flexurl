import os
import random
import sys
import time
from datetime import datetime, timezone

import httpx

ENDPOINTS_TO_WARM = [
    "https://api.zerodaily.in/api/v1/feed?limit=20",
    "https://api.zerodaily.in/api/v1/feed/ai?limit=20",
    "https://api.zerodaily.in/api/v1/feed/cybersec?limit=20",
    "https://api.zerodaily.in/api/v1/feed/programming?limit=20",
    "https://api.zerodaily.in/api/v1/feed/hardware?limit=20",
    "https://api.zerodaily.in/api/v1/feed/robotics?limit=20",
    "https://api.zerodaily.in/api/v1/feed/defense_aerospace?limit=20",
    "https://api.zerodaily.in/api/v1/feed/finance?limit=20",
    "https://api.zerodaily.in/api/v1/categories",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# Defaults: cycle repeats every 5 minutes (300s) with 2-6s random interval between pings
WARM_CYCLE_SECONDS = int(os.getenv("WARM_CYCLE_SECONDS", "300"))
MIN_DELAY_SECONDS = float(os.getenv("MIN_DELAY_SECONDS", "2.0"))
MAX_DELAY_SECONDS = float(os.getenv("MAX_DELAY_SECONDS", "6.0"))


def _current_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def warm_cache():
    print(f"[{_current_timestamp()}] Starting cache warming cycle for {len(ENDPOINTS_TO_WARM)} endpoints...", flush=True)

    with httpx.Client(timeout=10.0, follow_redirects=True) as client:
        for idx, url in enumerate(ENDPOINTS_TO_WARM):
            try:
                res = client.get(url, headers=HEADERS)
                cf_cache = res.headers.get("cf-cache-status", "NONE")
                print(
                    f"[{_current_timestamp()}] "
                    f"Warmed {url} -> {res.status_code} [CF-Cache: {cf_cache}]",
                    flush=True
                )
            except httpx.HTTPError as err:
                print(
                    f"[{_current_timestamp()}] "
                    f"HTTP error warming {url}: {err}",
                    flush=True
                )
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[{_current_timestamp()}] "
                    f"Unexpected error warming {url}: {exc}",
                    flush=True
                )

            # Random second interval between endpoint requests
            if idx < len(ENDPOINTS_TO_WARM) - 1:
                delay = random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
                time.sleep(delay)


def main():
    print("ZeroDaily Cache Warmer daemon started.", flush=True)
    while True:
        start_time = time.time()
        warm_cache()
        elapsed = time.time() - start_time

        # Sleep for remainder of the 5-minute cycle with random second jitter (+/- 10s)
        jitter = random.uniform(-10.0, 10.0)
        target_interval = WARM_CYCLE_SECONDS + jitter
        sleep_duration = max(5.0, target_interval - elapsed)

        print(
            f"[{_current_timestamp()}] "
            f"Cycle completed in {elapsed:.2f}s. Next cycle in {sleep_duration:.1f}s (~5 minutes)...",
            flush=True
        )
        time.sleep(sleep_duration)


if __name__ == "__main__":
    if "--once" in sys.argv:
        warm_cache()
    else:
        main()
