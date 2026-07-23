-- 118_unify_places_into_entities.sql
--
-- Unify the dual-table referent model into one.
--
-- BEFORE
--   entities (person, org, product, facility, aircraft, vehicle, ...)
--   places   (city, region, country, ...)
--   concept_nodes promotes → entities ONLY (substrate excluded places)
--
-- AFTER
--   entities (ALL referents — including places)
--   place_attributes (sidecar keyed on entity_id, holds spatial-specific
--                     data: coordinates, bbox, ISO codes, parent, etc.)
--   concept_nodes promotes → entities (substrate covers places too)
--
-- Why: form→concept→instance collapse needs one substrate to be
-- universal. Pre-unification, a "Toronto" mention couldn't form a
-- form_concept_binding because places aren't entities and concept_nodes
-- only points at entities. Post-unification, every referent — Sam
-- Rowan, Toronto, the 1953 Iran coup — participates in the same
-- collapse mechanism: form binds to concept, concept promotes to
-- entity, entity has type-specific sidecar(s) for richer data.
--
-- Sidecar pattern: same as entity_kb_cache (biographical/encyclopedic),
-- vessel_specs, aircraft_specs — entity is the spine, sidecar is the
-- type-specific richer fields.
--
-- This migration is DESTRUCTIVE on the places table. The design
-- chose "drop the separate places table" over "extend substrate to
-- cover places too".
-- The fresh DB run is the moment to land it; existing data is reference
-- only and not preserved.

BEGIN;

-- ── 1. PLACE_ATTRIBUTES SIDECAR ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS place_attributes (
    -- Spine: every row is a place-typed entity. PK doubles as FK so a
    -- given entity has at most one sidecar row.
    entity_id              BIGINT PRIMARY KEY
                              REFERENCES entities(entity_id) ON DELETE CASCADE,

    -- Spatial data — was places.coordinates / places.bbox.
    coordinates            geography(Point, 4326),
    bbox                   geometry(Polygon, 4326),
    elevation_m            double precision,

    -- ISO codes — was places.iso_country_code / places.iso_region_code.
    iso_country_code       char(2),
    iso_region_code        varchar(8),

    -- Hierarchy — was places.parent_place_id; now points at entities.
    parent_entity_id       BIGINT REFERENCES entities(entity_id)
                              ON DELETE SET NULL,

    -- Gazetteer identifier (also lives in entities.identifiers JSONB
    -- for substrate lookup; kept here for direct join performance).
    geonames_id            varchar(64),

    -- Geocoding metadata.
    geocode_source         varchar(64),
    geocode_confidence     numeric(3, 2),

    -- Spatial embedding for similarity search (was places.embedding).
    embedding              vector(384),

    -- Completeness tracking (was places.completeness_score / _tier).
    completeness_score     numeric(3, 2) DEFAULT 0.00,
    completeness_tier      varchar(16),

    created_at             timestamptz NOT NULL DEFAULT NOW(),
    updated_at             timestamptz NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_place_attrs_coords
    ON place_attributes USING gist(coordinates);
CREATE INDEX IF NOT EXISTS idx_place_attrs_country
    ON place_attributes(iso_country_code)
    WHERE iso_country_code IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_place_attrs_parent
    ON place_attributes(parent_entity_id)
    WHERE parent_entity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_place_attrs_geonames
    ON place_attributes(geonames_id)
    WHERE geonames_id IS NOT NULL;

COMMENT ON TABLE place_attributes IS
    'Spatial sidecar for entities whose canonical_type is place-like '
    '(city, region, country, facility, etc.). Mirrors the entity_kb_cache '
    'pattern: entity_id is both PK and FK so each entity has at most one '
    'sidecar row. Unification of the legacy places table into the '
    'universal entities table — see migration 118 header for rationale.';


-- ── 2. DROP THE LEGACY PLACES TABLE ─────────────────────────────────────

-- CASCADE drops dependent FK constraints from other tables. Affected:
--   * knowledge_nodes.master_place_id (FK constraint dropped; column
--     itself is removed below)
--   * extraction_passes.place_id (same)
--   * places.parent_place_id (self-FK, gone with the table)

DROP TABLE IF EXISTS places CASCADE;


-- ── 3. DROP DANGLING COLUMNS LEFT BEHIND BY CASCADE ─────────────────────

-- These columns referenced places.place_id; their FK constraints went
-- away with CASCADE but the columns themselves remain (unconstrained).
-- In the unified world every referent — including places — is in the
-- entities table, so master_entity_id covers what master_place_id used
-- to. Same for extraction_passes.entity_id covering place_id.

ALTER TABLE knowledge_nodes
    DROP COLUMN IF EXISTS master_place_id;

ALTER TABLE extraction_passes
    DROP COLUMN IF EXISTS place_id;

ALTER TABLE signal_extractions
    DROP COLUMN IF EXISTS master_place_id;


INSERT INTO schema_migrations (version, applied_at)
VALUES ('118_unify_places_into_entities', NOW())
ON CONFLICT DO NOTHING;

COMMIT;
