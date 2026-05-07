-- 002_jurisdiction_court_id_nullable.sql
-- Allow bankruptcy_event.jurisdiction_court_id to be NULL.
--
-- EDGAR 8-K Item 1.03 filings disclose a bankruptcy event without naming
-- the bankruptcy court (we'd need to parse the 8-K body to extract it).
-- The cross-check pass backfills the court id when a CourtListener docket
-- for the same company turns up. Until then, NULL is the honest value.
--
-- The original NOT NULL was correct for CourtListener-only data; relaxing
-- it now that we have multiple sources is the right design call.

ALTER TABLE bankruptcy_event
    ALTER COLUMN jurisdiction_court_id DROP NOT NULL;
