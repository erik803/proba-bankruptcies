# Bankruptcy Detection Pilot

A pilot system that detects US Chapter 7 and Chapter 11 bankruptcy filings and surfaces them through a JSON API, a dashboard, and a webhook alert mechanism.

Built as a take-home for Veridion. See [SCHEMA.md](SCHEMA.md) for the data model and design rationale, and [progress.md](progress.md) for development status.

## Sources

- **Primary:** [CourtListener](https://www.courtlistener.com/) RECAP archive of US federal bankruptcy court dockets.
- **Secondary (Phase 3):** SEC EDGAR 8-K Item 1.03 filings for public-company cross-validation.

## Quick start

Requires **Python 3.11+** and either Postgres access (Supabase / hosted) or **Docker** for local Postgres.

```bash
# 1. Clone
git clone https://github.com/erik803/proba-bankruptcies.git
cd proba-bankruptcies

# 2. Virtual environment + install
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# macOS / Linux:
# source .venv/bin/activate
pip install -e ".[dev]"

# 3. Configure — create .env in the project root with:
#    COURTLISTENER_API_TOKEN=<your token from courtlistener.com>
#    DATABASE_URL=<postgres connection string>
#    ALERT_WEBHOOK_URL=                # optional, leave empty to disable alerts
```

### Database options

**Option A — hosted (Supabase / RDS / similar):**
1. Set `DATABASE_URL` in `.env` to the Postgres connection string.
2. Apply the migration: paste `migrations/001_initial_schema.sql` into the SQL editor and run.

**Option B — local Postgres via Docker:**
```bash
docker compose up -d postgres
```
The migration applies automatically on first container start. Set `DATABASE_URL=postgresql://bankruptcy:bankruptcy@localhost:5432/bankruptcy` in `.env`.

### Sanity check

```bash
python scripts/check_db.py
```

Should print row counts for `bankruptcy_event`, `debtor`, `alert_delivery` (zero on a fresh install).

## Running the pilot

_Coming in Phase 2._ Will include:

- `python -m bankruptcy.ingest` — pull recent filings from CourtListener
- `uvicorn bankruptcy.api:app` — JSON API + dashboard
- `python -m bankruptcy.alerts` — webhook delivery worker

## Project layout

```
bankruptcy/        Python package (config, db, models, ingest, api, alerts)
migrations/        Versioned SQL migrations
scripts/           Operational helpers
tests/             Tests
```
