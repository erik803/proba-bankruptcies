# Bankruptcy Detection Pilot — Progress

Take-home for Veridion. Tracks where we are across the phases laid out at the start.

**Timeline:** Started Thursday 2026-05-07. Targeting delivery Monday 2026-05-11 or Tuesday 2026-05-12 (~4–5 days of work). User confirmed Veridion is fine with any timeline as long as they're notified.

## Phase 0 — Setup
- [x] CourtListener API token obtained (stored in `.env`)
- [x] `.env` created (`.env.example` removed — README documents the variables)
- [x] `.gitignore` configured (Python + .env + local DB)
- [x] Stack decision finalized — see Stack section below
- [x] Supabase project created (URL: `https://bsthilbuoydofvgbkgtl.supabase.co`, region: `aws-1-eu-west-2`)
- [x] Supabase Postgres connection string added to `.env` as `DATABASE_URL`
- [x] GitHub repo created: `https://github.com/erik803/proba-bankruptcies` (no commits pushed yet)
- [x] LICENSE file added (MIT, copyright Ernst Francisc-Erik)
- [ ] GCP project created (for Cloud Run deploys later — defer until ready to deploy)
- [x] Project scaffolded (folder structure, `pyproject.toml` written, `docker-compose.yml` for local dev, README, `bankruptcy/` package with config + db + models, `scripts/check_db.py` sanity script)
- [x] Local git repo initialized and remote configured (`origin` → erik803/proba-bankruptcies)
- [x] Dependencies installed in `.venv` (Python 3.13.13, fastapi 0.136, sqlmodel 0.0.38, psycopg 3.3.4)
- [x] Sanity check passes — DB connection OK, three tables visible
- [x] Commits pushed to GitHub (3 commits: scaffold, working pilot, clustering)

## Phase 1 — Schema design
- [x] `SCHEMA.md` written (event model, debtor model, jurisdiction-specific sidecar)
- [x] Migration SQL written (`migrations/001_initial_schema.sql`)
- [x] Source field mappings documented (CourtListener + EDGAR)
- [x] Extension story for non-US jurisdictions documented (UK + EU sketches)
- [x] Migration applied to Supabase Postgres
- [x] SQLModel types implemented in code (`bankruptcy/models.py`)
- [ ] Schema validated against 5–10 real CourtListener records end-to-end

## Phase 2 — Vertical slice (end-to-end on a single court)
- [x] Foundation: package structure, config, DB engine, models, sanity script
- [x] CourtListener client (`bankruptcy/sources/courtlistener.py`) — async httpx wrapper, token auth, cursor pagination, retry on 429/5xx via tenacity
- [x] Normalizer (`bankruptcy/normalize.py`) — pure functions mapping CourtListener result → `BankruptcyEvent` + `Debtor`, with entity-type suffix detection and classification scoring
- [x] Ingestion CLI: `python -m bankruptcy.ingest` — argparse with `--court`, `--chapter`, `--max-per-combo`, `--dry-run`; idempotent via `(source, source_record_id)` UNIQUE
- [x] First successful ingest: 25 Delaware Ch 11 filings into Supabase, 24/25 correctly classified as business, 0 errors
- [x] Inspection scripts: `scripts/inspect_recent.py`, `scripts/debug_one.py`
- [x] JSON API (`bankruptcy/api.py`): `GET /bankruptcies` with `company`, `from`, `to`, `court`, `proceeding_type`, `classification`, `min_confidence`, `limit`, `offset` filters; `GET /bankruptcies/{event_id}`; `GET /healthz`
- [x] Dashboard: server-rendered shell + Chart.js, fetches `/bankruptcies` client-side for the table; summary cards, by-day chart, by-court chart, filterable
- [x] Alert webhook delivery (`bankruptcy/alerts.py`): POSTs structured JSON, records every attempt in `alert_delivery` (success or failure), fired automatically from ingest when `ALERT_WEBHOOK_URL` is set
- [x] End-to-end verified: 28 real events (25 Ch 11 + 3 Ch 7) ingested into Supabase, 3 alerts delivered to httpbin with HTTP 200, classifications calibrated (24 business, 0 individual, 4 unknown including Keysha J. Johnson and NB Element DTS — conservative by design)

## Phase 3 — Messy parts
- [x] Business-vs-consumer filter (entity-name regex + docket-entry fingerprint + confidence score) — implemented in normalize.classify_debtor; calibrated 39% business / 31% individual / 30% unknown on a 208-event sample
- [x] Related-filing-group clustering (`bankruptcy/clustering.py`) — two-pass: explicit `joint_administration` flag from CourtListener (zero false positives), then consecutive-case-number runs among business-classified events. Surfaced 10 corporate groups including QVC (17 entities), Impac/Copperfield/Synergy (12), Finch (4)
- [x] Re-normalization pass (`scripts/renormalize.py`) — re-runs the normalizer on stored `raw` payloads. Used to retrofit the HTML-stripping fix without re-fetching from CourtListener
- [x] Backfill: 208 events across 4 courts (deb, nysb, txsb, cacb) and chapters 7+11
- [x] EDGAR 8-K Item 1.03 cross-check as second source — `bankruptcy/sources/edgar.py` queries the EFTS API for 8-Ks with Item 1.03 in a date window; `bankruptcy/normalize.normalize_edgar_filing` produces source='edgar' events; `bankruptcy/ingest_edgar.py` is the CLI
- [x] Cross-check pass — `bankruptcy/crosscheck.py` matches EDGAR events to CourtListener events by date proximity (±14 days) + name-token containment (>= 1.0). On match: links into shared `related_filing_group_id`, boosts CL classification_confidence to 1.0 with method='cross_check', backfills EDGAR's court_id from the matched CL docket, copies CIK/ticker into the CL primary debtor's identifiers
- [x] Migration 002: `jurisdiction_court_id` made nullable to support EDGAR (no court info until cross-checked)
- [x] First successful cross-check: 7 EDGAR events ingested for 2026-04-01..2026-05-07; 2 of them (QVC Group + QVC INC) matched all 17 of CL's QVC corporate group → 19 events in one related_filing_group_id
- [ ] Tradeoffs documented in README

## Phase 4 — Deck + README
- [ ] README: setup, run, sample queries
- [ ] Slide 1: problem & why it's hard
- [ ] Slide 2: source landscape & why CourtListener
- [ ] Slide 3: schema & extension story
- [ ] Slide 4: tradeoffs (latency / completeness / accuracy)
- [ ] Slide 5: live demo
- [ ] Slide 6: what we'd do with more time

---

## Stack (working decisions)

| Layer | Choice | Status |
|-------|--------|--------|
| Language | Python 3.11+ | confirmed |
| API framework | FastAPI | confirmed |
| ORM | SQLModel (sits on SQLAlchemy + Pydantic) | confirmed |
| Database | Supabase Postgres for deployed; docker-compose Postgres for local dev (SQLite path dropped — schema uses JSONB / pg_trgm / TIMESTAMPTZ) | confirmed |
| Dashboard | FastAPI-served HTML + Chart.js, same Cloud Run service as the API | confirmed |
| Hosting | Google Cloud Run (API+dashboard service); Cloud Scheduler → Cloud Run `/ingest` endpoint for polling | confirmed |
| Alerts | App-level webhook POST on insert (fires from ingestion code, not a DB trigger) | confirmed |
| Repo | GitHub, MIT license | confirmed |

## Open questions / decisions to lock
- Cold-start mitigation on Cloud Run — `min-instances=1` is acceptable; user OK with the small extra cost.
- Polling cadence — every 15 min vs hourly. CourtListener latency is hours-to-day, so hourly is the right default; can dial up if needed.
- Whether to backfill the demo dataset from CourtListener (~30 days, 2–3 courts) or use a curated snapshot file checked into the repo for the reviewer.

## Scheduled follow-ups
- **~2026-05-15 (Fri) or later:** re-run `python -m bankruptcy.ingest_edgar --start 2026-05-11 --end 2026-05-16` and then `python -m bankruptcy.crosscheck`. Spanish Broadcasting System filed Ch 11 in Delaware on 2026-05-11 with 52 subsidiary entities — captured in CourtListener already, but the SEC 8-K Item 1.03 disclosure deadline is 4 business days (Fri 2026-05-15). When the 8-K lands in EDGAR, the cross-check pass should auto-link it into the existing 52-entity SBS group, demonstrating the fast-lane/broad-lane pattern end-to-end on a fresh major filing.

## Notes & decisions log

See `DECISIONS.md` for design rationale (gitignored personal study aid; defers to that file as the single source of truth).
