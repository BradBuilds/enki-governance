-- Migration 038: per-mention geolocation
--
-- Phase 4.1 of the collapse rebuild plan. ``entity_mentions`` records
-- where each entity was mentioned in each article, but didn't carry
-- coordinates — so the heatmap had to JOIN through entities → places
-- and aggregate at country grain. With these columns:
--
--   * the heatmap query becomes a single GROUP BY on H3 hex
--   * a single article that mentions Cairo and Tehran produces two
--     points instead of one country-grain blob
--
-- Backfill strategy: copy from the entity's place row (if it's a place)
-- so existing mentions get coords as a one-shot. Going forward, writers
-- (gateway / resolver / node_builder) populate the coords at mention
-- insert time, sourced from the gazetteer centroid or the resolved
-- place node.

BEGIN;

ALTER TABLE entity_mentions
  ADD COLUMN IF NOT EXISTS latitude  DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS h3_index  TEXT;

CREATE INDEX IF NOT EXISTS idx_entity_mentions_geo
  ON entity_mentions (latitude, longitude)
  WHERE latitude IS NOT NULL AND longitude IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_entity_mentions_h3
  ON entity_mentions (h3_index)
  WHERE h3_index IS NOT NULL;

-- Backfill from the related entity's base60 / known place. The places
-- table (migration 003) carries lat/lng on each place row.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_name = 'places' AND column_name = 'latitude'
  ) THEN
    UPDATE entity_mentions m
       SET latitude  = p.latitude,
           longitude = p.longitude
      FROM places p
     WHERE p.entity_id = m.entity_id
       AND m.latitude IS NULL;
  END IF;
END $$;

-- Some installs carry lat/lng on entities directly (when an entity is
-- itself a place — country, city, etc.). Backfill from there too.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_name = 'entities' AND column_name = 'latitude'
  ) THEN
    UPDATE entity_mentions m
       SET latitude  = e.latitude,
           longitude = e.longitude
      FROM entities e
     WHERE e.entity_id = m.entity_id
       AND m.latitude IS NULL
       AND e.latitude IS NOT NULL;
  END IF;
END $$;

INSERT INTO schema_migrations (version, applied_at)
VALUES ('038_mention_coords', NOW())
ON CONFLICT DO NOTHING;

COMMIT;
