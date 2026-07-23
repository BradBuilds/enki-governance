-- Migration 041: mentions_queue — async worker boundary for entity_mentions
--
-- Architectural fix: entity_mentions writes were inline during signal
-- ingest (`core/extraction/runner.py:505`), which mismatched the rest of
-- the system where every aggregator/writer runs in a supervised worker
-- (watch_derivatives, conflict_scanner, node_builder, etc.).
--
-- Inline writes had three problems:
--   1. Blocked ingest on extraction completion (slow LLM = stalled queue)
--   2. Silent failure on exception (no retry, no backfill)
--   3. No observability (queue depth invisible)
--
-- New flow:
--   ingest → run_extraction → enqueue_mentions() ← cheap INSERT (~3ms)
--                                  │
--                                  ▼
--                            mentions_queue
--                                  │
--                                  ▼
--                       core/workers/mentions_writer.py
--                       (daemon, polls every 2s, batches of 20,
--                        FOR UPDATE SKIP LOCKED for safe concurrency)
--                                  │
--                                  ▼
--                             entity_mentions
--
-- Queue is append-friendly: 'done' and 'error' rows stay for audit.
-- A separate cleanup job (in mentions_writer) prunes 'done' > 7 days old.

BEGIN;

CREATE TABLE IF NOT EXISTS mentions_queue (
    queue_id        BIGSERIAL PRIMARY KEY,
    tenant_id       VARCHAR(255) NOT NULL DEFAULT 'default',

    -- What to process
    signal_rid      BIGINT NOT NULL,
    envelope        JSONB  NOT NULL,

    -- Status: queued | processing | done | error
    status          VARCHAR(16) NOT NULL DEFAULT 'queued',
    attempts        INT NOT NULL DEFAULT 0,
    last_error      TEXT,

    -- Stats populated on success
    inserted_count  INT,
    skipped_count   INT,
    duration_ms     INT,

    -- Lineage at enqueue time (which active_set/packs queued this)
    queued_under_active_set     VARCHAR(32),
    queued_under_pack_versions  JSONB DEFAULT '{}'::jsonb,

    -- Timestamps
    queued_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    locked_at       TIMESTAMPTZ,
    processed_at    TIMESTAMPTZ,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT mentions_queue_status_chk
        CHECK (status IN ('queued', 'processing', 'done', 'error'))
);

-- Worker pulls active rows here (queued or retry-eligible errors)
CREATE INDEX IF NOT EXISTS idx_mentions_queue_active
    ON mentions_queue (status, queued_at)
    WHERE status IN ('queued', 'processing', 'error');

-- Lookup by signal (debugging "what happened to signal 42's mentions?")
CREATE INDEX IF NOT EXISTS idx_mentions_queue_signal
    ON mentions_queue (signal_rid);

-- One ACTIVE queue row per signal (prevents duplicate enqueue for the same
-- in-flight extraction). 'done' / 'error' rows can accumulate for audit;
-- a fresh extraction (replay) inserts a new row because the old one is
-- in a terminal state.
CREATE UNIQUE INDEX IF NOT EXISTS idx_mentions_queue_active_unique
    ON mentions_queue (tenant_id, signal_rid)
    WHERE status IN ('queued', 'processing');

INSERT INTO schema_migrations (version, applied_at)
VALUES ('041_mentions_queue', NOW())
ON CONFLICT DO NOTHING;

COMMIT;
