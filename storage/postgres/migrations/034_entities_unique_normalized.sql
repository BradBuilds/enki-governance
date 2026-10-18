-- Migration 034: prevent entity collapse-key forks
--
-- After scripts/merge_entity_forks.py has consolidated existing duplicates,
-- this partial unique index makes the (tenant_id, normalized_name) collapse
-- key an enforceable invariant for future writes — but only among canonical
-- rows. Merged/reclassified rows (gdu_canonical=FALSE, with envelope marks
-- like merged_into / reclassification) are explicitly NOT covered, so the
-- table can quietly retain history without re-colliding with the survivors.
--
-- Why partial, not full:
--   The table is append-history-friendly. Reclassified-to-place rows from
--   cross_category_migrator stay around as audit trails with the original
--   normalized_name; merged dup rows likewise. A full unique index would
--   force destructive cleanup of this audit data.

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
     WHERE schemaname = 'public'
       AND indexname  = 'entities_tenant_normalized_canonical_uq'
  ) THEN
    CREATE UNIQUE INDEX entities_tenant_normalized_canonical_uq
      ON entities (tenant_id, normalized_name)
      WHERE gdu_canonical = TRUE
        AND normalized_name IS NOT NULL
        AND normalized_name <> '';
    RAISE NOTICE 'Created entities_tenant_normalized_canonical_uq';
  ELSE
    RAISE NOTICE 'entities_tenant_normalized_canonical_uq already exists';
  END IF;
END $$;

INSERT INTO schema_migrations (version, applied_at)
VALUES ('034_entities_unique_normalized', NOW())
ON CONFLICT DO NOTHING;
