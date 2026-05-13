# Bankruptcy Detection Pilot

A pilot system that detects US Chapter 7 and Chapter 11 bankruptcy filings and surfaces them through a JSON API, a dashboard, and a basic webhook alert mechanism.

Built as a take-home for Veridion.

## What this is

Two source pipelines feed a normalizer, a classifier, and post-processing passes:

- **CourtListener** — the broad lane. Pulls every Chapter 7 / Chapter 11 docket from any of the 95 US federal bankruptcy courts. Most events come from here. Hours-to-day latency depending on PACER ingestion.
- **SEC EDGAR** — the fast lane. Pulls 8-K Item 1.03 filings, which public companies must file within 4 business days of declaring bankruptcy. Smaller coverage (public companies only) but lower latency than the court system. We fetch the full 8-K body and pull out the chapter, court, and case number — which lets us catch state-law proceedings (e.g. a Florida Assignment for the Benefit of Creditors) that file under Item 1.03 but aren't actually federal bankruptcies.

Then:

- A **normalizer + classifier** turns raw source records into events with calibrated `business` / `individual` / `unknown` classifications and confidence scores.
- A **clustering pass** detects corporate groups (a big bankruptcy like QVC shows up as 17 separate court dockets; we link them into one group).
- A **cross-source pass** matches EDGAR ↔ CourtListener via case-number primary-key when available, else via name + date proximity.
- A **persistent watermark** lets scheduled polls resume from the last successful run.

Read [`DECISIONS.md`](DECISIONS.md) for the design rationale on each call (gitignored locally; the public version of the same content lives across [`components/`](components/) and inline code docs).

## Quick start

Requires **Python 3.11+** and access to a Postgres database (Supabase or local).

```bash
# 1. Clone
git clone https://github.com/erik803/proba-bankruptcies.git
cd proba-bankruptcies

# 2. Virtual environment + install
python -m venv .venv
.\.venv\Scripts\Activate.ps1   # Windows
# source .venv/bin/activate    # macOS / Linux
pip install -e ".[dev]"

# 3. Create a .env file in the project root:
#    COURTLISTENER_API_TOKEN=<token from courtlistener.com>
#    DATABASE_URL=postgresql://USER:PASS@HOST:5432/DBNAME
#    ALERT_WEBHOOK_URL=                 # optional; see "Alerts" below

# 4. Apply the migrations against your Postgres in order:
#    migrations/001_initial_schema.sql
#    migrations/002_jurisdiction_court_id_nullable.sql
#    migrations/003_ingest_watermark.sql

# 5. Sanity check
python scripts/check_db.py
```

`check_db.py` should print row counts for the four tables (zero on a fresh install).

## Running the pilot

### Pull data from the sources

```bash
# CourtListener — nationwide poll, resumes from the last watermark.
python -m bankruptcy.ingest --use-watermark

# CourtListener — explicit window, useful for backfills.
python -m bankruptcy.ingest --filed-after 2026-04-13 --filed-before 2026-05-13

# CourtListener — nationwide Ch 7 backfill, filtering out obvious consumer cases
# at ingest. Designed for the dominant-consumer Ch 7 stream where ~99% is noise.
python -m bankruptcy.ingest --chapter 7 --filed-after 2026-04-13 --skip-individual

# EDGAR — pulls 8-K Item 1.03 filings + fetches each body for chapter/court/case#.
python -m bankruptcy.ingest_edgar --use-watermark
```

The ingest is **idempotent**: re-running the same window won't duplicate. The rate-limit-aware client honors `Retry-After` and paces at 13 s/page by default (overridable via `CL_INTER_PAGE_SLEEP_S` for long sustained runs — we use 75 s for the nationwide Ch 7 backfill to stay under the documented 50/hour cap).

### Post-processing passes

```bash
# Detect corporate groups (joint-administration + consecutive case numbers).
python -m bankruptcy.clustering

# Link EDGAR ↔ CourtListener via case-number primary key, then name+date fallback.
python -m bankruptcy.crosscheck

# For EDGAR rows whose 8-K body gave us a case# but whose CL docket isn't yet
# in our DB (often because the docket pre-dates our backfill window): targeted
# CL fetch for that exact (court, case) tuple. Then re-run crosscheck.
python scripts/fetch_cl_for_edgar.py

# Re-run the normalizer + body parsers against stored raw payloads.
# Use when normalizer logic improves and you want to retrofit without re-fetching.
python -u scripts/renormalize.py
python -u scripts/reparse_edgar_bodies.py
```

### Run the API + dashboard

```bash
uvicorn bankruptcy.api:app --reload
# → http://localhost:8000  (dashboard)
# → http://localhost:8000/about  (explainer of dashboard terms)
# → http://localhost:8000/docs   (auto-generated OpenAPI)
```

### Sample API queries

```bash
# Recent events
curl "http://localhost:8000/bankruptcies?limit=20"

# Filter by company name (substring, case-insensitive)
curl "http://localhost:8000/bankruptcies?company=QVC"

# Filter by chapter + min confidence
curl "http://localhost:8000/bankruptcies?proceeding_type=chapter_11&min_confidence=0.8"

# Cross-source confirmed business filings in the last week
curl "http://localhost:8000/bankruptcies?classification=business&from=2026-05-06"

# All events in one corporate group (returned by the dashboard "Top groups" panel)
curl "http://localhost:8000/bankruptcies?group_id=<uuid>"

# Detail view for one event
curl "http://localhost:8000/bankruptcies/<event_id>"
```

## Alerts

The pilot fires a webhook POST when a new event is inserted, configured via `ALERT_WEBHOOK_URL` in `.env`. Each attempt is recorded in the `alert_delivery` table with HTTP status + timestamp for audit.

**For the local demo**, the simplest path is [https://webhook.site](https://webhook.site) — open the URL, copy your unique webhook into `ALERT_WEBHOOK_URL`, and you'll see each fresh event POST land in real time.

**Important caveat**: because the pilot runs locally (not deployed), the alert layer is intentionally minimal. A production deployment would replace this with something fan-out-friendly — n8n routing to Slack/email/PagerDuty, or a per-customer subscription model with filtering. The current "POST every insert to one URL" is enough to demonstrate the *mechanism*, not the *product*.

## Testing

```bash
pytest
```

The `classify_debtor` heuristic (the central messy-data decision in the pipeline) has full test coverage of all four rules + their priority interactions; see [`tests/test_classify_debtor.py`](tests/test_classify_debtor.py).

## Project layout

```
bankruptcy/        Python package
  api.py           FastAPI app: JSON API + dashboard route + /about
  config.py        Settings (env-driven)
  db.py            SQLAlchemy engine
  models.py        SQLModel classes mirroring the migrations
  normalize.py     Pure functions: source records → events, classification, 8-K body parsing
  sources/         External-API clients
    courtlistener.py  CL REST v4, rate-limit-aware retries
    edgar.py          SEC EDGAR EFTS + 8-K body fetching
  ingest.py        CL ingestion CLI (--use-watermark, --skip-individual, --filed-after/before)
  ingest_edgar.py  EDGAR ingestion CLI
  clustering.py    Corporate-group detection
  crosscheck.py    EDGAR ↔ CL linking (case-number primary, name+date fallback)
  watermark.py     Persistent polling cursor with per-source overlap window
  alerts.py        Webhook delivery
  templates/       Dashboard HTML + /about page

migrations/        Versioned SQL migrations (001, 002, 003)
scripts/           Operational helpers (check_db, renormalize, reparse_edgar_bodies, fetch_cl_for_edgar, coverage_check, ...)
tests/             Test suite (pytest)
components/        Per-component explainer docs
```

## What's in scope, what isn't

In scope for the pilot, all working:

- Two sources (CourtListener, EDGAR), normalization, classification with calibrated confidence
- 8-K body parsing for chapter / court / case number
- Cross-source matching (case-number fast-path + name-token fallback)
- Corporate-group clustering
- Persistent polling watermark with late-arrival overlap window
- Data-quality guards (filters PACER bulk placeholders, rejects implausible future filing dates)
- JSON API with filtering, pagination, dashboard, alerts
- Test coverage on the classifier

Deliberately not built for the pilot (explained in `DECISIONS.md`):

- News-based third source (GDELT / Reuters) for fastest detection on big names
- Cloud Run / scheduled deployment — the brief asks for a locally-runnable pilot, and demos cleaner on screenshare than against a cold-starting hosted instance. The deployment story (Cloud Run job + Cloud Scheduler reading the watermark) is documented in DECISIONS §4.4 as the production path.
- Full nationwide Chapter 7 backfill — bounded by CourtListener's 125 requests/day rate limit; achievable only across multiple calendar days or with a Free Law Project membership for a higher quota. Documented as a measured constraint in DECISIONS §1.6.

## Where to read next

- **[`SCHEMA.md`](SCHEMA.md)** — data model + extension story for non-US jurisdictions.
- **[`components/`](components/)** — practical explainers per component (data collection, API, dashboard, alerts).
- **[`progress.md`](progress.md)** — current development status and presentation talking points.
