-- Migration 045: headline_hash column on signal_enrichments for content dedup.
--
-- The existing `enrichment_hash` column is a hash-CHAIN integrity field
-- (sha256(prev_hash + payload)) — every row has a distinct value by design,
-- so it cannot serve as a content-dedup key. We need a separate column
-- that hashes the normalized HEADLINE alone, so feed_enrichment can ask
-- "have we already enriched an article with this same headline?".
--
-- Phase 1 of the internal news collapse plan.
--
-- Backfill is deliberately omitted — older rows store the headline only
-- inside `summary` (formatted as "headline | why_it_matters") and the
-- `prevent_mutation` trigger blocks UPDATE on this append-only table.
-- New enrichments populate headline_hash on insert; over the next 24-48h
-- of normal feed activity the index will fill organically.

BEGIN;

ALTER TABLE signal_enrichments
    ADD COLUMN IF NOT EXISTS headline_hash VARCHAR(16);

-- Partial index: only rows that ran the LLM (tier >= 2) and have a hash
-- are eligible dedup targets. Tier-1 / dedup-link rows shouldn't be
-- pointed back to.
CREATE INDEX IF NOT EXISTS idx_se_headline_hash
    ON signal_enrichments (tenant_id, headline_hash)
    WHERE headline_hash IS NOT NULL AND tier >= 2 AND method = 'llm';

COMMENT ON COLUMN signal_enrichments.headline_hash IS
    'Truncated SHA-256 (16 chars) of normalized headline (lowercased, whitespace-collapsed). '
    'Populated by core/workers/feed_enrichment.py via _headline_hash(). '
    'Used by the dedup-before-LLM lookup; distinct from enrichment_hash which is chain-integrity.';

COMMIT;
