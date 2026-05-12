-- 003_ingest_watermark.sql
-- Persistent high-watermark for incremental polling per source.
--
-- Each ingest CLI (CourtListener, EDGAR) writes one row keyed by source name.
-- On `--use-watermark`, the CLI reads `last_event_date`, queries
-- `filed_after = last_event_date - lookback_days`, and on success updates the
-- watermark to the new max `filed_at` seen.
--
-- The `lookback_days` overlap is per-source because CourtListener backfills
-- PACER over a window (we've seen up to ~3 days late), while EDGAR 8-Ks are
-- filed once and immutable. CL default = 7, EDGAR default = 2.
--
-- See DECISIONS.md §1.7 for the design rationale (high-watermark + overlap is
-- the standard industry pattern for incremental ETL from sources you don't
-- own).

CREATE TABLE ingest_watermark (
    source              TEXT        PRIMARY KEY,
    last_event_date     DATE        NOT NULL,
    last_run_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_run_status     TEXT        NOT NULL DEFAULT 'success',
    last_event_count    INT         NOT NULL DEFAULT 0,
    lookback_days       INT         NOT NULL DEFAULT 7,

    CONSTRAINT ingest_watermark_status_known
        CHECK (last_run_status IN ('success', 'failed', 'partial')),
    CONSTRAINT ingest_watermark_lookback_sane
        CHECK (lookback_days >= 0 AND lookback_days <= 90)
);
