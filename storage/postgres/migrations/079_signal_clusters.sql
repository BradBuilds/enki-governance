-- 079_signal_clusters.sql
-- Article-level deduplication. News signals from many RSS sources cover
-- the same story with slightly different titles. Without clustering, the
-- feed shows 6 copies of "Vale says clock is ticking for Iran" from
-- AP, Reuters, BBC, Axios, Fox, Al Jazeera as 6 unrelated items.
--
-- A signal_cluster is "one story with N articles from M sources".
-- The clusterer worker keys signals to clusters by entity-set + day-bucket
-- + key-word fingerprint. The feed UI shows clusters by default with
-- frequency (article count + source count) front and center.

CREATE TABLE IF NOT EXISTS signal_clusters (
    cluster_id           BIGSERIAL    PRIMARY KEY,
    tenant_id            VARCHAR(64)  NOT NULL DEFAULT 'default',
    canonical_signal_rid BIGINT       REFERENCES signals(rid),  -- representative article
    topic_summary        TEXT,                                  -- short story headline
    first_seen           TIMESTAMPTZ  NOT NULL,
    last_seen            TIMESTAMPTZ  NOT NULL,
    article_count        INT          NOT NULL DEFAULT 1,
    source_count         INT          NOT NULL DEFAULT 1,
    entity_ids           BIGINT[]     NOT NULL DEFAULT '{}',
    title_keywords       TEXT[],
    fingerprint_hash     TEXT,                                  -- exact-match shortcut
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_signal_clusters_fingerprint
    ON signal_clusters (fingerprint_hash) WHERE fingerprint_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_signal_clusters_last_seen
    ON signal_clusters (last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_signal_clusters_entities
    ON signal_clusters USING gin (entity_ids);
CREATE INDEX IF NOT EXISTS idx_signal_clusters_tenant_last
    ON signal_clusters (tenant_id, last_seen DESC);

ALTER TABLE signals
    ADD COLUMN IF NOT EXISTS cluster_id BIGINT REFERENCES signal_clusters(cluster_id);
CREATE INDEX IF NOT EXISTS idx_signals_cluster
    ON signals (cluster_id) WHERE cluster_id IS NOT NULL;
