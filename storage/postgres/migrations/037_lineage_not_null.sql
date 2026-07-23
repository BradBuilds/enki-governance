-- Migration 037: lineage stamping becomes mandatory
--
-- Phase 1.5 of the collapse rebuild plan. The audit found 66% of entity rows
-- with a NULL admitted_under_active_set — entities that were admitted at a
-- time when the GDU context was degraded (or by a code path that didn't
-- stamp lineage). With Phase 1.3 closing the bypass paths and the gateway
-- being the sole writer, every new row carries a lineage stamp.
--
-- This migration:
--   1. Backfills NULL lineage with a sentinel "pre-stamping-era" value so
--      historical rows remain queryable. (After the Phase 1.1 wipe these
--      rows won't exist, but the migration must be idempotent against a
--      live DB that hasn't been wiped yet.)
--   2. Sets admitted_under_active_set NOT NULL.
--   3. Defaults admitted_under_pack_versions to '{}'::jsonb so empty stamps
--      are still queryable JSONB.
--
-- Tables touched: entities, entity_mentions, knowledge_nodes (where the
-- lineage columns exist).

BEGIN;

-- ── entities ─────────────────────────────────────────────────────────────
UPDATE entities
   SET admitted_under_active_set = 'pre-stamping-era'
 WHERE admitted_under_active_set IS NULL;

UPDATE entities
   SET admitted_under_pack_versions = '{}'::jsonb
 WHERE admitted_under_pack_versions IS NULL;

ALTER TABLE entities
  ALTER COLUMN admitted_under_active_set SET NOT NULL,
  ALTER COLUMN admitted_under_pack_versions SET NOT NULL,
  ALTER COLUMN admitted_under_pack_versions SET DEFAULT '{}'::jsonb;

-- ── entity_mentions ──────────────────────────────────────────────────────
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_name = 'entity_mentions'
       AND column_name = 'admitted_under_active_set'
  ) THEN
    UPDATE entity_mentions
       SET admitted_under_active_set = 'pre-stamping-era'
     WHERE admitted_under_active_set IS NULL;

    UPDATE entity_mentions
       SET admitted_under_pack_versions = '{}'::jsonb
     WHERE admitted_under_pack_versions IS NULL;

    ALTER TABLE entity_mentions
      ALTER COLUMN admitted_under_active_set SET NOT NULL;
  END IF;
END $$;

-- ── knowledge_nodes ──────────────────────────────────────────────────────
-- knowledge_nodes carries lineage too; mirror the constraints.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_name = 'knowledge_nodes'
       AND column_name = 'admitted_under_active_set'
  ) THEN
    UPDATE knowledge_nodes
       SET admitted_under_active_set = 'pre-stamping-era'
     WHERE admitted_under_active_set IS NULL;

    -- knowledge_nodes.admitted_under_pack_versions may not exist on every
    -- install — guard the second UPDATE.
    IF EXISTS (
      SELECT 1 FROM information_schema.columns
       WHERE table_name = 'knowledge_nodes'
         AND column_name = 'admitted_under_pack_versions'
    ) THEN
      UPDATE knowledge_nodes
         SET admitted_under_pack_versions = '{}'::jsonb
       WHERE admitted_under_pack_versions IS NULL;
    END IF;

    -- Don't NOT NULL knowledge_nodes lineage yet: existing rows came from
    -- many code paths and the safer move is to enforce NOT NULL once the
    -- wipe-and-replay cycle (Phase 5) has confirmed full coverage.
  END IF;
END $$;

INSERT INTO schema_migrations (version, applied_at)
VALUES ('037_lineage_not_null', NOW())
ON CONFLICT DO NOTHING;

COMMIT;
