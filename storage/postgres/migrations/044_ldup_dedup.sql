-- Migration 044: dedup entity_ldu_p so the same (signal, name, type)
-- can't write three rows.
--
-- The May 7 leaker scan found signal_rid=2887 had THREE pending LDU-P rows
-- for "IDF" (organization), all with the same rejection_reason. Same call
-- site fires repeatedly — likely the gateway is retrying on transient
-- failure without checking whether the LDU-P row already exists.
--
-- Actual schema (15 cols): rid (PK seq), ldu_p_id (UNIQUE), tenant_id,
-- raw_name, suspected_type, source_pipeline, source_signal_id,
-- rejection_reason, raw_payload (jsonb), status, promoted_entity_id,
-- reviewed_by, reviewed_at, created_at, updated_at.
--
-- Dedup key: (tenant_id, source_signal_id, raw_name, suspected_type).
-- Two different signals can legitimately produce the same (name, type) —
-- re-rejection of the SAME signal IS the bug we're stopping.
--
-- Strategy:
--   1. DELETE existing duplicates, keeping the oldest row per group.
--   2. CREATE UNIQUE INDEX on the dedup key WHERE source_signal_id IS NOT
--      NULL — older rows can have NULL source_signal_id and we don't want
--      to break the migration on those.

BEGIN;

-- Step 1: delete duplicate rows, keep oldest per (tenant, signal, name, type).
DELETE FROM entity_ldu_p e
USING (
    SELECT tenant_id, source_signal_id, raw_name, suspected_type,
           MIN(created_at) AS keep_created_at
    FROM entity_ldu_p
    WHERE source_signal_id IS NOT NULL
    GROUP BY 1, 2, 3, 4
    HAVING COUNT(*) > 1
) keepers
WHERE e.tenant_id = keepers.tenant_id
  AND e.source_signal_id = keepers.source_signal_id
  AND e.raw_name = keepers.raw_name
  AND e.suspected_type = keepers.suspected_type
  AND e.created_at > keepers.keep_created_at;

-- Step 2: prevent recurrence. Partial index because a NULL signal_rid can
-- legitimately repeat (rare path used by historical backfill).
CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_ldu_p_unique_signal
ON entity_ldu_p (tenant_id, source_signal_id, raw_name, suspected_type)
WHERE source_signal_id IS NOT NULL;

COMMIT;
