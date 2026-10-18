-- ============================================================================
-- ENKI HUB — Lightweight control plane schema
--
-- This schema is used ONLY on the cloud hub (example.com).
-- It stores NO customer data — only node registry, fleet health, and
-- preregistrations.
-- ============================================================================

-- Preregistrations (early access signups from marketing page)
CREATE TABLE IF NOT EXISTS preregistrations (
    id                          SERIAL PRIMARY KEY,
    email                       VARCHAR(255) NOT NULL UNIQUE,
    name                        VARCHAR(500),
    organization                VARCHAR(500),
    country                     VARCHAR(2),                       -- ISO 3166-1 alpha-2 (CA, US, ...)
    use_case                    TEXT,
    message                     TEXT,
    status                      VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending, approved, rejected
    status_token                VARCHAR(64) NOT NULL,             -- bearer secret for /status/{token} page
    install_command             TEXT,                              -- populated on approval, shown on status page
    install_command_expires_at  TIMESTAMPTZ,
    node_id                     VARCHAR(255),                     -- set on approval, points at node_registry
    approved_at                 TIMESTAMPTZ,
    rejected_at                 TIMESTAMPTZ,
    confirmation_email_sent_at  TIMESTAMPTZ,
    approval_email_sent_at      TIMESTAMPTZ,
    rejection_email_sent_at     TIMESTAMPTZ,
    approval_push_sent_at       TIMESTAMPTZ,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_preregistrations_status_token
    ON preregistrations(status_token);
CREATE INDEX IF NOT EXISTS idx_preregistrations_country
    ON preregistrations(country) WHERE country IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_preregistrations_status
    ON preregistrations(status, created_at DESC);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id                   SERIAL PRIMARY KEY,
    preregistration_id   INTEGER NOT NULL REFERENCES preregistrations(id) ON DELETE CASCADE,
    endpoint             TEXT NOT NULL,
    p256dh               TEXT NOT NULL,
    auth                 TEXT NOT NULL,
    user_agent           TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at         TIMESTAMPTZ,
    last_error           TEXT,
    UNIQUE (preregistration_id, endpoint)
);

CREATE INDEX IF NOT EXISTS idx_push_sub_prereg
    ON push_subscriptions(preregistration_id);

-- Node registry (approved customer nodes)
CREATE TABLE IF NOT EXISTS node_registry (
    node_id                 VARCHAR(255) PRIMARY KEY,
    owner_email             VARCHAR(255) NOT NULL,
    display_name            VARCHAR(500),
    public_key              TEXT,                              -- Ed25519 pubkey (base64), set on first heartbeat
    domain                  VARCHAR(500),                     -- customer's custom domain if any
    registration_token_hash VARCHAR(64),                      -- SHA-256 of one-time registration token
    token_expires_at        TIMESTAMPTZ,
    status                  VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending, active, suspended
    approved_at             TIMESTAMPTZ,
    last_heartbeat          TIMESTAMPTZ,
    health                  VARCHAR(20) NOT NULL DEFAULT 'unknown',  -- unknown, healthy, degraded, error
    health_snapshot         JSONB NOT NULL DEFAULT '{}',       -- latest heartbeat data (counts, versions)
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_node_registry_status ON node_registry(status);
CREATE INDEX IF NOT EXISTS idx_node_registry_email ON node_registry(owner_email);

-- Fleet stations (heartbeat data — same as full schema but standalone here)
CREATE TABLE IF NOT EXISTS fleet_stations (
    node_id          TEXT PRIMARY KEY,
    gdu_version      TEXT,
    platform_version TEXT,
    firmware_version TEXT,
    auth_mode        TEXT DEFAULT 'full',
    hardware_profile TEXT DEFAULT 'EMULATED',
    user_count       INT DEFAULT 0,
    connector_count  INT DEFAULT 0,
    signal_count     INT DEFAULT 0,
    uptime_hours     FLOAT DEFAULT 0,
    health           TEXT DEFAULT 'healthy',
    health_details   JSONB,
    last_heartbeat   TIMESTAMPTZ,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);

-- Fleet updates (vendor-published updates for distribution to nodes)
CREATE TABLE IF NOT EXISTS fleet_updates (
    update_id       TEXT PRIMARY KEY,
    update_type     TEXT NOT NULL,
    version         TEXT NOT NULL,
    title           TEXT NOT NULL,
    description     TEXT,
    severity        TEXT DEFAULT 'normal',
    package_hash    TEXT,
    package_url     TEXT,
    requires_watch  BOOLEAN DEFAULT TRUE,
    target_nodes    TEXT[],
    published_by    TEXT,
    published_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Fleet update receipts (confirmation that a node applied an update)
CREATE TABLE IF NOT EXISTS fleet_update_receipts (
    node_id          TEXT NOT NULL,
    update_id        TEXT NOT NULL REFERENCES fleet_updates(update_id),
    applied_at       TEXT,
    watch_signature  TEXT,
    previous_version TEXT,
    new_version      TEXT,
    reported_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (node_id, update_id)
);

-- Hub admin credentials (simple password auth for the hub admin)
CREATE TABLE IF NOT EXISTS hub_admins (
    admin_id        VARCHAR(255) PRIMARY KEY,
    email           VARCHAR(255) NOT NULL UNIQUE,
    password_hash   VARCHAR(255) NOT NULL,
    display_name    VARCHAR(500),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Install companion conversations (hub-side, short-lived per token).
-- One row per status_token. `retired_at` is set on first node activation
-- ([fleet.py /fleet/register]) and after that the chat endpoint returns 410.
-- See the install-assistant design notes.
CREATE TABLE IF NOT EXISTS install_conversations (
    status_token         TEXT PRIMARY KEY REFERENCES preregistrations(status_token) ON DELETE CASCADE,
    wizard_step          TEXT,
    turns                JSONB NOT NULL DEFAULT '[]',          -- array of {role, content, ts}
    turn_count           INTEGER NOT NULL DEFAULT 0,           -- counts user turns; 20 = hard cap
    total_input_tokens   INTEGER NOT NULL DEFAULT 0,
    total_output_tokens  INTEGER NOT NULL DEFAULT 0,
    retired_at           TIMESTAMPTZ,                          -- set when node registers; chat returns 410 after
    last_message_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_install_conversations_last_message
    ON install_conversations(last_message_at);
