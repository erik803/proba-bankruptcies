# API

JSON HTTP service for querying ingested events. FastAPI app at `bankruptcy/api.py`. Run with `uvicorn bankruptcy.api:app --reload`.

## GET /bankruptcies
- **State:** working
- **What it does:** Returns a list of bankruptcy events matching a set of filters — search by company name, date range, court, chapter, business-vs-individual classification, minimum confidence, or corporate-group ID. Paginated. Each row carries derived `group_size` (how many entities are in this row's corporate group) and `cross_source_confirmed` (true when the group has an EDGAR confirmation) so a client can show "part of a 52-entity filing, EDGAR-confirmed" without a second call. Returns a slim `EventSummary` shape — the heavy `jurisdiction_specific` JSONB is omitted to keep list payloads in the tens of kilobytes instead of megabytes.
- **Where:** `bankruptcy/api.py`

## GET /bankruptcies/{event_id}
- **State:** working
- **What it does:** Returns the full `EventResponse` for one event — case number, court, filing date, every debtor on the case (a corporate filing can have many), stock ticker and CIK if it's a public company, the linked corporate group, plus the `jurisdiction_specific` JSONB sidecar with docket entries, judge, joint-administration flag, etc. Use this for the detail view; use `GET /bankruptcies` for the list / summary view.
- **Where:** `bankruptcy/api.py`

## GET /healthz
- **State:** working
- **What it does:** Health check. Confirms the app is running and the database is reachable. Cloud Run pings this to know whether to keep the instance alive.
- **Where:** `bankruptcy/api.py`

## GET /
- **State:** working
- **What it does:** Serves the dashboard HTML — the human-friendly visualization over the same data the API exposes. Same FastAPI app, separate concern. See `components/dashboard.md`.
- **Where:** `bankruptcy/api.py`

## Auth
- **State:** not implemented
- **What it does:** Anyone who can reach the URL can query it. Fine for a take-home reviewer. A customer-facing version would gate access behind per-customer API keys and rate-limit each key separately.

## Tests
- **State:** not implemented
- **What it does:** There's no automated test suite for the endpoints — filters were hand-checked once against a 28-event dataset back in Phase 2. Worth a smoke test now that the data is 25× bigger; that's the obvious next thing for this component.

## Known gaps
- **OpenAPI schema is loose.** FastAPI auto-generates `/docs` and `/openapi.json`, but the response models are defined inline rather than via Pydantic schemas — the docs work but the typing isn't formally pinned.
- **No response envelope.** Endpoints return raw arrays / objects. A production API would wrap responses in `{ "data": ..., "meta": { "total": ..., "page": ... } }` so pagination metadata is consistent across endpoints.
