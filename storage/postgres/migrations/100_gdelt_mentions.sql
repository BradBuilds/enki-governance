-- Migration 100: gdelt_mentions + gdelt_sync_state
--
-- Stores geo-coded news mentions from the GDELT GKG 2.0 feed
-- (http://data.gdeltproject.org/gdeltv2/). One row per (record, location)
-- pair — a single article that mentions five places becomes five rows.
--
-- Retention is the worker's job (not enforced at the schema level): the
-- gdelt_gkg worker purges rows older than 48h on each cycle. The map
-- layer uses the last 24h by default. Skip the retention sweep and
-- the table grows unbounded — ~3-5M rows/day at GDELT's full rate.
--
-- The sync_state singleton tracks the last processed file URL so we
-- never re-ingest the same 15-min batch on worker restart.

BEGIN;

CREATE TABLE IF NOT EXISTS gdelt_mentions (
    mention_id      BIGSERIAL PRIMARY KEY,
    gkg_record_id   VARCHAR(128) NOT NULL,
    published_at    TIMESTAMPTZ NOT NULL,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    latitude        DOUBLE PRECISION NOT NULL,
    longitude       DOUBLE PRECISION NOT NULL,
    location_name   TEXT,
    location_type   SMALLINT,
    -- 1=COUNTRY  2=USSTATE  3=USCITY  4=WORLDCITY  5=WORLDSTATE
    country_code    VARCHAR(8),
    adm1_code       VARCHAR(16),
    feature_id      VARCHAR(64),
    themes          TEXT[],
    tone            NUMERIC(6,2),
    source_domain   TEXT,
    document_url    TEXT
);

-- bbox lookup: longitude leads because most map queries scan W↔E first.
CREATE INDEX IF NOT EXISTS idx_gdelt_mentions_bbox
    ON gdelt_mentions (longitude, latitude);

-- time-decay queries (last N hours)
CREATE INDEX IF NOT EXISTS idx_gdelt_mentions_pub
    ON gdelt_mentions (published_at DESC);

-- theme filtering (e.g. ONLY rows tagged TERROR or NATURAL_DISASTER)
CREATE INDEX IF NOT EXISTS idx_gdelt_mentions_themes
    ON gdelt_mentions USING GIN (themes);

-- per-country queries
CREATE INDEX IF NOT EXISTS idx_gdelt_mentions_country
    ON gdelt_mentions (country_code, published_at DESC)
    WHERE country_code IS NOT NULL;


-- Singleton sync state — last processed file URL + when + how many rows.
-- enforce singleton via CHECK (id = 1).
CREATE TABLE IF NOT EXISTS gdelt_sync_state (
    id                  SMALLINT PRIMARY KEY DEFAULT 1,
    last_processed_url  TEXT,
    last_processed_at   TIMESTAMPTZ,
    last_record_count   INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT,
    consecutive_errors  INTEGER NOT NULL DEFAULT 0,
    CHECK (id = 1)
);

-- Seed the singleton so UPSERTs don't have to first-check.
INSERT INTO gdelt_sync_state (id) VALUES (1)
ON CONFLICT (id) DO NOTHING;

COMMIT;

INSERT INTO schema_migrations (version, applied_at)
VALUES ('100_gdelt_mentions', NOW())
ON CONFLICT DO NOTHING;
