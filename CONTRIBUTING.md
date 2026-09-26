# Contributing to FlexURL

Thank you for your interest in contributing to **FlexURL**! We welcome contributions from the community to help make this platform faster, more reliable, and feature-rich.

Please take a moment to review this guide before submitting code or opening issues.

---

## Code of Conduct

By participating in this project, you agree to abide by the terms of our [Code of Conduct](CODE_OF_CONDUCT.md). Please treat all contributors with respect and professionalism.

---

## How Can I Contribute?

- **Reporting Bugs:** Open an issue using the [Bug Report template](.github/ISSUE_TEMPLATE/bug_report.md).
- **Suggesting Enhancements:** Open an issue using the [Feature Request template](.github/ISSUE_TEMPLATE/feature_request.md).
- **Code Contributions:** Fix open bugs, add test coverage, or implement requested features.
- **Documentation:** Improve guides, API documentation, or code comments.

---

## Development Setup

### 1. Prerequisites
- **Python:** 3.10+ (Python 3.12 recommended)
- **Redis:** Local instance or Docker container on port `6379`
- **PostgreSQL:** 14+ on port `5432`
- **Node.js:** 18+ (for compiling Tailwind CSS)
- **Git**

### 2. Clone and Setup Environment
```bash
# Clone the repository
git clone https://github.com/err0rgod/flexurl.git
cd flexurl

# Create and activate virtual environment
python -m venv venv
# On Linux/macOS:
source venv/bin/activate
# On Windows:
.\venv\Scripts\activate

# Install Python dependencies
pip install -r requirements.txt
pip install ruff pytest httpx

# Install Node dependencies for Tailwind
npm install
```

### 3. Environment Variables
Copy and configure your `.env` file:
```bash
cp .env.example .env # or configure .env directly
```
Ensure `DB_PATH`, `REDIS_HOST`, and `JWT_SECRET_KEY` are configured for your local environment.

### 4. Build Frontend Assets
```bash
npm run build:css
```

### 5. Running the Application Locally
In terminal 1 (FastAPI backend):
```bash
uvicorn backend.app:app --reload --port 8000
```
In terminal 2 (ARQ analytics worker):
```bash
arq backend.services.arq_worker.WorkerSettings
```

---

## Coding Standards & Conventions

1. **Async I/O:** Always use `async`/`await` for I/O-bound operations (database queries, network calls, cache operations).
2. **Background Jobs:** Heavy external API requests (e.g. Google Safe Browsing, Resend email) must be dispatched via `BackgroundTasks` or ARQ queues.
3. **Database Queries:** Use SQLModel queries. When checking for `NULL` in queries, use `== None` (e.g., `urldata.user_id == None`) to ensure proper SQL `IS NULL` compilation.
4. **Validation:** Keep input validation logic in `backend/utils/validations.py`.
5. **Code Style & Linting:**
   We enforce linting using [Ruff](https://github.com/astral-sh/ruff). Ensure checks pass before committing:
   ```bash
   ruff check backend/ --select F
   ```

---

## Testing

Always run the test suite to verify your changes do not break existing workflows:
```bash
# Make sure your local Redis and PostgreSQL instances are running
pytest tests/
```

All existing redirect, expiration, and authentication tests must pass before submitting a Pull Request.

---

## Pull Request Guidelines

1. **Branch Naming:**
   - Feature: `feat/short-description`
   - Bug fix: `fix/short-description`
   - Documentation: `docs/short-description`
2. **Commit Messages:**
   - Write clear, imperative commit messages (e.g., `Add custom expiration check to redirect route`).
3. **Keep PRs Focused:**
   - Address a single issue or feature per pull request to streamline review.
4. **CI Checks:**
   - Ensure the automated GitHub Actions workflow (Ruff linter and tests) is green.
5. **Fill out the PR Template:**
   - Complete all sections of the [Pull Request Template](.github/pull_request_template.md).

---

## Questions?

Feel free to open a GitHub Discussion or reach out to [@err0rgod](https://github.com/err0rgod).
