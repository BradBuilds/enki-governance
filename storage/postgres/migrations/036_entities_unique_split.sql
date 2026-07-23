-- Migration 036: split the canonical-only partial UNIQUE into two indices
--
-- Migration 034 created a single partial UNIQUE on (tenant_id, normalized_name)
-- WHERE gdu_canonical = TRUE. That constraint protected canonical rows from
-- duplicating against each other — but it also created a structural bypass:
-- code (and the LLM mapper) could quietly write a "pending" row with
-- gdu_canonical = FALSE that lived outside the constraint. The Phase 1 audit
-- found 1,363 such rows.
--
-- This migration replaces the single partial UNIQUE with TWO partial UNIQUE
-- indices:
--   * one for canonical rows (unchanged behavior)
--   * one for non-canonical rows (closes the loophole — pending dups can no
--     longer accumulate)
--
-- Both keep their normalized_name guards so empty-name audit rows don't
-- collide. Reclassified merged-into rows are still expected to set
-- normalized_name = NULL (the merge_entity_forks path); those continue to
-- bypass both indices, which is the intended behavior — they are tombstones,
-- not canonical claimants.
--
-- The gateway is now the sole writer for both canonical and pending entity
-- rows (Phase 1.3). With these two indices, the database enforces the
-- single-writer-per-name invariant from both sides.

DO $$
BEGIN
  -- Drop the old single-side index if present.
  IF EXISTS (
    SELECT 1 FROM pg_indexes
     WHERE schemaname = 'public'
       AND indexname  = 'entities_tenant_normalized_canonical_uq'
  ) THEN
    DROP INDEX entities_tenant_normalized_canonical_uq;
    RAISE NOTICE 'Dropped entities_tenant_normalized_canonical_uq';
  END IF;

  -- Canonical-side: one canonical row per (tenant, normalized_name).
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
     WHERE schemaname = 'public'
       AND indexname  = 'entities_canonical_unique'
  ) THEN
    CREATE UNIQUE INDEX entities_canonical_unique
      ON entities (tenant_id, normalized_name)
      WHERE gdu_canonical = TRUE
        AND normalized_name IS NOT NULL
        AND normalized_name <> '';
    RAISE NOTICE 'Created entities_canonical_unique';
  END IF;

  -- Pending-side: one pending row per (tenant, normalized_name). This
  -- closes the gdu_canonical=FALSE bypass loophole flagged by the audit.
  -- Tombstones (normalized_name IS NULL) and reclassified rows are
  -- explicitly excluded so the audit history isn't crushed.
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
     WHERE schemaname = 'public'
       AND indexname  = 'entities_pending_unique'
  ) THEN
    CREATE UNIQUE INDEX entities_pending_unique
      ON entities (tenant_id, normalized_name)
      WHERE gdu_canonical = FALSE
        AND normalized_name IS NOT NULL
        AND normalized_name <> ''
        AND (envelope IS NULL
             OR NOT (envelope ? 'merged_into')
             AND NOT (envelope ? 'reclassified_to'));
    RAISE NOTICE 'Created entities_pending_unique';
  END IF;
END $$;

INSERT INTO schema_migrations (version, applied_at)
VALUES ('036_entities_unique_split', NOW())
ON CONFLICT DO NOTHING;
