# Project Memory & Handoff State

## Context
This project is **FlexURL**, a URL shortener built with FastAPI, SQLModel/PostgreSQL, Redis, and a Vanilla JS frontend.
We have implemented premium subscription features (passwords, custom fallback URLs, iOS/Android target routing, real-time webhooks, bot detection, city-level geolocation analytics, and CSV log exports).

---

## Current Development State

### 1. Backend (Fully Implemented & Secured)
All backend logic supporting premium capabilities and security validation is active:
- **`backend/models.py`:** Consolidated all schemas and Pydantic request models (`URLRequest`, `URLEditRequest`, `QuoteRequest`, `SupportTicketRequest`, `PaymentOrderRequest`, `PaymentVerifyRequest`, `CustomDomainRequest`). Added custom domain metadata column to the `urldata` model.
- **`backend/database.py`:** In `init_db()`, added automatic SQL code execution to safely drop the PostgreSQL index/constraint `urldata_long_url_key` if it exists, and migrate the new `domain` column.
- **`backend/app.py`:**
  - **FastAPI Dependency Injection Authentication:** Introduced clean auth helper dependencies (`get_optional_user_id` and `get_required_user_id`) to automatically handle cookie-based JWT decoding and validation. Refactored all `/api/` endpoints to use this middleware-style dependency injection.
  - **Custom Domain Routing (Cloudflare for SaaS):** Integrated `CloudflareSaaSManager` in [cloudflare_saas.py](file:///D:/urlshortener/backend/cloudflare_saas.py) to automatically register and remove custom hostnames via the Cloudflare API (`POST /zones/{zone_id}/custom_hostnames`). Enforced access constraints on link redirection, ensuring custom domain links are only resolvable through the selected domain or `flexurl.app` (not other custom domains).
  - **On-Demand TLS Helper Route:** Added `GET /api/domains/check-allowed` to verify custom domains against registered entries in PostgreSQL.
  - **Domain Registration Formats (Stored XSS Prevention):** Added strict regex validation in `POST /api/domains` to restrict domain names to valid DNS characters, blocking folder traversal paths and script injection tags.
  - **Dynamic Redis Cache:** Premium links (and links created with custom domains) are marked in Redis as `"DYNAMIC"` to enforce DB-level redirect parameter and domain routing validation checks.
  - **Support Ticketing API & UI:** Added a dedicated `/support` route serving a clean, user-friendly [support.html](file:///D:/urlshortener/frontend/support.html) ticket center. Handled query submissions via `POST /api/support` to save queries to `support_tickets.json` and dispatch email alerts to the admin's `ADMIN_EMAIL`. Escaped all user input values using `html.escape` to prevent HTML Injection/XSS in email clients.
  - **Quotes Page Route:** Remapped `/quotes` route to serve [contact.html](file:///D:/urlshortener/frontend/contact.html) for custom Enterprise Quotes. Escaped all user-provided business details in `quotation.py` to prevent HTML injection email vulnerabilities.
  - **Documentation Route:** Added `GET /documentation` to serve the interactive documentation page.
  - **Client IP Resolution:** Added `get_client_ip` helper resolving actual visitor IPs from Cloudflare (`CF-Connecting-IP`) and reverse proxy (`X-Forwarded-For`) headers, resolving the Netherlands/Amsterdam geolocation mismatch.
  - **Premium Checkout Pricing:** Set up Razorpay backend order API to charge in INR (₹1599/month for Startup Choice, ₹3499/month for Business Pro) to support domestic UPI/QR code and card processing, while maintaining USD pricing displayed in the frontend.

### 2. Frontend UI & Static Assets
All frontend interface files are completed with high-impact, premium aesthetics:
- **Logo Types Integration:** Wired up `logo.svg` across all page headers and footers (with heights set to `h-12` and `h-10` respectively, plus CSS monochrome inversion in dark mode), configured the header logo image with `py-1.5 px-2` padding to prevent edge cropping, decoupled the logo width from layout shifts using a fixed-width `w-48` container, expanded all header inner containers by 8% (to `max-w-[1380px]` and `max-w-[1244px]`), and configured `small_logo.svg` as the canonical SVG favicon in `<head>` across all 13 HTML pages.
- **FastAPI Static Mount:** Mounted `StaticFiles` at `/static` inside [app.py](file:///D:/urlshortener/backend/app.py) to enable robust local delivery of these SVG and video assets.
- **[index.html](file:///D:/urlshortener/frontend/index.html):** Swapped the "Most Popular" highlight badge and Indigo card formatting to the Startup Choice ($19/mo) plan. Changed the video choice header to "Which founder are you?". Replaced duplicate footer items and API links with clean Resources and Legal columns including a Contact Support link to `/support`. Exposed the advanced premium settings accordion wrapper to all logged-in users (free tier and premium), disabling input fields and adding helper warning alerts for free-tier profiles to prompt upgrades. Passed selected custom domain parameter in the JSON payload of the shorten AJAX call to ensure correct database storage. Integrated the new video asset paths `/static/sad_meme.mp4` and `/static/dancing_meme.mp4` within the custom video blocks.
- **[documentation.html](file:///D:/urlshortener/frontend/documentation.html):** Added an interactive, responsive documentation page detailing all features, DNS setup with Nginx/Cloudflare, custom domain routing constraints (isolation/security guidelines), branded domain creation procedures, and an integrated Support Ticket contact form.
- **[dashboard.html](file:///D:/urlshortener/frontend/dashboard.html):** Restructured custom domain dropdown options, displaying custom domains instead of root hosts. Changed billing tier pricing buttons to show USD values ($19 and $39).
- **Footer & Header Cleanups:** Removed all personal social links pointing to `err0rgod` profiles (`github.com/err0rgod`, `x.com/err0rgod`, `linkedin.com/in/nirbhay-katiyar`, `err0rgod.medium.com`) and updated copyright headers to "FlexURL". Cleaned up the header navigation across system template pages (`privacy.html`, `terms.html`, `banned.html`, `expired.html`) to link to `/documentation` and `/support` instead of dead anchors.

---

## Infrastructure & Nginx Configuration
- **Cloudflare SSL Integration:** Configured to run in **Full (non-strict)** mode, which routes encrypted traffic to the origin VPS and accepts the fallback primary certificate for the custom domain handshake.
- **Nginx configuration (`/etc/nginx/sites-available/urlshortener`):**
  - **HTTP/2 support:** Enabled via `listen 443 ssl http2 default_server;`.
  - **Gzip Compression:** Enabled inside the server blocks.
  - **Canonical Redirects:** Automatically redirects HTTP to HTTPS and `www` to non-`www` *only* for the primary domain (`flexurl.app`), preventing redirect loop issues with custom domains using Cloudflare Flexible mode.
  - **Custom Domain Proxying:** Catch-all default block forwards all HTTP/HTTPS custom domain requests directly to FastAPI port 8000.

---

## Active Rules
- **No emojis** in code, templates, or documentation.
- All database inputs must use **timezone-naive UTC datetimes**.

---

## Next Steps for Future Sessions
All core workflows, pricing, and infrastructure routes have been fully completed and tested. The application is ready for marketing and production deployment.
