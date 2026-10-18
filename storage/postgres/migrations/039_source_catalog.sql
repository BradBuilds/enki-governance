-- Migration 039: source catalog + per-tenant overrides
--
-- Phase 3.2 of the collapse rebuild plan. The user composes their intel
-- diet from a curated YAML catalog (scripts/seed/source_catalog/*.yaml).
-- The catalog is loaded into ``source_catalog`` at boot from those files
-- (read-mostly mirror), and per-tenant on/off state lives in
-- ``source_catalog_overrides``. Topics filter on enabled sources via the
-- ``connector_ids`` list on ``watch_topics`` (already exists).
--
-- Tables:
--   source_catalog          — hot mirror of YAML, refreshed at boot
--   source_catalog_overrides — per-tenant overrides (disable / custom add)
--
-- Both tables are intentionally append-friendly: rows aren't deleted,
-- they're flagged. This preserves audit history of catalog changes.

CREATE TABLE IF NOT EXISTS source_catalog (
    catalog_id            VARCHAR(64)  PRIMARY KEY,    -- e.g. 'reuters_world'
    name                  TEXT         NOT NULL,
    url                   TEXT         NOT NULL,
    region                VARCHAR(32)  NOT NULL,
    language              VARCHAR(8)   NOT NULL,
    category              VARCHAR(32)  NOT NULL,
    poll_interval_default INTEGER      NOT NULL DEFAULT 1800,
    description           TEXT,
    requires_key          BOOLEAN      NOT NULL DEFAULT FALSE,
    parser_hint           VARCHAR(64),
    logo_url              TEXT,

    -- Provenance: which YAML file + pack_version this came from
    pack_version          VARCHAR(32),
    source_yaml_file      VARCHAR(128),

    -- Lifecycle
    deprecated_at         TIMESTAMPTZ,
    deprecated_reason     TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_source_catalog_category
    ON source_catalog (category);
CREATE INDEX IF NOT EXISTS idx_source_catalog_region
    ON source_catalog (region);
CREATE INDEX IF NOT EXISTS idx_source_catalog_language
    ON source_catalog (language);
CREATE INDEX IF NOT EXISTS idx_source_catalog_active
    ON source_catalog (deprecated_at)
    WHERE deprecated_at IS NULL;


CREATE TABLE IF NOT EXISTS source_catalog_overrides (
    override_id           BIGSERIAL    PRIMARY KEY,
    tenant_id             VARCHAR(255) NOT NULL,

    -- Which catalog entry is being overridden (NULL = custom URL add)
    catalog_id            VARCHAR(64)  REFERENCES source_catalog(catalog_id) ON DELETE CASCADE,

    -- For per-tenant custom adds (URL paste), these fields fill in.
    -- Either catalog_id is set (existing entry override) OR custom_* fields
    -- are set (new tenant-specific source). Constraint enforced below.
    custom_name           TEXT,
    custom_url            TEXT,
    custom_parser_hint    VARCHAR(64),

    -- The override
    enabled               BOOLEAN NOT NULL DEFAULT TRUE,
    poll_interval_secs    INTEGER,                      -- NULL = use default

    -- Audit
    created_by_identity   VARCHAR(64),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT source_overrides_xor_target CHECK (
        (catalog_id IS NOT NULL) OR
        (custom_name IS NOT NULL AND custom_url IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_source_overrides_tenant
    ON source_catalog_overrides (tenant_id);

CREATE INDEX IF NOT EXISTS idx_source_overrides_catalog
    ON source_catalog_overrides (catalog_id)
    WHERE catalog_id IS NOT NULL;

-- One active override per (tenant, catalog_id) — toggling on/off updates
-- the existing row.
CREATE UNIQUE INDEX IF NOT EXISTS idx_source_overrides_unique
    ON source_catalog_overrides (tenant_id, catalog_id)
    WHERE catalog_id IS NOT NULL;


-- watch_topics may already have a connector_ids array (used by Phase 1.3
-- changes). Confirm the column exists; this is a no-op if so.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_name = 'watch_topics' AND column_name = 'connector_ids'
  ) THEN
    ALTER TABLE watch_topics ADD COLUMN connector_ids TEXT[] DEFAULT '{}';
  END IF;
END $$;

INSERT INTO schema_migrations (version, applied_at)
VALUES ('039_source_catalog', NOW())
ON CONFLICT DO NOTHING;
