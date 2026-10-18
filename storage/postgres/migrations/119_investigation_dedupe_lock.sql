-- Migration 119: Investigation dedupe lock
--
-- Stops the same entity from having two active investigations at once.
-- Multiple writers (gateway admit-time, periodic sweep, selection-research,
-- /research-now endpoint, AI action handler, cascade) all INSERT here.
-- Before this index, each one did a SELECT-then-INSERT cooldown check that
-- raced — we observed 10 runs for one entity in 6.5 hours.
--
-- The partial unique index makes the active-investigation invariant
-- enforceable in the database, so all callers can `ON CONFLICT DO NOTHING`
-- and let Postgres serialize the race.
--
-- Scope:
--   - Applies only to rows with a non-null entity_id (cascade by seed_name
--     keeps its own cooldown — it operates before the entity exists).
--   - Limited to 'pending' and 'running' — completed/failed rows stay queryable.

CREATE UNIQUE INDEX IF NOT EXISTS uniq_entity_investigations_active
    ON entity_investigations (tenant_id, entity_id)
 WHERE entity_id IS NOT NULL
   AND status IN ('pending', 'running');

COMMENT ON INDEX uniq_entity_investigations_active IS
    'Stops duplicate active investigations for the same entity. All INSERT '
    'paths should use ON CONFLICT DO NOTHING to make the race a no-op.';
