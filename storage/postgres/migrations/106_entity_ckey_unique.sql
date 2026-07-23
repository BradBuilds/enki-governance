-- Migration 106: Tighten canonical_entity_key index to UNIQUE.
--
-- Migration 104 created a non-UNIQUE index because node-A had 29 pre-existing
-- duplicate entity groups (5x "United States / country", 3x DOJ,
-- 3x "Example Island / island", etc.). The Phase 3 subscriber's
-- resolver compensated by picking the highest-signal_count match when
-- multiple rows shared a key.
--
-- core/federation/entity_merge.py runs as a one-shot CLI to collapse
-- those duplicates: pick a winner (most signals), repoint every entity_id
-- FK in the schema (information_schema-driven, ~40 columns across
-- entity_mentions / statements / assessments / entity_relationships /
-- document_mentions / etc.), delete losers. Result on node-A: 29 groups
-- merged, 34 entity rows deleted, zero errors.
--
-- With the data clean, this migration adds the real UNIQUE constraint
-- so future duplicates fail at INSERT time rather than accumulating
-- silently. The Phase 3 subscriber's "pick highest signal_count" fallback
-- still works — it just has nothing to fall back to once UNIQUE holds.
--
-- DEPLOY ORDER:
--   1. Apply migration 104 (creates non-UNIQUE index)
--   2. Run `python -m core.gdu.entity_merge --apply`
--   3. Verify with `SELECT canonical_entity_key, COUNT(*) ... HAVING > 1`
--      returns zero rows
--   4. Apply this migration (tightens to UNIQUE)
--
-- If step 4 errors with "duplicate key value", step 2 wasn't fully
-- successful — re-run the merger before retrying this migration.

-- Drop the non-unique index from mig 104 and replace with UNIQUE.
DROP INDEX IF EXISTS idx_entities_canonical_key;

CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_canonical_key
    ON entities (tenant_id, canonical_entity_key)
    WHERE canonical_entity_key IS NOT NULL;

COMMENT ON INDEX idx_entities_canonical_key IS
    'Per-tenant uniqueness of canonical_entity_key — sha256(lower(name)|lower(type)). '
    'Tightened to UNIQUE in mig 106 after entity_merge.py collapsed 29 '
    'pre-existing node-A duplicate groups. New entity inserts that would '
    'duplicate this key now fail at INSERT time, forcing the GDU '
    'governance to use the existing canonical row.';
