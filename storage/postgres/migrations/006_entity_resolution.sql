-- Migration 006: Entity Resolution Infrastructure
-- Adds entity_redirects, entity_enrichment_queue, and links watch_topic_entities
-- to the canonical entities table.

-- ─── Entity Redirects ─────────────────────────────────────────────────────────
-- When entities merge, the loser's ID redirects to the winner.
-- All queries resolving entity_id should chase redirects.
CREATE TABLE IF NOT EXISTS entity_redirects (
    old_entity_id   BIGINT NOT NULL,
    new_entity_id   BIGINT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    merged_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    merged_by       VARCHAR(255) NOT NULL DEFAULT 'system',
    merge_reason    TEXT,
    PRIMARY KEY (old_entity_id)
);

CREATE INDEX IF NOT EXISTS idx_entity_redirects_new
    ON entity_redirects (new_entity_id);

-- ─── Entity Enrichment Queue ──────────────────────────────────────────────────
-- Background research tasks per entity: corporate filings, aircraft registry,
-- court records, social profiles, etc.
CREATE TABLE IF NOT EXISTS entity_enrichment_queue (
    queue_id            BIGSERIAL PRIMARY KEY,
    entity_id           BIGINT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    tenant_id           VARCHAR(255) NOT NULL,
    enrichment_type     VARCHAR(64) NOT NULL,
    status              VARCHAR(20) NOT NULL DEFAULT 'pending',
    priority            INTEGER NOT NULL DEFAULT 5,
    result              JSONB,
    error               TEXT,
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    max_attempts        INTEGER NOT NULL DEFAULT 3,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    CONSTRAINT enrichment_status_check
        CHECK (status IN ('pending', 'running', 'complete', 'failed', 'skipped'))
);

CREATE INDEX IF NOT EXISTS idx_enrichment_queue_pending
    ON entity_enrichment_queue (status, priority DESC, created_at ASC)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_enrichment_queue_entity
    ON entity_enrichment_queue (entity_id, enrichment_type);

-- Prevent duplicate enrichment tasks for same entity+type while pending/running
CREATE UNIQUE INDEX IF NOT EXISTS idx_enrichment_queue_dedup
    ON entity_enrichment_queue (entity_id, enrichment_type)
    WHERE status IN ('pending', 'running');

-- ─── Link watch_topic_entities to canonical entities table ────────────────────
ALTER TABLE watch_topic_entities
    ADD COLUMN IF NOT EXISTS canonical_entity_id BIGINT REFERENCES entities(entity_id),
    ADD COLUMN IF NOT EXISTS resolution_status   VARCHAR(20) DEFAULT 'unresolved',
    ADD COLUMN IF NOT EXISTS resolution_confidence NUMERIC(3,2);

CREATE INDEX IF NOT EXISTS idx_wte_canonical
    ON watch_topic_entities (canonical_entity_id)
    WHERE canonical_entity_id IS NOT NULL;

-- ─── Add master_entity_id FK constraint on knowledge_nodes ────────────────────
-- Column already exists (schema_v2.sql line 2538) but lacks FK constraint
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'fk_knowledge_nodes_master_entity'
    ) THEN
        ALTER TABLE knowledge_nodes
            ADD CONSTRAINT fk_knowledge_nodes_master_entity
            FOREIGN KEY (master_entity_id) REFERENCES entities(entity_id)
            ON UPDATE CASCADE ON DELETE SET NULL;
    END IF;
END $$;

-- ─── Entity resolution audit log ──────────────────────────────────────────────
-- Records every resolution decision for the validation transcript (GDU S1.4)
CREATE TABLE IF NOT EXISTS entity_resolution_log (
    log_id              BIGSERIAL PRIMARY KEY,
    tenant_id           VARCHAR(255) NOT NULL,
    candidate_name      TEXT NOT NULL,
    candidate_type      VARCHAR(64),
    candidate_identifiers JSONB,
    action              VARCHAR(20) NOT NULL,
    matched_entity_id   BIGINT,
    confidence          NUMERIC(3,2),
    match_reason        TEXT,
    resolution_method   VARCHAR(32) NOT NULL,
    gdu_canonical       BOOLEAN NOT NULL DEFAULT TRUE,
    active_set_version  VARCHAR(32),
    source_pipeline     VARCHAR(64),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT resolution_action_check
        CHECK (action IN ('merge', 'create', 'review', 'skip'))
);

CREATE INDEX IF NOT EXISTS idx_resolution_log_entity
    ON entity_resolution_log (matched_entity_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_resolution_log_review
    ON entity_resolution_log (action, created_at DESC)
    WHERE action = 'review';
