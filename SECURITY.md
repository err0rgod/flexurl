# Security Policy

At FlexURL, the security of our users and infrastructure is a top priority. We take all security vulnerabilities seriously and appreciate responsible disclosure.

---

## Supported Versions

Only the latest release on the `main` branch receives active security updates and patches.

| Version / Branch | Supported          |
| ---------------- | ------------------ |
| `main` (latest)  | :white_check_mark: |
| Older releases   | :x:                |

---

## Reporting a Vulnerability

If you discover a security vulnerability or potential threat in FlexURL, please report it responsibly:

> [!WARNING]
> **Please do NOT report security vulnerabilities through public GitHub issues, pull requests, or discussions.**

### Contact Options
1. **GitHub Security Advisories (Preferred):**
   Submit a private advisory via GitHub: [Report a vulnerability](https://github.com/err0rgod/flexurl/security/advisories/new)
2. **Direct Contact:**
   Send an email with the subject line `[SECURITY] FlexURL Vulnerability Report` to the project maintainer:
   - **Maintainer:** Nirbhay Katiyar ([@err0rgod](https://github.com/err0rgod))
   - **X / Twitter:** [@err0rgod](https://x.com/err0rgod)

### What to Include in Your Report
To help us investigate and reproduce the issue quickly, please provide:
- A descriptive summary of the vulnerability.
- Step-by-step instructions or Proof of Concept (PoC) to reproduce the behavior.
- Affected endpoints, parameters, or components.
- Potential impact and exploitation scenario.
- Suggested remediations or mitigations (if known).

---

## Security Architecture & Defenses

FlexURL incorporates multi-layered security controls across the stack:

1. **Malicious Link Protection:**
   - Asynchronous URL scanning using the **Google Safe Browsing API**.
   - Banned links are quarantined in PostgreSQL (`is_banned = True`) and cached as `"BANNED"` in Redis to immediately serve security landing warnings instead of redirects.
2. **Server-Side Request Forgery (SSRF) Prevention:**
   - All destination URLs undergo hostname and IP validation to block loopback (`127.0.0.0/8`, `::1`), private ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), and link-local addresses before outbound HTTP checks.
3. **Rate Limiting & Abuse Prevention:**
   - Distributed sliding-window rate limiting enforced via Redis on sensitive endpoints (link creation, login, payment sessions).
4. **Authentication & Session Hardening:**
   - Cryptographically signed JWT tokens stored in `HttpOnly`, `SameSite=Lax`, and `Secure` cookies.
   - Firebase Admin SDK verification on identity tokens.
5. **Infrastructure Isolation:**
   - Background tasks (analytics flushing, reports, Safe Browsing checks) execute asynchronously in dedicated ARQ worker processes to prevent denial-of-service on the HTTP event loop.

---

## Response Timeline & Disclosure Process

- **Acknowledgment:** Within 48 hours of initial report receipt.
- **Triage & Assessment:** We will confirm the vulnerability and determine its severity level.
- **Patch Development:** A fix will be developed and verified in a private branch.
- **Deployment:** The patch will be deployed to production servers and merged to `main`.
- **Coordinated Disclosure:** We will publish a security advisory detailing the issue and crediting the reporter (unless anonymity is requested).
