# Data collection

How filings get from external sources into our database. Two source pipelines (CourtListener + EDGAR) feed a normalizer + classifier, then post-passes for clustering and cross-source linking. All of it is in `bankruptcy/`.

## CourtListener client
- **State:** working
- **What it does:** Pulls bankruptcy docket records from CourtListener — every Chapter 7 and Chapter 11 case filed in any US federal bankruptcy court. Used as the **broad lane**: covers all 95 courts and includes both public and private companies, but lags the actual court filing by hours-to-day. This is where most of our events come from.
- **Where:** `bankruptcy/sources/courtlistener.py`

## EDGAR client
- **State:** working
- **What it does:** Pulls bankruptcy disclosures filed by public companies — specifically 8-K filings with Item 1.03, which the SEC requires within 4 business days of a public company filing for bankruptcy. Used as the **fast lane**: lower latency than CourtListener and high precision (only confirmed public-company bankruptcies), but limited to publicly-traded debtors.
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
- **What it does:** The actual entry points for fetching data. Run on a schedule (or manually) and they pull new filings from CourtListener and EDGAR, normalize them, classify them, and write them to the database. Idempotent — running the same window twice doesn't duplicate anything.
- **Where:** `bankruptcy/ingest.py`, `bankruptcy/ingest_edgar.py`

## Clustering
- **State:** working
- **What it does:** Detects when many separate court filings belong to the same parent company. A big bankruptcy like QVC shows up as 17 individual dockets in CourtListener; this pass links them into one logical "corporate filing" so the dashboard and API can show *QVC + 16 subsidiaries* rather than 17 unrelated rows. Currently finds **27 corporate groups** in our data, including one Delaware filing with 52 subsidiary entities.
- **Where:** `bankruptcy/clustering.py`

## Cross-source matching
- **State:** working
- **What it does:** When a public company files for bankruptcy it appears in both EDGAR (the 8-K disclosure) and CourtListener (the court docket). This pass links those two records together so a user can see *this is the SEC-confirmed version of the QVC bankruptcy*. Also bumps the classification confidence to 1.0 — an SEC filing is near-definitive evidence the debtor is a business — and copies the company's stock ticker and CIK identifier into the CourtListener record.
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
- **Persistent polling watermark.** Today `--filed-after` is a manual flag. Steady-state polling would benefit from a watermark stored in the DB so the worker resumes from the last successful poll automatically.
- **8-K body parsing.** EDGAR events default to `proceeding_type='chapter_11'` because we don't parse the 8-K body for the actual chapter. Right answer for production; out of scope for the pilot.
- **No third source.** News (GDELT / Reuters) would catch high-profile filings before either CL or EDGAR sees them. Not yet built.
