# Schema

The data model behind the bankruptcy detection pilot. Three tables: `bankruptcy_event`, `debtor`, `alert_delivery`. Postgres-flavored (Supabase). Migration in [`migrations/001_initial_schema.sql`](migrations/001_initial_schema.sql).

## Design principles

1. **Source-agnostic core, source-specific sidecar.** The columns at the top level of `bankruptcy_event` are abstract — they apply to any jurisdiction. Country-specific fields go into the `jurisdiction_specific` JSONB column. This is the extension story for non-US jurisdictions.
2. **One event = one filing from one source.** Cross-source matches (CourtListener + EDGAR for the same case) and parallel filings (`NodesNow LLC` + `NodesNow Inc.`) are linked via `related_filing_group_id`, not collapsed into one row.
3. **Encode messy decisions as data, not code.** The business-vs-consumer call is a classification, not a hard filter. We store `debtor_classification` + `classification_confidence` + `classification_method` so the API can return calibrated results and the dashboard can render uncertainty.
4. **Keep the raw source payload forever.** `raw` JSONB column is cheap; it lets us reprocess past events when normalization logic improves.
5. **Separate three timestamps.** `filed_at`, `source_first_seen_at`, `ingested_at` — each tells a different latency story. Together they prove our latency claims rather than asserting them.

---

## Table: `bankruptcy_event`

The core event record. One row per (source, source_record_id) pair.

| Column | Type | Why |
|---|---|---|
| `event_id` | UUID PK | Internal stable ID. Generated in code (not DB) so the alerter can reference it pre-insert. |
| `source` | TEXT | `'courtlistener'`, `'edgar'`, `'news'`. Identifies origin. |
| `source_record_id` | TEXT | Source's own ID, e.g. CourtListener `docket_id`. UNIQUE with `source`. Enables idempotent upsert. |
| `source_url` | TEXT | Back-link to the source page. Demoability + auditability. |
| `jurisdiction_country` | CHAR(2) | ISO 3166 country code. `'US'` today. The abstract field that makes extension possible. |
| `jurisdiction_court_id` | TEXT | Court ID within the country. `'deb'` for D. Delaware, etc. |
| `jurisdiction_court_name` | TEXT | Human-readable court name, denormalized for query convenience. |
| `proceeding_type` | TEXT | Enum-ish, validated by CHECK. `'chapter_7'`, `'chapter_11'`, `'chapter_13'`, `'chapter_15'`, `'uk_administration'`, `'uk_liquidation'`, `'uk_cva'`, `'eu_insolvency'`, `'other'`. |
| `case_number` | TEXT | Court-assigned case number, e.g. `'26-10653'`. |
| `pacer_case_id` | TEXT | US-specific identifier. Lives at top level (instead of in `jurisdiction_specific`) because it's queried often enough to warrant the convenience. Tradeoff to call out in the deck. Nullable. |
| `filed_at` | DATE | Legal filing date. |
| `source_first_seen_at` | TIMESTAMPTZ | When the source first published the record (e.g. CourtListener `date_created`). Approximates source-side lag from PACER. |
| `ingested_at` | TIMESTAMPTZ | When *we* ingested it. Approximates pipeline lag from source. |
| `status` | TEXT | `'filed' \| 'dismissed' \| 'confirmed' \| 'closed' \| 'converted' \| 'unknown'`. Updates as case progresses. |
| `status_updated_at` | TIMESTAMPTZ | When status last changed. |
| `debtor_classification` | TEXT | `'business' \| 'individual' \| 'unknown'`. The filtering decision. |
| `classification_confidence` | REAL | 0..1. How sure we are. The API can filter on `>= 0.7`, the dashboard can shade rows by confidence. |
| `classification_method` | TEXT | `'name_suffix' \| 'docket_fingerprint' \| 'cross_check' \| 'manual'`. Auditability. |
| `related_filing_group_id` | UUID | Nullable. Joint petitions (NodesNow LLC + NodesNow Inc.) share a group ID. |
| `jurisdiction_specific` | JSONB | Sidecar for country-specific fields. See "Jurisdiction-specific shapes" below. |
| `raw` | JSONB | Original source payload, untouched. |
| `created_at`, `updated_at` | TIMESTAMPTZ | Standard. `updated_at` maintained by trigger. |

**Constraints:** UNIQUE (`source`, `source_record_id`), CHECK constraints on enums, CHECK on `classification_confidence` range, CHECK on `jurisdiction_country` format.

**Indexes:** `filed_at DESC`, `jurisdiction_court_id`, `proceeding_type`, `debtor_classification`, partial index on `related_filing_group_id`, `ingested_at DESC`.

---

## Table: `debtor`

N debtors per event. Most events have one; joint petitions can have many.

| Column | Type | Why |
|---|---|---|
| `debtor_id` | UUID PK | Internal stable ID. |
| `event_id` | UUID FK → `bankruptcy_event` | ON DELETE CASCADE. |
| `name` | TEXT | Raw, as it appeared in the source. For display. |
| `normalized_name` | TEXT | Lowercased, suffixes stripped, punctuation normalized. For search. |
| `entity_type` | TEXT | `'llc' \| 'inc' \| 'corp' \| 'lp' \| 'individual' \| ...`. Extracted from name + heuristics. |
| `identifiers` | JSONB | `{ ein, ticker, lei, cik }`. Populated where known; sparse. |
| `address` | JSONB | `{ street, city, state, zip, country }`. Nullable. |
| `role` | TEXT | `'primary' \| 'co_debtor' \| 'affiliate'`. Distinguishes the lead debtor in joint filings. |

**Indexes:** `event_id`, `normalized_name`, GIN trigram index on `normalized_name` for fast `ILIKE '%acme%'` queries on the API.

---

## Table: `alert_delivery`

Audit log of webhook deliveries. Cheap insurance for the "how do you handle delivery failures?" question.

| Column | Type | Why |
|---|---|---|
| `delivery_id` | UUID PK | Internal ID. |
| `event_id` | UUID FK → `bankruptcy_event` | ON DELETE CASCADE. |
| `webhook_url` | TEXT | Where we tried to send it. |
| `attempted_at` | TIMESTAMPTZ | When we tried. |
| `delivered_at` | TIMESTAMPTZ | Nullable until success. NULL means pending/failed. |
| `http_status` | INT | The webhook's HTTP response, if we got one. |
| `retry_count` | INT | How many retries so far. |
| `last_error` | TEXT | Error message if any. |

**Indexes:** `event_id`, partial index on `attempted_at WHERE delivered_at IS NULL` for the retry worker.

---

## Source field mappings

How records from each source land in the schema.

### CourtListener `/api/rest/v4/search/?type=r`

| Source field | Schema target |
|---|---|
| `docket_id` | `bankruptcy_event.source_record_id` (with `source='courtlistener'`) |
| `docket_absolute_url` | `bankruptcy_event.source_url` (prefix with `https://www.courtlistener.com`) |
| `court_id` | `bankruptcy_event.jurisdiction_court_id`; sets `jurisdiction_country='US'` |
| `court` | `bankruptcy_event.jurisdiction_court_name` |
| `chapter` | mapped to `bankruptcy_event.proceeding_type` (`'7'` → `'chapter_7'`, `'11'` → `'chapter_11'`) |
| `docketNumber` | `bankruptcy_event.case_number` |
| `pacer_case_id` | `bankruptcy_event.pacer_case_id` |
| `dateFiled` | `bankruptcy_event.filed_at` |
| `meta.date_created` | `bankruptcy_event.source_first_seen_at` |
| `caseName`, `party[]` | one or more `debtor.name` rows; first → `role='primary'`, rest → `'co_debtor'` |
| `assignedTo` | `bankruptcy_event.jurisdiction_specific.judge` |
| `trustee_str` | `bankruptcy_event.jurisdiction_specific.trustee` |
| `recap_documents[]` | `bankruptcy_event.jurisdiction_specific.docket_entries` (also drives classification) |
| entire response | `bankruptcy_event.raw` |

### SEC EDGAR 8-K Item 1.03

| Source field | Schema target |
|---|---|
| accession number | `bankruptcy_event.source_record_id` (with `source='edgar'`) |
| filing index URL | `bankruptcy_event.source_url` |
| companyName | `debtor.name` (typically single primary debtor) |
| ticker, CIK | `debtor.identifiers.{ticker, cik}` |
| filingDate | `bankruptcy_event.filed_at` (NB: this is the *announcement* date, not necessarily the court filing date) |
| derived from filing body | `bankruptcy_event.proceeding_type` (must be parsed; usually Ch 11) |
| derived from filing body | `bankruptcy_event.jurisdiction_court_id` (named in the filing if known; may be NULL if EDGAR precedes court docket) |
| `jurisdiction_country` | `'US'` (US-listed companies) |
| entire response | `bankruptcy_event.raw` |

**Important nuance:** an EDGAR 8-K Item 1.03 can arrive *before* the corresponding CourtListener docket exists (the 4-business-day disclosure rule means companies announce as soon as they file; CourtListener depends on PACER ingestion which can lag). The schema handles this gracefully — `case_number` and `jurisdiction_court_id` are nullable on the EDGAR row, and the cross-check process backfills them when CourtListener catches up. The `related_filing_group_id` then links the EDGAR row to the CourtListener row as the same corporate event.

---

## Jurisdiction-specific shapes (`jurisdiction_specific` JSONB)

The sidecar that makes extension to other jurisdictions painless.

### US

```json
{
  "judge": "Mary F. Walrath",
  "trustee": "Alfred T. Giuliano",
  "dip_status": "debtor_in_possession",
  "voluntary": true,
  "joint_petition": false,
  "docket_entries": [
    {"date": "2026-05-03", "description": "Voluntary Petition (Chapter 11)", "doc_id": "..."},
    {"date": "2026-05-04", "description": "Hearing - Notice", "doc_id": "..."}
  ],
  "schedules_filed": false,
  "first_meeting_341_date": null
}
```

### UK (sketch — not implemented)

```json
{
  "administrator": "Joe Smith",
  "insolvency_practitioner_firm": "FRP Advisory",
  "company_number": "12345678",
  "registered_office": "...",
  "appointment_type": "out_of_court",
  "gazette_notice_url": "https://www.thegazette.co.uk/notice/..."
}
```

`proceeding_type` would be `'uk_administration'` | `'uk_liquidation'` | `'uk_cva'`. Source: The Gazette + Companies House. `jurisdiction_country='GB'`.

### EU member states (sketch)

`proceeding_type` uses prefixes per ISO country code: `'de_insolvenz'`, `'fr_redressement_judiciaire'`, `'fr_liquidation_judiciaire'`, `'es_concurso'`, etc. Each would have its own `jurisdiction_specific` shape, with sources from national gazettes (Bundesanzeiger, BODACC, BORME, etc.).

---

## Classification scoring

The business-vs-consumer call. Today's heuristic, in order of strength:

| Method | Confidence boost | Notes |
|---|---|---|
| `name_suffix` | base 0.7 | Name ends in LLC/Inc/Corp/Corporation/LP/LLP/PLLC/PC/Ltd. |
| `docket_fingerprint` | up to +0.2 | Negative signals like "Certificate of Credit Counseling" or Form 2030 strongly indicate individual; their absence on a Ch 7/11 supports business. |
| `chapter` | up to +0.05 | Ch 11 leans corporate (individuals do file Ch 11, but rarely). Ch 7 is split. Ch 13 is individual-only — auto-disqualifies. |
| `cross_check` | sets to 1.0 | If matched against an EDGAR 8-K Item 1.03, the entity is by definition a public company → `business`. |
| `manual` | sets to 1.0 | Operator override. |

The API exposes `?min_confidence=0.7` so consumers can dial up precision at the cost of recall.

---

## Open design tradeoffs (to call out in the deck)

1. **`pacer_case_id` at top level vs. inside `jurisdiction_specific`.** Strict purity says it belongs in the sidecar; convenience says top level. Picked top-level for query ergonomics.
2. **Court table not normalized.** 95 US bankruptcy courts; we store `jurisdiction_court_id` + `jurisdiction_court_name` on every row instead of a separate `court` table. Trades ~50 bytes/row of redundancy for simpler queries.
3. **Single migration file, no Alembic.** Pilot scope. Production would use Alembic for versioning; for now the migration directory is plain SQL files numbered in order.
4. **`raw` JSONB on the event vs. separate `source_record` table.** Inline now. Move to its own table if we ever need multiple raw payloads per event (e.g. tracking source updates over time).
