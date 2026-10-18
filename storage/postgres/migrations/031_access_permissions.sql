-- Migration 031: Access Permission Layer
-- Controls who can query whose personal data, with alert-on-access.

CREATE TABLE IF NOT EXISTS access_permissions (
    permission_id   BIGSERIAL PRIMARY KEY,
    tenant_id       VARCHAR(255) NOT NULL,
    identity_id     VARCHAR(255) NOT NULL,        -- who is granted/denied access
    target_id       VARCHAR(255) NOT NULL,        -- whose data
    data_type       VARCHAR(50)  NOT NULL,        -- location, contacts, financial, communications, photos, all
    allowed         BOOLEAN      NOT NULL DEFAULT FALSE,
    alert_on_access BOOLEAN      NOT NULL DEFAULT TRUE,  -- notify target when accessed
    granted_by      VARCHAR(255),                  -- who set this permission
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, identity_id, target_id, data_type)
);

CREATE INDEX IF NOT EXISTS idx_access_perm_lookup
    ON access_permissions (tenant_id, target_id, data_type);

CREATE INDEX IF NOT EXISTS idx_access_perm_identity
    ON access_permissions (tenant_id, identity_id);

-- Access log for personal data retrievals (supplements audit_events)
CREATE TABLE IF NOT EXISTS access_log (
    log_id          BIGSERIAL PRIMARY KEY,
    tenant_id       VARCHAR(255) NOT NULL,
    querier_id      VARCHAR(255) NOT NULL,        -- who asked
    target_id       VARCHAR(255) NOT NULL,        -- whose data
    data_type       VARCHAR(50)  NOT NULL,        -- what type was accessed
    action          VARCHAR(50)  NOT NULL,        -- query, view, export
    allowed         BOOLEAN      NOT NULL,        -- was access granted?
    disclosed       JSONB,                         -- what was shown (summary, not full data)
    context         VARCHAR(100),                  -- chat, feed, search, api
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_access_log_target
    ON access_log (tenant_id, target_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_access_log_querier
    ON access_log (tenant_id, querier_id, created_at DESC);
