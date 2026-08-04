# FlexURL

FlexURL is a modern, full-stack, enterprise-grade URL shortening platform built around FastAPI, PostgreSQL, Redis, and static Tailwind-based frontend pages. It supports anonymous and authenticated link creation, branded domains, protected redirects, analytics collection, developer API access, subscription-aware feature gating, and custom dynamic Github Badges.

This repository contains both the backend services API and the static frontend served by the platform.

## Features

- **Link Shortening:** Transform long URLs into compact shareable links with custom alias support.
- **Branded Domains:** Resolve links through the primary host or verified custom SaaS domains (powered by Cloudflare).
- **Advanced Premium Controls:** 
  - Password-protected links
  - Activation scheduling & auto-expiration
  - Fallback URLs
  - OS-specific targeting (iOS vs Android)
  - Custom countdown redirects
  - Outbound webhooks on redirect events
- **Security:** Asynchronous Google Safe Browsing checks to automatically block malicious destinations.
- **Analytics Pipeline:** High-performance, Redis-buffered analytics collection tracking country, city, browser, device, referer, and bot signals.
- **Badge Counters:** Live, dynamic SVG Github badges displaying total visitors and links created.
- **Monetization & Auth:** Firebase-backed authentication (with JWT cookies) and Razorpay integration for Free, Startup, and Business tiers.
- **Developer API:** API key management and programmatic single/batch shortening endpoints.
- **Robust Background Jobs:** Dedicated ARQ workers for flushing analytics, reporting, and executing subscription dunning emails securely using Redis locks.

## Tech Stack

- **Backend:** FastAPI (Python 3.10+)
- **ORM & Database:** SQLModel / SQLAlchemy with PostgreSQL
- **Cache & Queue:** Redis
- **Background Jobs:** ARQ (Asynchronous Redis Queue)
- **Frontend:** Vanilla HTML/JS with Tailwind CSS (JIT)
- **Authentication:** Firebase Admin SDK + JWT Session Cookies
- **Payments:** Razorpay
- **Email Delivery:** Resend API
- **DNS & SSL:** Cloudflare for SaaS

## System Architecture & Directory Structure

FlexURL has been meticulously refactored into a highly modular, scalable micro-architecture pattern.

```text
/
├── backend/
│   ├── api/routes/          # Modular FastAPI routers (auth, links, billing, etc.)
│   ├── core/                # Core system configurations (database, redis, logger)
│   ├── models/              # SQLModel schema definitions (domain.py)
│   ├── services/            # Heavy background workers and 3rd-party integrations
│   │                        # (ARQ workers, Cloudflare SaaS, report schedulers)
│   ├── utils/               # Helpers (Base62 generator, validation, rate limiting)
│   └── app.py               # Main FastAPI entrypoint merging all routers
├── frontend/                # Static HTML files and UI layouts
│   └── static/              # CSS, SVGs, Videos, and raw assets
├── config/                  # Highly sensitive configurations (Firebase Admin SDK JSON)
├── data/                    # Flat-file data persistence (quotations, support tickets)
├── docs/                    # Architectural documents, memory logs, audits
├── deploy/                  # Systemd service files and deployment scripts
├── tests/                   # Pytest suites for link expiration, routing, and backend
├── pyproject.toml           # Python package definitions
├── package.json             # Tailwind CSS build definitions
└── nginx.conf               # Production Nginx reverse-proxy configuration
```

## Core Workflows

### 1. Link Lifecycle & Redirection
Links are persisted in PostgreSQL. Redis acts as a high-speed cache for rate limits, counter synchronization, and short-lived session states. When a link is visited, the system instantly validates its status against cache (checking expiration, passwords, and ban-lists) before executing the HTTP 302 redirect.

### 2. High-Performance Analytics Pipeline
Instead of blocking the redirect to write analytics to Postgres, redirect events are offloaded to an ARQ background worker. The worker handles Geo-IP lookup, parses User-Agent headers, detects bots, and flushes aggregated data to the database in batches.

### 3. Concurrency-Safe Schedulers
The system runs background schedulers for daily reports and dunning emails. These operations use highly robust Redis `SET NX` concurrency locks paired with O_EXCL local file locks to ensure that even in multi-worker environments (e.g. 4+ Uvicorn instances), emails are only triggered exactly once.

### 4. Custom Domains (Cloudflare SaaS)
FlexURL programmatically provisions custom hostnames via the Cloudflare API. When a premium user adds a domain, the backend validates DNS CNAME ownership and orchestrates SSL provisioning seamlessly.

## Environment Variables

Configure the system by placing a `.env` file in the root directory.

### Core Configuration
- `DB_PATH`: PostgreSQL connection string (e.g., `postgresql://user:pass@localhost:5432/flexurl`)
- `JWT_SECRET_KEY`: High-entropy secret for signing session cookies
- `REDIS_HOST` / `REDIS_PORT`: Redis connection targets (default: 127.0.0.1:6379)
- `FIREBASE_ADMIN_SDK_JSON`: Path to Firebase Service Account JSON (e.g., `config/flexurlapp-firebase-adminsdk...json`)

### Integrations
- `SAFE_BROWSING`: Google Safe Browsing API Key
- `RESEND_API_KEY`: Resend Email API Key
- `ADMIN_EMAIL`: Destination for daily reports and support tickets
- `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET`: Razorpay Payment Gateway
- `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ZONE_ID`: Cloudflare SaaS API Configuration

### Tuning
- `CLICK_FLUSH_INTERVAL`: Seconds between ARQ analytics flushes
- `CLICK_BATCH_SIZE`: Maximum batch size for Postgres insertions
- `GEO_CACHE_TTL`: Geo lookup cache expiry

## Local Development Guide

1. **Install Dependencies:**
   ```bash
   pip install -r requirements.txt
   npm install
   ```

2. **Set up `.env` File:**
   Ensure database and redis configurations are set.

3. **Compile Tailwind CSS:**
   ```bash
   npm run build:css
   ```

4. **Start the FastAPI Backend:**
   ```bash
   uvicorn backend.app:app --reload
   ```

5. **Start the Background Analytics Worker:**
   ```bash
   arq backend.services.arq_worker.WorkerSettings
   ```

## Testing

Comprehensive testing suites are provided in the `/tests` directory. Run them via pytest:
```bash
pytest tests/
```

## Deployment Considerations (AWS Lightsail)

- **Reverse Proxy:** Run Nginx to route port 80/443 traffic to the Uvicorn workers on port 8000.
- **Badge Counter:** If using the standalone SVG Badge Counter, route `/badge/` traffic to the standalone badge service running on port 8010.
- **Worker Daemon:** Keep the ARQ worker running as a persistent systemd daemon alongside the main API.

---
*Maintained by Nirbhay Katiyar (@err0rgod)*
