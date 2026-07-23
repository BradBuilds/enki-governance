-- Migration 042: entity_investigations cascade columns
--
-- The investigation worker's cascade path (core/workers/investigation.py
-- _cascade_discovered_entities) writes `depth` and `discovered_from` to
-- entity_investigations when auto-queueing follow-on investigations from
-- a parent's discovered officers/subsidiaries.
--
-- Migration 029 created the table without these columns. The cascade INSERT
-- raised `column "depth" does not exist`, which the worker's try/except
-- swallowed — so cascades silently no-op'd and we never auto-investigated
-- discovered entities. Adding the columns restores the cascade.
--
-- depth           — 0 for user-initiated, +1 per cascade hop, capped at
--                   MAX_CASCADE_DEPTH=3 in the worker.
-- discovered_from — free-text breadcrumb of the parent investigation, e.g.
--                   "cascade from investigation 123".

ALTER TABLE entity_investigations
    ADD COLUMN IF NOT EXISTS depth SMALLINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS discovered_from TEXT;

CREATE INDEX IF NOT EXISTS idx_investigations_depth
    ON entity_investigations(depth)
    WHERE depth > 0;
