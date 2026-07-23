-- ============================================================================
-- 096 — Preregistration admin panel
--
-- Extends the hub-side `preregistrations` table to support:
--   1. A per-row `status_token` (32-byte secret) the registrant uses to revisit
--      their own status page at /status/{token}. No login, no email — they
--      bookmark the URL at form-submit time and come back to see approval.
--   2. A `country` field so the admin can prioritize CA/US.
--   3. `install_command` + `install_command_expires_at` populated by
--      hub.approve_node so the customer's status page can render the curl
--      command without the admin having to send it manually.
--   4. `node_id` link to the row created in node_registry on approval.
-- ============================================================================

ALTER TABLE preregistrations
    ADD COLUMN IF NOT EXISTS status_token                VARCHAR(64),
    ADD COLUMN IF NOT EXISTS country                     VARCHAR(2),
    ADD COLUMN IF NOT EXISTS install_command             TEXT,
    ADD COLUMN IF NOT EXISTS install_command_expires_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS node_id                     VARCHAR(255),
    -- status drives the admin panel queue: pending → approved/rejected
    ADD COLUMN IF NOT EXISTS status                      VARCHAR(32)
        NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected'));

-- Backfill status_token for rows created before this migration. Uses
-- gen_random_uuid() twice and concatenates to get ~64 hex chars of entropy.
UPDATE preregistrations
   SET status_token = REPLACE(gen_random_uuid()::text, '-', '')
                   || REPLACE(gen_random_uuid()::text, '-', '')
 WHERE status_token IS NULL;

ALTER TABLE preregistrations
    ALTER COLUMN status_token SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_preregistrations_status_token
    ON preregistrations(status_token);

CREATE INDEX IF NOT EXISTS idx_preregistrations_country
    ON preregistrations(country) WHERE country IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_preregistrations_status
    ON preregistrations(status, created_at DESC);
