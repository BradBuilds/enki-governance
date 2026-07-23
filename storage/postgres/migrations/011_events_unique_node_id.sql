-- Migration 011: UNIQUE constraint on events(tenant_id, node_id)
--
-- Phase 1.2 of the node-collapse plan. Without a UNIQUE constraint on
-- events.node_id the existing `ON CONFLICT DO NOTHING` in persist_events
-- is a silent no-op — every extraction pass creates a new row, even for
-- the same deterministic event node_id.
--
-- Steps:
--   1. Backfill NULL node_ids by deriving one from (event_type, title, event_time)
--      using the same SHA1 formula as core.extraction.extractors.events._node_id_for_event
--   2. Dedupe existing (tenant_id, node_id) duplicates — keep the oldest row,
--      merge source_signal_rids / actor_entity_ids / target_entity_ids into it,
--      sum source_count, take MAX confidence, delete the others
--   3. Add UNIQUE constraint on (tenant_id, node_id)
--   4. Add lookup index on node_id alone (needed for Phase 1.3 clustering
--      and for edge-join queries)
--
-- Safe to re-run: every step is idempotent.

BEGIN;

-- Required for DIGEST() used in the NULL node_id backfill
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Step 1: backfill NULL node_ids
UPDATE events
SET node_id = 'event:' || SUBSTRING(
    ENCODE(DIGEST(
        COALESCE(event_type, '') || '|' ||
        LOWER(COALESCE(title, '')) || '|' ||
        COALESCE(TO_CHAR(event_time AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"+00:00"'), ''),
        'sha1'
    ), 'hex')
    FROM 1 FOR 16
)
WHERE node_id IS NULL;

-- Step 2: dedupe by (tenant_id, node_id). Pick the oldest event_id as the
-- survivor and fold all duplicate data into it before deleting the dups.
-- Arrays are flattened via LATERAL unnest (array_agg of nullable/mixed-
-- dimension BIGINT[] can't be un-nested directly, so we unnest per-row
-- then DISTINCT the flattened values).
WITH ranked AS (
    SELECT
        event_id,
        tenant_id,
        node_id,
        ROW_NUMBER() OVER (PARTITION BY tenant_id, node_id ORDER BY created_at ASC, event_id ASC) AS rn
    FROM events
    WHERE node_id IS NOT NULL
),
survivors AS (
    SELECT tenant_id, node_id, event_id AS survivor_id
    FROM ranked WHERE rn = 1
),
merged AS (
    SELECT
        s.survivor_id,
        (SELECT ARRAY(
            SELECT DISTINCT sig
              FROM events e2
              JOIN survivors s2 ON s2.tenant_id = e2.tenant_id AND s2.node_id = e2.node_id
              CROSS JOIN LATERAL unnest(COALESCE(e2.source_signal_rids, ARRAY[]::bigint[])) AS sig
             WHERE s2.survivor_id = s.survivor_id
        )) AS merged_signals,
        (SELECT ARRAY(
            SELECT DISTINCT a
              FROM events e2
              JOIN survivors s2 ON s2.tenant_id = e2.tenant_id AND s2.node_id = e2.node_id
              CROSS JOIN LATERAL unnest(COALESCE(e2.actor_entity_ids, ARRAY[]::bigint[])) AS a
             WHERE s2.survivor_id = s.survivor_id
        )) AS merged_actors,
        (SELECT ARRAY(
            SELECT DISTINCT t
              FROM events e2
              JOIN survivors s2 ON s2.tenant_id = e2.tenant_id AND s2.node_id = e2.node_id
              CROSS JOIN LATERAL unnest(COALESCE(e2.target_entity_ids, ARRAY[]::bigint[])) AS t
             WHERE s2.survivor_id = s.survivor_id
        )) AS merged_targets,
        (SELECT MAX(confidence) FROM events e3
         JOIN survivors s3 ON s3.tenant_id = e3.tenant_id AND s3.node_id = e3.node_id
         WHERE s3.survivor_id = s.survivor_id) AS max_confidence
    FROM survivors s
)
UPDATE events e
SET
    source_signal_rids = m.merged_signals,
    source_count = cardinality(m.merged_signals),
    actor_entity_ids = m.merged_actors,
    target_entity_ids = m.merged_targets,
    confidence = m.max_confidence,
    updated_at = NOW()
FROM merged m
WHERE e.event_id = m.survivor_id;

-- Delete the duplicates (everything except the survivor per group).
-- Use (created_at, event_id) lexicographic compare so ties in created_at
-- don't leave two survivors — fall back to lower event_id wins.
DELETE FROM events e
WHERE EXISTS (
    SELECT 1 FROM events s
    WHERE s.tenant_id = e.tenant_id
      AND s.node_id = e.node_id
      AND s.event_id <> e.event_id
      AND (s.created_at, s.event_id) < (e.created_at, e.event_id)
);

-- Step 3: enforce NOT NULL + UNIQUE going forward
ALTER TABLE events ALTER COLUMN node_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'events_tenant_node_unique'
    ) THEN
        ALTER TABLE events
          ADD CONSTRAINT events_tenant_node_unique UNIQUE (tenant_id, node_id);
    END IF;
END $$;

-- Step 4: lookup index for clustering + edge joins
CREATE INDEX IF NOT EXISTS idx_events_node_id ON events (node_id);

COMMIT;
