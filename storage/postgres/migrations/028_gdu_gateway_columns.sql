-- 028_gdu_gateway_columns.sql
-- Adds columns to knowledge_nodes that the GDU gateway writes during admission,
-- and creates the entity_ldu_p table for provisional entity staging.

-- ── knowledge_nodes: columns the gateway needs ────────────────────────
ALTER TABLE knowledge_nodes ADD COLUMN IF NOT EXISTS entity_type VARCHAR(64);
ALTER TABLE knowledge_nodes ADD COLUMN IF NOT EXISTS entity_name TEXT;
ALTER TABLE knowledge_nodes ADD COLUMN IF NOT EXISTS source_count INTEGER NOT NULL DEFAULT 1;
ALTER TABLE knowledge_nodes ADD COLUMN IF NOT EXISTS confidence NUMERIC(3,2) DEFAULT 0.50;
ALTER TABLE knowledge_nodes ADD COLUMN IF NOT EXISTS enrichment_status VARCHAR(32) DEFAULT 'pending';
ALTER TABLE knowledge_nodes ADD COLUMN IF NOT EXISTS research_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE knowledge_nodes ADD COLUMN IF NOT EXISTS last_research_at TIMESTAMPTZ;
ALTER TABLE knowledge_nodes ADD COLUMN IF NOT EXISTS research_pass_count INTEGER DEFAULT 0;

-- ── entity_ldu_p: provisional entity staging ──────────────────────────
-- Items that fail GDU vocabulary mapping land here instead of being
-- silently discarded. They accumulate for human review and potential
-- vocabulary expansion.
CREATE TABLE IF NOT EXISTS entity_ldu_p (
    rid              BIGSERIAL    PRIMARY KEY,
    ldu_p_id         VARCHAR(255) NOT NULL UNIQUE,
    tenant_id        VARCHAR(255) NOT NULL,
    raw_name         VARCHAR(255),
    suspected_type   VARCHAR(64),
    source_pipeline  VARCHAR(100),
    source_signal_id VARCHAR(255),
    rejection_reason TEXT,
    raw_payload      JSONB DEFAULT '{}'::jsonb,
    status           VARCHAR(32)  NOT NULL DEFAULT 'pending',  -- pending, promoted, rejected
    promoted_entity_id BIGINT,
    reviewed_by      VARCHAR(255),
    reviewed_at      TIMESTAMPTZ,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_entity_ldu_p_status
    ON entity_ldu_p (tenant_id, status)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_entity_ldu_p_type
    ON entity_ldu_p (suspected_type);

-- ── entity_relationships: ensure table exists for semantic edges ──────
CREATE TABLE IF NOT EXISTS entity_relationships (
    rid                BIGSERIAL   PRIMARY KEY,
    tenant_id          VARCHAR(255) NOT NULL,
    source_entity_id   BIGINT      NOT NULL,
    target_entity_id   BIGINT      NOT NULL,
    relationship       VARCHAR(64) NOT NULL,
    confidence         NUMERIC(3,2) DEFAULT 0.70,
    source             VARCHAR(64) DEFAULT 'llm',
    evidence           TEXT,
    is_current         BOOLEAN     DEFAULT TRUE,
    article_count      INTEGER     DEFAULT 1,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_entity_rel_source
    ON entity_relationships (tenant_id, source_entity_id);

CREATE INDEX IF NOT EXISTS idx_entity_rel_target
    ON entity_relationships (tenant_id, target_entity_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_rel_unique
    ON entity_relationships (tenant_id, source_entity_id, target_entity_id, relationship);
