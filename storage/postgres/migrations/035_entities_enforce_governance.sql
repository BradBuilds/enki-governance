-- Migration 035: enforce_governance trigger on entities
--
-- Migration 032 gave knowledge_nodes a fail-closed BEFORE-INSERT/UPDATE
-- trigger that demotes any row claiming gdu_canonical=TRUE without a
-- valid base60 address. The mirror was correct, but the source-of-truth
-- table (entities) had no equivalent guard — a row could be inserted
-- with canonical_type='country' and gdu_canonical=TRUE, and only the
-- mirror would notice. This migration closes that gap.
--
-- The trigger fires only on INSERT or on UPDATE OF the columns that
-- govern the canonical claim (canonical_type, gdu_canonical). Other
-- UPDATEs (signal_count++, last_seen=NOW(), envelope merges) bypass it,
-- mirroring migration 033's housekeeping carve-out for knowledge_nodes.
--
-- Demotion path: row stays in the table, but gdu_canonical is forced to
-- FALSE and an envelope marker records the reason. cross_category_migrator
-- and merge_entity_forks paths are not affected because they explicitly
-- set gdu_canonical=FALSE themselves.

CREATE OR REPLACE FUNCTION enforce_entities_governance()
RETURNS TRIGGER AS $$
BEGIN
  IF NEW.gdu_canonical = TRUE THEN
    -- Validate canonical_type against the live entity pack positions.
    -- An entity row with a place / topic / event subtype is a contamination
    -- bug — the cross_category_migrator should route it instead.
    IF NEW.canonical_type IS NOT NULL
       AND NOT EXISTS (
         SELECT 1 FROM gdu_pack_positions
          WHERE category = 'entity'
            AND subtype  = NEW.canonical_type
       )
    THEN
      NEW.gdu_canonical := FALSE;
      NEW.envelope := COALESCE(NEW.envelope, '{}'::jsonb)
                    || jsonb_build_object(
                         'enforce_entities_governance',
                         jsonb_build_object(
                           'demoted_at',   NOW(),
                           'raw_subtype',  NEW.canonical_type,
                           'reason',       'subtype_not_in_entity_pack'
                         )
                       );
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_enforce_entities_governance ON entities;
CREATE TRIGGER trg_enforce_entities_governance
  BEFORE INSERT OR UPDATE OF canonical_type, gdu_canonical
  ON entities
  FOR EACH ROW
  EXECUTE FUNCTION enforce_entities_governance();

INSERT INTO schema_migrations (version, applied_at)
VALUES ('035_entities_enforce_governance', NOW())
ON CONFLICT DO NOTHING;
