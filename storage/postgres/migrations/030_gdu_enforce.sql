-- Migration 030: GDU Enforcement
-- Ensures base-60 addresses are unique and entity redirects are indexed.

-- Unique constraint on base-60 addresses (partial — only non-NULL)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_knowledge_nodes_base60'
    ) THEN
        ALTER TABLE knowledge_nodes
        ADD CONSTRAINT uq_knowledge_nodes_base60
        UNIQUE (base60_address);
    END IF;
EXCEPTION WHEN undefined_table THEN
    NULL;
END $$;

-- Index for entity redirect lookups
CREATE INDEX IF NOT EXISTS idx_entity_redirects_old_id
    ON entity_redirects(old_entity_id)
    WHERE old_entity_id IS NOT NULL;

-- Index for ungoverned node auditing
CREATE INDEX IF NOT EXISTS idx_knowledge_nodes_ungoverned
    ON knowledge_nodes(gdu_canonical)
    WHERE gdu_canonical IS NULL OR gdu_canonical = FALSE;
