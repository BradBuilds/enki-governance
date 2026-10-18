-- Migration 051: entity_addresses table + canonical ownership relationship
-- types for the asset & address layer.
--
-- Phase 8b.6.2 of the internal 8b5 8b6 plan.
--
-- Two pieces:
--
-- 1. entity_addresses — first-class storage for any address belonging to an
--    entity. Today addresses live in the unstructured `payload` JSONB on
--    investigation_findings, so we can't query "all of Vale's properties"
--    or render them on a map. This migration promotes addresses to
--    columns + spatial index, with the geocode worker (8b.6.3) populating
--    lat/lon via Nominatim.
--
-- 2. Canonical ownership relationship types — entity_relationships.relationship
--    is already an unrestricted VARCHAR(64), so no schema change is needed
--    for new types. This migration just registers the canonical string set
--    via a CHECK-style helper view + a documentation comment so the
--    subsidiary chain walker (8b.6.4) knows which edges to follow.
--
-- Reversible: DROP TABLE entity_addresses + DROP VIEW canonical_ownership_types.

BEGIN;

-- ── entity_addresses ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS entity_addresses (
    address_id        BIGSERIAL PRIMARY KEY,
    tenant_id         VARCHAR(255) NOT NULL,
    entity_id         BIGINT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,

    -- What kind of address this is for the entity
    address_type      VARCHAR(40),
    -- canonical values: hq | branch | registered | residence
    --                  | asset_location | mailing | principal_office
    --                  | base | port | airport | facility

    -- Raw + parsed components
    raw_address       TEXT NOT NULL,
    street            TEXT,
    city              TEXT,
    region            TEXT,           -- state/province
    postal_code       TEXT,
    country           TEXT,

    -- Geocoding
    lat               DOUBLE PRECISION,
    lon               DOUBLE PRECISION,
    coordinates       GEOGRAPHY(POINT, 4326),  -- generated below
    geocoded_at       TIMESTAMPTZ,
    geocode_source    VARCHAR(40),    -- nominatim | factbook | manual | source_record

    -- Provenance
    source            VARCHAR(40),    -- which bridge admitted this (sec_edgar, opencorporates, faa, etc.)
    source_record     JSONB,
    confidence        NUMERIC(3,2),

    -- Lifecycle (optional; sources like SEC give effective dates)
    valid_from        DATE,
    valid_to          DATE,

    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Lineage stamps for governance audit
    admitted_under_active_set     VARCHAR(32),
    admitted_under_pack_versions  JSONB,

    CONSTRAINT entity_addresses_unique
        UNIQUE (tenant_id, entity_id, address_type, raw_address)
);

-- Spatial point derived from lat/lon. Updated by trigger on INSERT/UPDATE.
-- Using GEOGRAPHY so distance queries (ST_Distance) work in metres.
CREATE OR REPLACE FUNCTION entity_addresses_set_coordinates()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.lat IS NOT NULL AND NEW.lon IS NOT NULL THEN
        NEW.coordinates := ST_SetSRID(ST_MakePoint(NEW.lon, NEW.lat), 4326)::geography;
    ELSE
        NEW.coordinates := NULL;
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_entity_addresses_set_coords ON entity_addresses;
CREATE TRIGGER trg_entity_addresses_set_coords
    BEFORE INSERT OR UPDATE ON entity_addresses
    FOR EACH ROW EXECUTE FUNCTION entity_addresses_set_coordinates();

CREATE INDEX IF NOT EXISTS idx_entity_addresses_entity
    ON entity_addresses (tenant_id, entity_id, address_type);
CREATE INDEX IF NOT EXISTS idx_entity_addresses_geo
    ON entity_addresses USING gist (coordinates);
CREATE INDEX IF NOT EXISTS idx_entity_addresses_country
    ON entity_addresses (country, region, city);
CREATE INDEX IF NOT EXISTS idx_entity_addresses_source
    ON entity_addresses (source);

COMMENT ON TABLE entity_addresses IS
    'Phase 8b.6.2: first-class storage for any address belonging to an '
    'entity. Geocoded via Nominatim by the address_geocoder worker. '
    'Map renderer pulls from this table to plot entity assets / HQs / '
    'properties. Multiple addresses per entity (HQ + branches + '
    'subsidiaries via the chain walker).';

COMMENT ON COLUMN entity_addresses.address_type IS
    'hq | branch | registered | residence | asset_location | mailing | '
    'principal_office | base | port | airport | facility';

-- ── Canonical ownership relationship types ────────────────────────────
-- entity_relationships.relationship is unrestricted VARCHAR(64). The
-- subsidiary chain walker (8b.6.4) follows ONLY these edges, so the
-- dossier doesn't grab unrelated entities through co_occurred /
-- mentioned_with edges.
CREATE OR REPLACE VIEW canonical_ownership_types AS
SELECT * FROM (VALUES
    ('owns',                'Direct ownership of asset or entity',         TRUE),
    ('subsidiary_of',       'Corporate subsidiary relationship',           TRUE),
    ('controls',            'Control without direct ownership (trustee, fiduciary)', TRUE),
    ('registered_to',       'Asset registration ownership (FAA, IMO, MMSI)', TRUE),
    ('beneficial_owner_of', 'UBO disclosure (Companies House, FinCEN)',    TRUE),
    ('parent_of',           'Inverse of subsidiary_of',                    TRUE),
    ('held_by',             'Inverse of owns',                             TRUE)
) t(relationship, description, is_ownership);

COMMENT ON VIEW canonical_ownership_types IS
    'Phase 8b.6.2: canonical set of ownership-class relationship strings '
    'used by entity_relationships.relationship. The subsidiary chain '
    'walker (8b.6.4) filters to these edges only. Adding new ownership '
    'types means adding a row here AND the walker''s allow-list.';

COMMIT;
