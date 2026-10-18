-- Migration 017: sync entities.canonical_type → watch_topic_entities.entity_type
--
-- watch_topic_entities is an index of which entities appear in which watch
-- topic. Its `entity_type` column was populated by the cheap extractor at
-- ingest time (everything defaulted to 'person'). Phase 5's validator fixes
-- the canonical type on the master entities table, but the index column
-- never gets synced — so the watch page's filter buckets collapse.
--
-- Fix: trigger that propagates entities.canonical_type updates into
-- watch_topic_entities.entity_type, plus a one-time backfill for all
-- rows where the canonical has been validated.

BEGIN;

CREATE OR REPLACE FUNCTION sync_canonical_type_to_wte()
RETURNS TRIGGER AS $$
BEGIN
    -- Only sync on actual change to canonical_type or validation state
    IF (NEW.canonical_type IS DISTINCT FROM OLD.canonical_type) THEN
        UPDATE watch_topic_entities
           SET entity_type = NEW.canonical_type
         WHERE canonical_entity_id = NEW.entity_id
           AND tenant_id = NEW.tenant_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_sync_canonical_type ON entities;
CREATE TRIGGER trg_sync_canonical_type
    AFTER UPDATE OF canonical_type ON entities
    FOR EACH ROW
    EXECUTE FUNCTION sync_canonical_type_to_wte();

-- One-time backfill: for every wte row with a resolved canonical entity,
-- pull the current canonical_type. Only runs where the canonical has
-- actually been updated (skips the default 'person' rows whose canonical
-- hasn't been validated yet — those are still honestly 'person/unknown').
UPDATE watch_topic_entities wte
   SET entity_type = e.canonical_type
  FROM entities e
 WHERE wte.canonical_entity_id = e.entity_id
   AND wte.tenant_id = e.tenant_id
   AND e.canonical_type IS NOT NULL
   AND e.canonical_type <> wte.entity_type;

COMMIT;
