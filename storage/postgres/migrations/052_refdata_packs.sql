-- 052: Reference Data Packs
--
-- The refdata layer holds locally-cached copies of public datasets that the
-- investigation pipeline can query without hitting the live network. Each
-- pack is a versioned bundle (OFAC SDN, UN Consolidated Sanctions, OpenSanctions,
-- GLEIF, etc.) downloaded by the refdata sync worker and indexed in-DB.
--
-- Source handlers consult `refdata_packs` to decide whether a local-first
-- lookup is available and current. When a pack is fresh, the handler runs
-- a sub-millisecond local query instead of the live API call; when stale or
-- absent, it falls through to live.
--
-- The pack itself stores its records in a per-pack schema (e.g. refdata.ofac_sdn,
-- refdata.un_consolidated). This migration creates the registry only;
-- per-pack tables are created by the pack's own initializer in
-- core/refdata/packs/<pack>.py to keep schema migrations independent of
-- which packs the operator has chosen to sync.

CREATE SCHEMA IF NOT EXISTS refdata;

-- Pack registry — one row per known pack, populated from the manifest at boot
-- and updated by the sync worker after every successful refresh.
CREATE TABLE IF NOT EXISTS refdata_packs (
    pack_id          TEXT PRIMARY KEY,                -- e.g. 'ofac_sdn'
    name             TEXT NOT NULL,                   -- 'OFAC Specially Designated Nationals'
    version          TEXT,                            -- pack version stamped at sync time
    source_url       TEXT NOT NULL,                   -- bulk download URL
    license          TEXT,                            -- 'PD' / 'CC-BY' / etc.
    trust_class      TEXT NOT NULL DEFAULT 'mentioned',  -- canonical / corroborating / mentioned / review_required
    refresh_cadence  TEXT,                            -- 'daily' / 'weekly' / 'monthly' / 'static'
    on_disk_path     TEXT,                            -- where the raw dump lives
    on_disk_bytes    BIGINT,                          -- raw dump size
    record_count     INT,                             -- indexed record count
    sha256           TEXT,                            -- raw dump content hash
    last_synced_at   TIMESTAMPTZ,
    last_sync_status TEXT,                            -- 'ok' / 'failed' / 'in_progress'
    last_sync_error  TEXT,
    enabled          BOOLEAN NOT NULL DEFAULT TRUE,   -- operator can disable
    metadata         JSONB DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_refdata_packs_enabled
    ON refdata_packs(enabled) WHERE enabled = TRUE;

-- Sync history — every sync attempt, success or failure. Useful for the
-- status panel ("last successful sync at X, last failure was Y, reason Z").
CREATE TABLE IF NOT EXISTS refdata_sync_runs (
    run_id           BIGSERIAL PRIMARY KEY,
    pack_id          TEXT NOT NULL REFERENCES refdata_packs(pack_id),
    started_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at     TIMESTAMPTZ,
    status           TEXT NOT NULL,                   -- 'in_progress' / 'ok' / 'failed'
    records_indexed  INT,
    bytes_downloaded BIGINT,
    error            TEXT,
    duration_ms      INT
);

CREATE INDEX IF NOT EXISTS idx_sync_runs_pack
    ON refdata_sync_runs(pack_id, started_at DESC);

-- ─── OFAC SDN pack table (created here because it's the first pack) ──────
-- Each match-relevant SDN entry. Multiple aka / alias rows per UID share the
-- same uid via the aliases table.
CREATE TABLE IF NOT EXISTS refdata.ofac_sdn (
    uid              INT PRIMARY KEY,                 -- OFAC's stable identifier
    name             TEXT NOT NULL,
    name_normalized  TEXT NOT NULL,                   -- uppercased, punctuation-stripped
    sdn_type         TEXT,                            -- 'Individual' / 'Entity' / 'Vessel' / 'Aircraft'
    program          TEXT,                            -- 'SDGT' / 'IRAN' / 'RUSSIA-EO14024' etc.
    title            TEXT,
    remarks          TEXT,
    call_sign        TEXT,
    vess_type        TEXT,
    tonnage          TEXT,
    grt              TEXT,
    vess_flag        TEXT,
    vess_owner       TEXT,
    pack_version     TEXT NOT NULL                    -- which pack version produced this row
);

CREATE INDEX IF NOT EXISTS idx_ofac_sdn_name_norm
    ON refdata.ofac_sdn(name_normalized);
CREATE INDEX IF NOT EXISTS idx_ofac_sdn_name_trgm
    ON refdata.ofac_sdn USING gin (name_normalized gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_ofac_sdn_type
    ON refdata.ofac_sdn(sdn_type);

-- AKA / alias rows for SDN entries
CREATE TABLE IF NOT EXISTS refdata.ofac_sdn_aliases (
    alias_id         INT,
    uid              INT NOT NULL REFERENCES refdata.ofac_sdn(uid) ON DELETE CASCADE,
    alias_type       TEXT,                            -- 'aka' / 'fka' / 'nka'
    alias_category   TEXT,                            -- 'strong' / 'weak'
    alias_name       TEXT NOT NULL,
    alias_normalized TEXT NOT NULL,
    PRIMARY KEY (alias_id, uid)
);

CREATE INDEX IF NOT EXISTS idx_ofac_sdn_aliases_norm
    ON refdata.ofac_sdn_aliases(alias_normalized);
CREATE INDEX IF NOT EXISTS idx_ofac_sdn_aliases_trgm
    ON refdata.ofac_sdn_aliases USING gin (alias_normalized gin_trgm_ops);

-- Addresses associated with SDN entries — useful for geo-correlated dossiers.
CREATE TABLE IF NOT EXISTS refdata.ofac_sdn_addresses (
    addr_id          INT,
    uid              INT NOT NULL REFERENCES refdata.ofac_sdn(uid) ON DELETE CASCADE,
    address          TEXT,
    city_state       TEXT,
    country          TEXT,
    PRIMARY KEY (addr_id, uid)
);

CREATE INDEX IF NOT EXISTS idx_ofac_sdn_addresses_uid
    ON refdata.ofac_sdn_addresses(uid);

-- Ensure trigram extension exists for fuzzy name matching
CREATE EXTENSION IF NOT EXISTS pg_trgm;
