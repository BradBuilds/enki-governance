-- Migration 008 — entity reality check
--
-- Cheap external-authority validation pass. Answers the question
-- "does this entity correspond to something real?" by looking it up in
-- Wikidata / REST Countries / GLEIF / a curated stop-list.
--
-- This is Phase 1 in the two-phase validation plan. Runs BEFORE deep
-- narrative research (Phase 2, Gemma-driven). Filters out LLM
-- extraction garbage like "Take Effect" or "Explosive Media" that
-- won't resolve to any authoritative source.

ALTER TABLE entities
    ADD COLUMN IF NOT EXISTS reality_status     VARCHAR(32),
    ADD COLUMN IF NOT EXISTS reality_source     VARCHAR(64),
    ADD COLUMN IF NOT EXISTS reality_external_id VARCHAR(255),
    ADD COLUMN IF NOT EXISTS reality_confidence NUMERIC(3, 2),
    ADD COLUMN IF NOT EXISTS reality_checked_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS reality_notes      TEXT;

-- Values for reality_status:
--   'verified'    — found in authoritative source with matching type
--   'ambiguous'   — found but type mismatched or multiple candidates
--   'unresolved'  — looked up, no matching result in any source
--   'stoplisted'  — matched the curated false-positive list (verbs, phrases)
--   NULL          — not yet checked

CREATE INDEX IF NOT EXISTS idx_entities_reality_status
    ON entities (tenant_id, reality_status, reality_checked_at);

CREATE INDEX IF NOT EXISTS idx_entities_reality_pending
    ON entities (tenant_id, created_at)
    WHERE reality_status IS NULL;
