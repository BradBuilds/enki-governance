-- ============================================================================
-- 098 — Install companion conversations (hub-side)
--
-- Backs the install assistant that lives at /status/{token} between approval
-- and first node activation. One row per status_token. `retired_at` is set
-- by fleet.py /fleet/register when the customer node first checks in, and
-- the chat endpoint returns 410 from that point on.
--
-- This is the canonical migration; the same DDL is also present in
-- storage/postgres/hub_schema.sql which is applied on every hub startup
-- (CREATE TABLE IF NOT EXISTS makes both safe to run).
--
-- See the install-assistant design notes for the rescope decision
-- (hub install only; ongoing assistant lives on the customer node).
-- ============================================================================

CREATE TABLE IF NOT EXISTS install_conversations (
    status_token         TEXT PRIMARY KEY REFERENCES preregistrations(status_token) ON DELETE CASCADE,
    wizard_step          TEXT,
    turns                JSONB NOT NULL DEFAULT '[]',
    turn_count           INTEGER NOT NULL DEFAULT 0,
    total_input_tokens   INTEGER NOT NULL DEFAULT 0,
    total_output_tokens  INTEGER NOT NULL DEFAULT 0,
    retired_at           TIMESTAMPTZ,
    last_message_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_install_conversations_last_message
    ON install_conversations(last_message_at);
