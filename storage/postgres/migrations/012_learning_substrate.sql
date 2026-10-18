-- Migration 012: self-learning substrate tables
--
-- learning_pulse  — single-row table holding the timestamp of the most
--                   recent user-facing API request. API middleware calls
--                   Governor.notice_foreground() which upserts this row.
--                   Governor reads it each tick to compute pressure level.
--
-- learning_log    — append-only audit of every background worker invocation:
--                   what ran, under what pressure, what it did, how long it
--                   took. Lets us see what the system learned while idle,
--                   and spot runaway workers.

BEGIN;

CREATE TABLE IF NOT EXISTS learning_pulse (
    pulse_id            SMALLINT PRIMARY KEY DEFAULT 1,
    last_activity_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT learning_pulse_singleton CHECK (pulse_id = 1)
);
INSERT INTO learning_pulse (pulse_id, last_activity_at)
VALUES (1, NOW())
ON CONFLICT (pulse_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS learning_log (
    log_id              BIGSERIAL PRIMARY KEY,
    worker              VARCHAR(64) NOT NULL,
    priority            SMALLINT NOT NULL,
    stats               JSONB NOT NULL DEFAULT '{}'::jsonb,
    error               TEXT,
    elapsed_ms          INT NOT NULL DEFAULT 0,
    pressure_level      SMALLINT NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_learning_log_recent
    ON learning_log (worker, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_learning_log_errors
    ON learning_log (created_at DESC)
    WHERE error IS NOT NULL;

COMMIT;
