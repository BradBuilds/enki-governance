-- 077_investigation_priority.sql
-- Adds a `priority` column to entity_investigations so live UI-triggered
-- research can jump ahead of the bulk-scan queue.
--
-- Lower number = higher priority. Default 100 (normal). UI-triggered = 10.
-- Worker's _find_pending now orders by priority ASC, created_at ASC.

ALTER TABLE entity_investigations
    ADD COLUMN IF NOT EXISTS priority SMALLINT NOT NULL DEFAULT 100;

CREATE INDEX IF NOT EXISTS idx_investigations_priority_pending
    ON entity_investigations (priority ASC, created_at ASC)
 WHERE status = 'pending';
