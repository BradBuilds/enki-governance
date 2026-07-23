-- Fleet management tables for station heartbeat, updates, and receipts.
-- Enables vendor→station update distribution with watch-approval.

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

CREATE TABLE IF NOT EXISTS fleet_updates (
    update_id    TEXT PRIMARY KEY,
    update_type  TEXT NOT NULL,        -- gdu_pack, firmware, security_patch, config
    version      TEXT NOT NULL,
    title        TEXT NOT NULL,
    description  TEXT,
    severity     TEXT DEFAULT 'normal', -- normal, important, critical
    package_hash TEXT,                  -- SHA256 of the update package
    package_url  TEXT,                  -- signed download URL
    requires_watch BOOLEAN DEFAULT TRUE,
    target_nodes TEXT[],                -- NULL = all stations
    published_by TEXT,
    published_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS fleet_update_receipts (
    node_id          TEXT NOT NULL,
    update_id        TEXT NOT NULL,
    applied_at       TEXT,
    watch_signature  TEXT,              -- Ed25519 signature from operator's watch
    previous_version TEXT,
    new_version      TEXT,
    reported_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (node_id, update_id)
);

CREATE INDEX IF NOT EXISTS idx_fleet_stations_health ON fleet_stations(health);
CREATE INDEX IF NOT EXISTS idx_fleet_stations_heartbeat ON fleet_stations(last_heartbeat DESC);
CREATE INDEX IF NOT EXISTS idx_fleet_updates_published ON fleet_updates(published_at DESC);
