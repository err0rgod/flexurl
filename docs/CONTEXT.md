# FlexURL Session Context - June 28, 2026

## What We Did Today

1. **Custom Domain Redirection & SSL (Cloudflare for SaaS):**
   - Integrated automated Cloudflare for SaaS custom hostname registration in `POST /api/domains` to provision SSL/TLS certificates and manage edge routing.
   - Enforced routing access constraints: custom domain links can only be resolved through their selected domain or `flexurl.app`, blocking resolution via another custom domain.
   - Cleans up custom hostname registration upon deletion inside `DELETE /api/domains/{domain_id}`.
   - Updated custom domain models and schemas to track active custom hostname IDs.
   - Set up the Let's Encrypt / Caddy check-allowed DNS validation route at `GET /api/domains/check-allowed`.

2. **Cloudflare & Nginx Infrastructure:**
   - Configured Cloudflare proxy SSL routing to run in **Full (non-strict)** mode to prevent Error 526.
   - Built a complete catch-all Nginx configuration supporting HTTP/2, Gzip compression, and HTTP-to-HTTPS canonical redirects.
   - Allowed custom domains on port 80 to proxy directly without forced redirect loops, accommodating Cloudflare Flexible mode.

3. **Multi-Payment Gateway Fix:**
   - Adjusted Razorpay checkout orders to be processed in INR (₹1599/month for Startup Choice, ₹3499/month for Business Pro) to support UPI, domestic QR codes, Netbanking, and international cards.
   - Kept USD prices ($19/mo and $39/mo) displayed on the frontend landing page and user dashboard.

4. **Dedicated Support Center & Quotes Routing:**
   - Created the `/support` route serving a dedicated [support.html](file:///D:/urlshortener/frontend/support.html) ticket center.
   - Connected form submissions to a backend `/api/support` endpoint which logs tickets to `support_tickets.json` and sends admin notification emails via the Resend API to `ADMIN_EMAIL`. Escaped all user inputs using `html.escape` to block HTML Injection in email clients.
   - Remapped the custom Enterprise Quotes page to `/quotes` (serving `contact.html` directly without redirects). Escaped all business profile inputs inside `quotation.py` to prevent HTML injection email vulnerabilities.
   - Linked to `/support`, `/quotes`, and `/documentation` from all template page headers and footers, removing old `API Keys` and `API Docs` placeholders.
   - Updated the custom domains guide on [documentation.html](file:///D:/urlshortener/frontend/documentation.html) to detail the new strict routing access constraints and describe branded domain creation procedures.
   - Added regex-based domain format verification in `/api/domains` to prevent Stored XSS attacks inside the user dashboard.

5. **Author Branding & Landing Page Updates:**
   - Removed personal social links (GitHub, Twitter/X, Medium, LinkedIn, PyPI) belonging to the author `err0rgod` from all HTML footers and input placeholders across the site.
   - Swapped "Most Popular" plan visual highlighting to Startup Choice ($19/mo).
   - Exposed the advanced settings panel on the home page to all logged-in profiles (including free users) but disabled editing permissions and added upgrade alert prompts.
   - Passed selected custom domain parameter in the JSON payload of the shorten AJAX call to ensure correct database storage.
   - Updated the `is_long_url_exists` check to match on the chosen domain, preventing duplicate record re-use without domains.

6. **SEO & Meta Enhancements:**
   - Corrected canonical URLs to `https://flexurl.app/` inside robots.txt and sitemap.xml.
   - Embedded structured JSON-LD Schema markup in `index.html` to enable rich snippet generation in search results.

7. **Code Consolidation & Authentication Refactoring:**
   - Moved all request schemas and Pydantic models from `app.py` to `models.py`, making all schema definitions central and unified.
   - Replaced redundant JWT token decoding and cookie validation logic across endpoints with FastAPI dependency injection helpers (`get_optional_user_id` and `get_required_user_id`), significantly reducing boilerplate code.

8. **Logo Types and Static Asset Integration:**
   - Wired up the normal sized `logo.svg` across all 12 page headers (scaled to `h-12` inside an independent `w-48` container with `py-1.5 px-2` padding) and footers (scaled to `h-10`) with a custom CSS filter mapping monochrome in dark mode. Increased the header container width by 8% to match.
   - Configured `small_logo.svg` as the canonical SVG favicon in `<head>` across all 13 system pages.
   - Mounted `StaticFiles` in `app.py` to enable FastAPI local delivery of these SVG and video assets.
   - Adjusted video blocks inside `index.html` to reference the new `/static/` subfolder locations.
