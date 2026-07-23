-- ============================================================================
-- 097 — Push subscriptions + email send tracking
--
-- Adds:
--   * push_subscriptions — Web Push (VAPID) endpoint + keys, one row per
--     browser/device the registrant has opted in from. Multiple per
--     preregistration are valid (phone + laptop).
--   * preregistrations.confirmation_email_sent_at — when we sent the
--     "you're on the list" email.
--   * preregistrations.approval_email_sent_at — when we sent the
--     approval email containing the install command.
--   * preregistrations.rejection_email_sent_at — same, for rejections.
-- ============================================================================

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id                   SERIAL PRIMARY KEY,
    preregistration_id   INTEGER NOT NULL REFERENCES preregistrations(id) ON DELETE CASCADE,
    endpoint             TEXT NOT NULL,
    p256dh               TEXT NOT NULL,            -- client public key (base64url)
    auth                 TEXT NOT NULL,            -- client auth secret (base64url)
    user_agent           TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at         TIMESTAMPTZ,
    last_error           TEXT,
    UNIQUE (preregistration_id, endpoint)
);

CREATE INDEX IF NOT EXISTS idx_push_sub_prereg
    ON push_subscriptions(preregistration_id);

ALTER TABLE preregistrations
    ADD COLUMN IF NOT EXISTS confirmation_email_sent_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS approval_email_sent_at      TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS rejection_email_sent_at     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS approval_push_sent_at       TIMESTAMPTZ;
