-- 001_initial_schema.sql
-- Initial schema for the bankruptcy detection pilot.
-- Target: PostgreSQL 13+ (Supabase). Apply via Supabase SQL editor or psql.

-- pg_trgm enables trigram indexes for fast ILIKE/substring search on debtor names.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ============================================================================
-- bankruptcy_event
-- One row per bankruptcy filing event from a single source.
-- A "corporate event" with multiple parallel filings (e.g. NodesNow LLC and
-- NodesNow Inc.) results in multiple rows linked via related_filing_group_id.
-- ============================================================================
CREATE TABLE bankruptcy_event (
    event_id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Provenance
    source                      TEXT        NOT NULL,
    source_record_id            TEXT        NOT NULL,
    source_url                  TEXT,

    -- Jurisdiction (abstract core, source-agnostic)
    jurisdiction_country        CHAR(2)     NOT NULL,
    jurisdiction_court_id       TEXT        NOT NULL,
    jurisdiction_court_name     TEXT,

    -- Proceeding
    proceeding_type             TEXT        NOT NULL,
    case_number                 TEXT,
    pacer_case_id               TEXT,

    -- Time
    filed_at                    DATE        NOT NULL,
    source_first_seen_at        TIMESTAMPTZ,
    ingested_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Lifecycle
    status                      TEXT        NOT NULL DEFAULT 'filed',
    status_updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Classification (encodes the messy business-vs-consumer judgment in data, not code)
    debtor_classification       TEXT        NOT NULL DEFAULT 'unknown',
    classification_confidence   REAL        NOT NULL DEFAULT 0.0,
    classification_method       TEXT,

    -- Grouping for joint/parallel filings
    related_filing_group_id     UUID,

    -- Extension surface: country-specific fields live here
    jurisdiction_specific       JSONB       NOT NULL DEFAULT '{}'::jsonb,

    -- Original source payload, kept verbatim for reprocessing
    raw                         JSONB       NOT NULL,

    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT bankruptcy_event_source_record_unique
        UNIQUE (source, source_record_id),
    CONSTRAINT bankruptcy_event_country_format
        CHECK (jurisdiction_country ~ '^[A-Z]{2}$'),
    CONSTRAINT bankruptcy_event_proceeding_type_known
        CHECK (proceeding_type IN (
            'chapter_7', 'chapter_11', 'chapter_13', 'chapter_15',
            'uk_administration', 'uk_liquidation', 'uk_cva',
            'eu_insolvency',
            'other'
        )),
    CONSTRAINT bankruptcy_event_status_known
        CHECK (status IN ('filed', 'dismissed', 'confirmed', 'closed', 'converted', 'unknown')),
    CONSTRAINT bankruptcy_event_classification_known
        CHECK (debtor_classification IN ('business', 'individual', 'unknown')),
    CONSTRAINT bankruptcy_event_confidence_range
        CHECK (classification_confidence >= 0.0 AND classification_confidence <= 1.0)
);

CREATE INDEX bankruptcy_event_filed_at_idx
    ON bankruptcy_event (filed_at DESC);
CREATE INDEX bankruptcy_event_court_idx
    ON bankruptcy_event (jurisdiction_court_id);
CREATE INDEX bankruptcy_event_proceeding_idx
    ON bankruptcy_event (proceeding_type);
CREATE INDEX bankruptcy_event_classification_idx
    ON bankruptcy_event (debtor_classification);
CREATE INDEX bankruptcy_event_group_idx
    ON bankruptcy_event (related_filing_group_id)
    WHERE related_filing_group_id IS NOT NULL;
CREATE INDEX bankruptcy_event_ingested_at_idx
    ON bankruptcy_event (ingested_at DESC);

-- ============================================================================
-- debtor
-- N debtors per event. Most events have 1; joint petitions can have many.
-- ============================================================================
CREATE TABLE debtor (
    debtor_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id            UUID NOT NULL REFERENCES bankruptcy_event(event_id) ON DELETE CASCADE,

    name                TEXT NOT NULL,
    normalized_name     TEXT NOT NULL,
    entity_type         TEXT NOT NULL DEFAULT 'unknown',
    identifiers         JSONB NOT NULL DEFAULT '{}'::jsonb,
    address             JSONB,
    role                TEXT NOT NULL DEFAULT 'primary',

    CONSTRAINT debtor_role_known
        CHECK (role IN ('primary', 'co_debtor', 'affiliate')),
    CONSTRAINT debtor_entity_type_known
        CHECK (entity_type IN (
            'llc', 'inc', 'corp', 'corporation', 'co',
            'lp', 'llp', 'pllc', 'pc', 'ltd',
            'partnership', 'trust', 'sole_proprietorship',
            'individual', 'unknown'
        ))
);

CREATE INDEX debtor_event_id_idx
    ON debtor (event_id);
CREATE INDEX debtor_normalized_name_idx
    ON debtor (normalized_name);
-- Trigram index for fast `WHERE normalized_name ILIKE '%acme%'` on the API
CREATE INDEX debtor_normalized_name_trgm_idx
    ON debtor USING gin (normalized_name gin_trgm_ops);

-- ============================================================================
-- alert_delivery
-- Audit log of webhook deliveries triggered by new events.
-- Lets us answer "did the alert fire? did it succeed? are we retrying?"
-- ============================================================================
CREATE TABLE alert_delivery (
    delivery_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id            UUID NOT NULL REFERENCES bankruptcy_event(event_id) ON DELETE CASCADE,

    webhook_url         TEXT NOT NULL,
    attempted_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    delivered_at        TIMESTAMPTZ,
    http_status         INT,
    retry_count         INT NOT NULL DEFAULT 0,
    last_error          TEXT
);

CREATE INDEX alert_delivery_event_id_idx
    ON alert_delivery (event_id);
-- Partial index of pending (undelivered) alerts for the retry worker
CREATE INDEX alert_delivery_pending_idx
    ON alert_delivery (attempted_at)
    WHERE delivered_at IS NULL;

-- ============================================================================
-- updated_at trigger for bankruptcy_event
-- ============================================================================
CREATE OR REPLACE FUNCTION trigger_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER bankruptcy_event_set_updated_at
    BEFORE UPDATE ON bankruptcy_event
    FOR EACH ROW
    EXECUTE FUNCTION trigger_set_updated_at();
