# Persistent GitHub Badges

This adds standalone persistent counters without changing the core FlexURL application code.

## What It Counts

- `links_created`: increments whenever a row is inserted into `urldata`.
- `visitors`: increments whenever a row is inserted into `clicklog`.
- `redirects`: increments whenever a row is inserted into `clicklog`.

The counters are stored in `badge_counters`, so deleting links or click logs does not reduce the displayed totals.

## Install Database Counters

Run this once against the production database:

```powershell
python backend/install_badge_counters.py
```

The installer backfills from the existing `urldata` and `clicklog` row counts, then adds PostgreSQL insert triggers for future increments.

## Badge Endpoint

The existing FastAPI application serves SVG badges directly:

```text
/badge/visitors.svg
/badge/links-created.svg
/badge/redirects.svg
```

## Markdown

Use your existing public domain:

```markdown
[![FlexURL Visitors](https://flexurl.app/badge/visitors.svg?left_text=visitors&left_color=BLACK&right_color=GREEN)](https://flexurl.app)

[![FlexURL Links Created](https://flexurl.app/badge/links-created.svg?left_text=links%20created&left_color=BLACK&right_color=GREEN)](https://flexurl.app)

[![FlexURL Redirects](https://flexurl.app/badge/redirects.svg?left_text=redirects&left_color=BLACK&right_color=GREEN)](https://flexurl.app)
```

Use `units=raw` if you do not want compact values like `1.2K`.
