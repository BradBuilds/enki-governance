-- Migration 109: TOTP (Time-based One-Time Password) MFA for local auth.
--
-- DEPLOY TARGET: any node running ENKI_PROFILE=node-local (or hardware /
-- evidentiary — same identities table).
--
-- Adds optional MFA layer to the existing /auth/local/* email+password
-- flow. Compatible with any RFC 6238 TOTP app:
--   • Google Authenticator
--   • Authy
--   • 1Password (built-in TOTP)
--   • Bitwarden (built-in TOTP)
--   • Microsoft Authenticator
--
-- The TOTP shared secret is generated server-side at enrollment time,
-- shown to the user once as a QR code, then verified by entering a
-- 6-digit code from the app. After enrollment the secret stays in the
-- DB (encrypted-at-rest if you've configured pg encryption; otherwise
-- plain). Codes are computed offline by the app — Google/Apple/anyone
-- else never sees the codes or the secret.
--
-- Backup codes are single-use 8-character strings shown ONCE at
-- enrollment. If the user loses their phone, they enter one backup
-- code instead of the 6-digit TOTP. Each code is consumed atomically.

-- ─── 1. TOTP columns on identities ────────────────────────────────────────

ALTER TABLE identities
    ADD COLUMN IF NOT EXISTS totp_secret      TEXT,
    ADD COLUMN IF NOT EXISTS totp_enabled     BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS totp_enrolled_at TIMESTAMPTZ;

COMMENT ON COLUMN identities.totp_secret IS
    'Base32-encoded TOTP shared secret. Generated server-side at /totp/setup, '
    'returned to client as a QR-code-encoded otpauth:// URL, verified by the '
    'first 6-digit code submitted to /totp/verify-setup. Stays in the DB '
    'after enrollment. Never logged. Use a column-level encryption extension '
    'if your threat model requires the DB-at-rest version be ciphertext too.';

COMMENT ON COLUMN identities.totp_enabled IS
    'TRUE once the user has successfully entered a TOTP code matching their '
    'secret (proves they have the app set up). Until then, the secret may '
    'be set but login does not require a code. This separation lets the '
    'enrollment flow handle "secret generated but app not yet set up" '
    'without locking the user out.';


-- ─── 2. Single-use backup codes ───────────────────────────────────────────

CREATE TABLE IF NOT EXISTS totp_backup_codes (
    code_id         BIGSERIAL PRIMARY KEY,
    identity_id     VARCHAR(255) NOT NULL REFERENCES identities(identity_id) ON DELETE CASCADE,
    -- bcrypt hash of the 8-char backup code. We never store plaintext;
    -- the user must keep their copy. 8 chars × 12 codes = enough entropy
    -- against brute force (2^48 search space per code, online-rate-limited).
    code_hash       VARCHAR(255) NOT NULL,
    -- Position in the original batch (1..12) so the UI can render
    -- "code 3 used" etc. without storing the plaintext.
    code_index      INTEGER NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    consumed_at     TIMESTAMPTZ,
    consumed_ip     INET,
    UNIQUE (identity_id, code_index)
);

CREATE INDEX IF NOT EXISTS idx_totp_backup_codes_identity
    ON totp_backup_codes (identity_id, consumed_at);

COMMENT ON TABLE totp_backup_codes IS
    'Single-use 8-char fallback codes shown ONCE at TOTP enrollment. User '
    'writes them down; if they lose their phone, they enter one of these '
    'instead of a 6-digit TOTP code. Each code is bcrypt-hashed in storage. '
    'Atomic consumption: UPDATE ... WHERE consumed_at IS NULL RETURNING — '
    'concurrent attempts to consume the same code race to be the one valid '
    'consumer.';


-- ─── 3. Auth audit log ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS local_auth_audit (
    audit_id      BIGSERIAL PRIMARY KEY,
    audited_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    identity_id   VARCHAR(255),
    email         VARCHAR(255),
    event         TEXT NOT NULL
                    CHECK (event IN (
                        'signup_success',
                        'signup_fail',
                        'login_success',
                        'login_fail_password',
                        'login_fail_totp',
                        'login_fail_locked',
                        'totp_enrolled',
                        'totp_verify_success',
                        'totp_verify_fail',
                        'backup_code_used',
                        'password_changed',
                        'lockout_triggered',
                        'lockout_cleared'
                    )),
    reason        TEXT,
    request_ip    INET,
    user_agent    TEXT,
    metadata      JSONB
);

CREATE INDEX IF NOT EXISTS idx_local_auth_audit_identity
    ON local_auth_audit (identity_id, audited_at DESC);
CREATE INDEX IF NOT EXISTS idx_local_auth_audit_event
    ON local_auth_audit (event, audited_at DESC);
CREATE INDEX IF NOT EXISTS idx_local_auth_audit_failures_ip
    ON local_auth_audit (request_ip, audited_at DESC)
    WHERE event LIKE 'login_fail_%';

COMMENT ON TABLE local_auth_audit IS
    'Append-only log of every authentication event on the local /auth/local/* '
    'endpoints. Used for: rate limiting (count failures per IP in last N min), '
    'lockout decisions (count failures per identity), security analysis '
    '(who tried to log in as whom, when, from where), and compliance '
    '(retain N days of evidence). Never updated or deleted in normal ops.';
