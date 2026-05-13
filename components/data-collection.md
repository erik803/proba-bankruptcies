# Data collection

How filings get from external sources into our database. Two source pipelines (CourtListener + EDGAR) feed a normalizer + classifier, then post-passes for clustering and cross-source linking. All of it is in `bankruptcy/`.

## CourtListener client
- **State:** working
- **What it does:** Pulls bankruptcy docket records from CourtListener — every Chapter 7 and Chapter 11 case filed in any US federal bankruptcy court. Used as the **broad lane**: covers all 95 courts and includes both public and private companies, but lags the actual court filing by hours-to-day. This is where most of our events come from.
- **Where:** `bankruptcy/sources/courtlistener.py`

## EDGAR client
- **State:** working
- **What it does:** Pulls bankruptcy disclosures filed by public companies — specifically 8-K filings with Item 1.03, which the SEC requires within 4 business days of a public company filing for bankruptcy. Used as the **fast lane**: lower latency than CourtListener and high precision (only confirmed public-company bankruptcies), but limited to publicly-traded debtors. Also fetches the full 8-K HTML body so the normalizer can extract the actual proceeding type (Chapter 7/11/13/15 or state-law) rather than defaulting blindly.
- **Where:** `bankruptcy/sources/edgar.py`

## Normalizer
- **State:** working
- **What it does:** Turns a raw CourtListener docket or EDGAR 8-K into our internal event shape — pulling out the debtor's name, the court, the case number, the chapter, the filing date, and an initial business-vs-individual call. Cleans up the messy real-world cases along the way: HTML-wrapped names, joint-administration parenthetical annotations, orphaned open-parens left by upstream truncations, and weird entity-suffix formatting. This is what makes a "source record" actually usable.
- **Where:** `bankruptcy/normalize.py`

## Classification
- **State:** working
- **What it does:** Decides whether each debtor is a business, an individual, or unknown. This is the central filtering decision for Veridion's use case — we want business bankruptcies, not consumer ones. Stores the call as **data** (value + confidence + which signal fired) rather than as a hard filter, so the API can return calibrated results and the dashboard can surface uncertainty.
- **Where:** `bankruptcy/normalize.py::classify_debtor`

## Ingestion CLIs
- **State:** working
- **What it does:** The actual entry points for fetching data. Run on a schedule (or manually) and they pull new filings from CourtListener and EDGAR, normalize them, classify them, and write them to the database. Idempotent — running the same window twice doesn't duplicate anything. The `--use-watermark` flag (opt-in for now) resumes each source from where it last left off using the `ingest_watermark` table; manual `--filed-after` / `--start` flags still work for backfills. The `--skip-individual` flag drops events that are obviously consumer (classification `individual`, or classification `unknown` with no corporate name suffix) — used for the nationwide Chapter 7 backfill so we capture business cases without inserting the ~99% consumer noise.
- **Where:** `bankruptcy/ingest.py`, `bankruptcy/ingest_edgar.py`

## CL rate-limit handling
- **State:** working
- **What it does:** CourtListener's documented quota is 5 req/min and 50 req/hour. The client paces page-fetches via `INTER_PAGE_SLEEP_S` (default 13s, overridable via the `CL_INTER_PAGE_SLEEP_S` env var — used at 75s for the long Ch 7 nationwide backfill to stay under the hourly cap). On a 429 response, the retry block honors the server's `Retry-After` header verbatim; without that header, it backs off starting at 60s (the per-minute window length) up to 300s, for 10 attempts. Long-running ingests log every retry decision and a progress line every 200 events processed, so they have signs of life during multi-hour runs.
- **Where:** `bankruptcy/sources/courtlistener.py`

## Data-quality guards at the normalizer
- **State:** working
- **What it does:** Rejects two known classes of CourtListener garbage at ingest time, before they reach the database. (1) Rows where `caseName == "Miscellaneous Entry"` &mdash; PACER bulk-entry placeholders, not real bankruptcies (40 of these landed in a single ingest, all dated `2029-01-01`, all named identically). (2) Rows where `filed_at` is implausibly far in the future &mdash; clear data-entry typos at the court level (we saw a `2079-11-23` filing date on a real 2021 docket). The watermark itself is also clamped to "today" at write time, so even if a bad row sneaks through, it can't poison the next poll.
- **Where:** `bankruptcy/normalize.py::normalize_courtlistener_result`, `bankruptcy/watermark.py::update_watermark`

## Targeted CL docket fetch (for EDGAR-only events)
- **State:** working
- **What it does:** For EDGAR events whose 8-K body gave us a court + case number, but whose CourtListener counterpart isn't in our DB (usually because the underlying bankruptcy filed before our backfill window), this script does a targeted CL search and ingests the specific docket. Cheap: one CL search per missing event, no pagination. After it runs, cross-check links the two rows via the case-number fast-path. Empirically, this is how we connect 8-Ks that disclose old bankruptcies (e.g. an April 2026 disclosing 8-K for a December 2025 docket).
- **Where:** `scripts/fetch_cl_for_edgar.py`

## Polling watermark
- **State:** working
- **What it does:** Remembers where each source's last successful poll left off, so a scheduled job can resume incrementally instead of re-scanning history. One row per source in `ingest_watermark` (CL + EDGAR). On each watermark-mode run: read the high-watermark, compute `filed_after = last_event_date - lookback_days` (7 days for CL to catch PACER backfills, 2 for EDGAR), run ingest, write the new high-watermark on success. Late-arriving filings are caught by the lookback window; duplicates are skipped by the existing UNIQUE constraint, not deduplicated by the watermark.
- **Where:** `bankruptcy/watermark.py`, `migrations/003_ingest_watermark.sql`

## 8-K body parser
- **State:** working
- **What it does:** Fetches the full HTML body of each 8-K Item 1.03 filing and pulls three things out of the prose:
  1. **Proceeding type** — chapter_7/11/13/15, or `other` for non-federal proceedings like state-law Assignments for the Benefit of Creditors that file under Item 1.03 because the SEC item covers "Bankruptcy *or Receivership*" broadly. Three rule tiers in priority order: strong patterns ("voluntary petition for relief under Chapter 11"), non-federal proceeding detection ("assignment for the benefit of creditors"), and a frequency fallback over bare "Chapter N" mentions. Defaults to `chapter_11` (confidence 0.0) when nothing matches.
  2. **Bankruptcy court** — matches phrases like "United States Bankruptcy Court for the Southern District of Texas" and translates the district phrase to a CourtListener court ID (`txsb`) via a static map covering ~30 venues. When the phrase matches but the district isn't in the map, the canonical court name is still returned so consumers see the venue.
  3. **Federal case number** — matches `"Case No. 25-90807"` / `"Case Number 26-10708"`. Federal bankruptcy case numbers are unique within a court, so once we have both, cross-check can match to CourtListener with primary-key precision (see `crosscheck.py` §8.0).
  All three are stored as event columns, with provenance (which rule fired, with what confidence) in `jurisdiction_specific`.
- **Where:** `bankruptcy/normalize.py::extract_proceeding_type_from_8k_body`, `bankruptcy/normalize.py::extract_court_and_case_from_8k_body`, `scripts/reparse_edgar_bodies.py`

## Clustering
- **State:** working
- **What it does:** Detects when many separate court filings belong to the same parent company. A big bankruptcy like QVC shows up as 17 individual dockets in CourtListener; this pass links them into one logical "corporate filing" so the dashboard and API can show *QVC + 16 subsidiaries* rather than 17 unrelated rows. Currently finds **27 corporate groups** in our data, including one Delaware filing with 52 subsidiary entities.
- **Where:** `bankruptcy/clustering.py`

## Cross-source matching
- **State:** working
- **What it does:** When a public company files for bankruptcy it appears in both EDGAR (the 8-K disclosure) and CourtListener (the court docket). This pass links those two records together so a user can see *this is the SEC-confirmed version of the QVC bankruptcy*. Two matching strategies in priority order: **(1) case-number lookup** — when the EDGAR row carries both `court_id` and `case_number` (extracted from the 8-K body, see "8-K body parser" below), match directly to the CL event with that `(court_id, case_number)` tuple. Skips any date window because the 8-K disclosure date can lag the actual docket by months (Luminar's 8-K filed April 2026 references a December 2025 docket). **(2) Name + date match** — the fallback containment-on-tokens logic with a ±14-day window, still useful for catching subsidiary dockets in a corporate group that don't appear in the parent's 8-K. On match, both passes link into a shared `related_filing_group_id`, boost classification_confidence to 1.0, backfill any missing fields between the two rows, and copy CIK/ticker identifiers.
- **Where:** `bankruptcy/crosscheck.py`

## Re-normalization
- **State:** working
- **What it does:** Lets us re-process every event already in the database when we improve the normalizer logic, without re-fetching from CourtListener or EDGAR. Computes new values in pure Python, then issues one bulk `UPDATE` per affected table — finishes the full 703-event dataset in ~1 second. Preserves cross-source classification upgrades (`cross_check`, `edgar_public_company`) so downstream passes don't get rolled back.
- **Where:** `scripts/renormalize.py`

## Inspection helpers
- **State:** working
- **What it does:** Tools for peeking at the data during development. `inspect_recent.py` shows what just landed. `debug_one.py` walks a single docket through the full normalize-and-classify pipeline. `coverage_check.py` measures what share of nationwide filings we've actually captured (and what kinds of debtors are in the courts we haven't backfilled).
- **Where:** `scripts/`

## Known gaps
- **Ch 7 coverage is sample-only, not exhaustive.** Total Ch 7 events: 253, of which 6 are classified business. A full nationwide backfill is infeasible on a free CourtListener account: their 125 requests/day cap is hard (we hit it empirically on 2026-05-13). At 125 pages/day = ~6,250 events/day, a 7-day Ch 7 window takes 2+ calendar days and a 30-day window takes a week. The production answer is a Free Law Project membership or paid PACER. See `DECISIONS.md §1.6`.
- **Watermark is opt-in.** `--use-watermark` works end-to-end but defaults to off so existing manual workflows keep working unchanged. When this graduates from pilot to scheduled production it should flip to opt-out — see `DECISIONS.md §1.7` and the presentation reminders in `progress.md`.
- **No third source.** News (GDELT / Reuters) would catch high-profile filings before either CL or EDGAR sees them. Not yet built — biggest remaining latency win on the table.
- **Cross-check doesn't reconcile chapter mismatches.** If the 8-K body parse says one chapter and the linked CourtListener docket says another, we keep both as-is. The CL value should win (court of record); needs a one-liner in `crosscheck.py`.
- **Court name + case number extraction is regex-based.** Works for the standard 8-K phrasing ("United States Bankruptcy Court for the Southern District of Texas" + "Case No. 26-90346") but misses unusual venues (Olenox's 8-K names "Eastern District of Oklahoma" without listing the case number) and multi-debtor 8-Ks (QVC's lists 17 case numbers; we grab the first). An LLM extractor would close these gaps.
- **Court-name → CL court_id map is hand-maintained.** ~30 venues covered. Could be bootstrapped from CL's `/api/rest/v4/courts/` endpoint instead.
