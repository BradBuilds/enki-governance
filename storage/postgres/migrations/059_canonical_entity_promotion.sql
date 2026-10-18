-- 059: Canonical entity promotion from document extraction
--
-- Until now, document extraction produced mentions → clusters → external
-- lookups → audit decisions, but NEVER created a canonical entity row.
-- All 36,134 rows in `entities` came from the signal pipeline, not from
-- documents. This migration closes the loop:
--
--   • document_mentions.entity_id (new FK)        — every mention backlinks
--                                                   to the canonical entity
--                                                   it was promoted into
--   • document_cluster_promotions (new table)     — explicit cluster → entity
--                                                   record with promotion
--                                                   evidence + reversibility
--   • entities.source_kind (new column)            — distinguishes signal-
--                                                   vs document-derived
--                                                   entities for audit
--
-- After promotion, an entity is the canonical anchor; the dossier query
-- joins mentions/clusters/lookups/decisions through it.

-- Backlink: every mention can point to its promoted entity.
ALTER TABLE document_mentions
    ADD COLUMN IF NOT EXISTS entity_id BIGINT
        REFERENCES entities(entity_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_doc_mentions_entity
    ON document_mentions(entity_id)
    WHERE entity_id IS NOT NULL;

-- The explicit promotion record (one row per cluster→entity link).
-- Lets us audit which evidence drove the promotion and reverse cleanly.
CREATE TABLE IF NOT EXISTS document_cluster_promotions (
    promotion_id      BIGSERIAL PRIMARY KEY,
    tenant_id         TEXT NOT NULL DEFAULT 'default',
    cluster_id        BIGINT NOT NULL REFERENCES document_mention_clusters(cluster_id) ON DELETE CASCADE,
    entity_id         BIGINT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    -- Why
    rule_fired        TEXT NOT NULL,                    -- 'wikipedia_qid_match' |
                                                        -- 'internal_name_match' |
                                                        -- 'singleton_new_entity'
    promotion_score   REAL,                             -- aggregated confidence 0..1
    confidence_tier   TEXT,                             -- 'hard' | 'soft' | 'suggested'
    -- Evidence trail (mirror of collapse_decisions for traceability)
    evidence          JSONB,
    -- Where the new entity came from
    wikidata_qid      TEXT,
    internal_node_id  BIGINT,                           -- if matched an existing entity
    -- Lifecycle
    promoted_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    promoted_by       TEXT NOT NULL DEFAULT 'auto:promoter_v1',
    reverted_at       TIMESTAMPTZ,
    reverted_by       TEXT,
    notes             TEXT,
    UNIQUE (cluster_id)                                 -- one promotion per cluster
);

CREATE INDEX IF NOT EXISTS idx_promotions_entity
    ON document_cluster_promotions(entity_id);
CREATE INDEX IF NOT EXISTS idx_promotions_qid
    ON document_cluster_promotions(wikidata_qid)
    WHERE wikidata_qid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_promotions_active
    ON document_cluster_promotions(promoted_at DESC)
    WHERE reverted_at IS NULL;

-- Distinguish entities created from doc extraction vs signal pipeline,
-- without breaking any existing query that reads `entities`.
ALTER TABLE entities
    ADD COLUMN IF NOT EXISTS source_kind TEXT;        -- 'signal' | 'document' | 'manual' | 'import'
