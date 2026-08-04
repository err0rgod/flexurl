# Project Maintenance and Migration Guide

## Project Structure
- app.py: Main entry point, auth dependencies, and API routes.
- models.py: SQLModel database schemas and Pydantic request models.
- database.py: Database connection, migration commands, and CRUD operations.
- short_url_gen.py: ID generation (Sonyflake), caching logic, and dynamic parameters mapping.
- arq_settings.py: ARQ queue connection details and tuning constants.
- arq_worker.py: ARQ worker background analytics task runner, Geo-IP resolver, and click count synchronizer.
- validations.py: URL validation, Safe Browsing integration, and custom alias checks.
- frontend/: HTML/CSS/JS files for the single page web application.

## Configuration (Environment Variables)
The application requires a .env file in the root directory with the following keys:
- DB_PATH: Connection string for PostgreSQL (e.g., postgresql://user:password@localhost/dbname).
- SAFE_BROWSING: Google Safe Browsing API Key.
- REDIS_HOST: Address of the Redis cache (defaults to 127.0.0.1).
- REDIS_PORT: Port of the Redis cache (defaults to 6379).

Additional queue performance parameters can be tuned in .env:
- CLICK_FLUSH_INTERVAL: Time interval in seconds to flush buffered Redis click counts to DB (default: 30).
- CLICK_BATCH_SIZE: Max number of clicks processed per flush loop (default: 1000).
- REDIS_LOCK_TIMEOUT: Lock lifetime in seconds for the click flusher distributed lock (default: 60).
- GEO_IP_TIMEOUT: Network request timeout for ip-api.com lookup in seconds (default: 2.0).
- WEBHOOK_TIMEOUT: Network request timeout for client webhook dispatches in seconds (default: 5.0).
- GEO_CACHE_TTL: Expiration time in seconds for cached visitor Geo-IP data (default: 604800 / 7 days).

## Database Configuration (PostgreSQL)
If migrating to a new database:
1. Create the database: CREATE DATABASE urlshortener;
2. Create the user: CREATE USER urluser WITH PASSWORD 'password';
3. Grant permissions: GRANT ALL PRIVILEGES ON DATABASE urlshortener TO urluser;
4. Ensure the schema exists. Run the following in the Python environment:
   python -c "from database import engine; from models import SQLModel; SQLModel.metadata.create_all(engine)"
5. If the table already exists, manually add the status column:
   ALTER TABLE urldata ADD COLUMN is_banned BOOLEAN DEFAULT FALSE;

## Process Management (Systemd)
The application is managed as two separate system services: Gunicorn (FastAPI Web App) and ARQ (Background Task Worker).

### 1. Web Application Service (/etc/systemd/system/urlshortener.service)
- Service commands:
  - Start: `sudo systemctl start urlshortener`
  - Restart: `sudo systemctl restart urlshortener`
  - Status: `sudo systemctl status urlshortener`
  - Logs: `sudo journalctl -u urlshortener -f`

### 2. ARQ Background Worker Service (/etc/systemd/system/flexurl-worker.service)
- Service commands:
  - Start: `sudo systemctl start flexurl-worker`
  - Restart: `sudo systemctl restart flexurl-worker`
  - Status: `sudo systemctl status flexurl-worker`
  - Logs: `sudo journalctl -u flexurl-worker -f`

## Reverse Proxy (Nginx)
The application is served via Nginx on port 80/443.
- Config Location: /etc/nginx/sites-available/urlshortener
- Critical header: proxy_set_header X-Forwarded-Proto $scheme; (Required for Cloudflare Full Strict mode).

## SSL and Cloudflare (SSL for SaaS)
- The primary domain SSL is handled by Nginx fallback certificates (or Certbot) on the server and proxied by Cloudflare.
- Cloudflare SSL mode should be set to **Full (non-strict)** to authorize edge validation for users' custom domains without certificate verification loop issues.
- Connect custom hostnames via the Cloudflare dashboard Fallback Origin configuration mapping to `fallback-origin.flexurl.app`.
- Static IPv4 must be attached in the AWS Lightsail console.

## Update Workflow
To update the application with new code:
1. Pull the latest code (git pull or scp).
2. Activate the virtual environment: source venv/bin/activate
3. Install new dependencies: pip install -r requirements.txt
4. Restart the web service: sudo systemctl restart urlshortener
5. Restart the background worker service: sudo systemctl restart flexurl-worker
6. Restart Nginx if config changed: sudo systemctl restart nginx

## Migration to New Server
1. Export the database: pg_dump urlshortener > backup.sql
2. Install dependencies on new server (Redis, Postgres, Nginx, Python).
3. Import the database: psql urlshortener < backup.sql
4. Copy the .env file.
5. Reconfigure Nginx and Systemd as per the paths above.
6. Update the A record in Cloudflare to the new server IP.
