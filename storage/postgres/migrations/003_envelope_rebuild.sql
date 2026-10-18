-- Migration 003: Envelope rebuild — entities, places, topics, extraction_passes,
--                 and envelope JSONB column on knowledge_nodes
--
-- This migration is the schema substrate for the universal extraction pipeline.
-- It is additive (CREATE TABLE IF NOT EXISTS, ALTER TABLE IF NOT EXISTS) so it
-- can be applied to existing databases without data loss. Idempotent.
--
-- Required by: Phase 2 (extraction worker framework), Phase 3 (resolvers),
--              Phase 5 (auto-node creation hook).
--
-- Authority: GDU active set v5 (envelopevocab, nodevocab, edgevocab,
--            completenessvocab, intentvocab, relevancevocab).

-- ═══════════════════════════════════════════════════════════════════════════
-- ENTITIES — master registry for people, organizations, vessels, aircraft,
-- vehicles, units, weapon systems, etc. One row per real-world thing.
-- Resolved by identifier_hash (deterministic merge) or normalized name +
-- embedding fallback. Replaces the per-node entity columns approach.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS entities (
    entity_id              BIGSERIAL PRIMARY KEY,
    tenant_id              VARCHAR(255) NOT NULL,

    -- Canonical identity
    canonical_type         VARCHAR(64) NOT NULL,           -- nodevocab entity subtype
    canonical_name         TEXT NOT NULL,                  -- primary display name
    normalized_name        TEXT NOT NULL,                  -- lowercased, suffix-stripped
    aliases                TEXT[] DEFAULT '{}',            -- alternate names / spellings

    -- Identifier-based deduplication
    identifiers            JSONB DEFAULT '{}'::jsonb,      -- {mmsi, vin, ein, lei, email, ...}
    identifier_hash        VARCHAR(64),                    -- sha256 over sorted identifiers
                                                            -- (deterministic merge key, NULL if no IDs)

    -- Embedding for semantic fallback resolution
    embedding              VECTOR(384),                    -- pgvector, populated by resolver

    -- Display + scoring (the canonical card envelope)
    envelope               JSONB DEFAULT '{}'::jsonb,
    envelope_version       VARCHAR(16) DEFAULT 'v1',
    completeness_score     NUMERIC(3,2) DEFAULT 0.00,
    completeness_tier      VARCHAR(16),                    -- stub | partial | solid | verified

    -- Activity timestamps
    first_seen             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    signal_count           INT NOT NULL DEFAULT 0,         -- distinct signals that mention this entity

    -- LDU lineage (which active set + pack versions admitted this row)
    admitted_under_active_set     VARCHAR(32),
    admitted_under_pack_versions  JSONB,
    gdu_canonical                 BOOLEAN NOT NULL DEFAULT TRUE,
    ldu_t_id                      BIGINT,                  -- pointer to LDU-T raw truth row

    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Identifier-hash dedup: one entity per real-world thing per tenant
    CONSTRAINT entities_identifier_hash_unique
        UNIQUE (tenant_id, identifier_hash)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX IF NOT EXISTS idx_entities_tenant_type
    ON entities (tenant_id, canonical_type);
CREATE INDEX IF NOT EXISTS idx_entities_normalized_name
    ON entities (tenant_id, normalized_name);
CREATE INDEX IF NOT EXISTS idx_entities_aliases
    ON entities USING GIN (aliases);
CREATE INDEX IF NOT EXISTS idx_entities_identifiers
    ON entities USING GIN (identifiers);
CREATE INDEX IF NOT EXISTS idx_entities_last_seen
    ON entities (tenant_id, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_entities_completeness
    ON entities (tenant_id, completeness_tier);


-- ═══════════════════════════════════════════════════════════════════════════
-- PLACES — master registry for countries, regions, cities, points of interest,
-- facility locations, named operation areas, maritime areas, airspace.
-- Separate from entities (different display needs: maps vs photos, hierarchy
-- via parent_place_id, geocoded coordinates).
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS places (
    place_id               BIGSERIAL PRIMARY KEY,
    tenant_id              VARCHAR(255) NOT NULL,

    -- Canonical identity
    canonical_type         VARCHAR(64) NOT NULL,           -- nodevocab place subtype
    canonical_name         TEXT NOT NULL,
    normalized_name        TEXT NOT NULL,
    aliases                TEXT[] DEFAULT '{}',            -- multilingual: ["Kharkiv","Харків","Kharkov"]

    -- Hierarchy
    parent_place_id        BIGINT REFERENCES places(place_id) ON DELETE SET NULL,
    iso_country_code       CHAR(2),                        -- ISO 3166-1 alpha-2
    iso_region_code        VARCHAR(8),                     -- ISO 3166-2

    -- Spatial
    coordinates            GEOGRAPHY(POINT, 4326),         -- single representative point
    bbox                   GEOMETRY(POLYGON, 4326),        -- optional bounding polygon
    elevation_m            DOUBLE PRECISION,

    -- Provenance of geocoding
    geocode_source         VARCHAR(64),                    -- 'gazetteer', 'nominatim', 'manual', etc.
    geocode_confidence     NUMERIC(3,2),

    -- Embedding for fuzzy place name resolution
    embedding              VECTOR(384),

    -- Display + scoring
    envelope               JSONB DEFAULT '{}'::jsonb,
    envelope_version       VARCHAR(16) DEFAULT 'v1',
    completeness_score     NUMERIC(3,2) DEFAULT 0.00,
    completeness_tier      VARCHAR(16),

    -- Activity
    first_seen             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    mention_count          INT NOT NULL DEFAULT 0,

    -- LDU lineage
    admitted_under_active_set     VARCHAR(32),
    admitted_under_pack_versions  JSONB,
    gdu_canonical                 BOOLEAN NOT NULL DEFAULT TRUE,
    ldu_t_id                      BIGINT,

    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_places_tenant_type
    ON places (tenant_id, canonical_type);
CREATE INDEX IF NOT EXISTS idx_places_normalized_name
    ON places (tenant_id, normalized_name);
CREATE INDEX IF NOT EXISTS idx_places_aliases
    ON places USING GIN (aliases);
CREATE INDEX IF NOT EXISTS idx_places_country
    ON places (tenant_id, iso_country_code);
CREATE INDEX IF NOT EXISTS idx_places_coordinates
    ON places USING GIST (coordinates);
CREATE INDEX IF NOT EXISTS idx_places_parent
    ON places (parent_place_id) WHERE parent_place_id IS NOT NULL;


-- ═══════════════════════════════════════════════════════════════════════════
-- TOPICS — master registry for abstract subjects (conflicts, operations,
-- treaties, technologies, ideologies). Topics emerge from extraction; they
-- are not seeded. New topics are created the first time the topic classifier
-- decides a signal belongs to one that doesn't exist yet.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS topics (
    topic_id               BIGSERIAL PRIMARY KEY,
    tenant_id              VARCHAR(255) NOT NULL,

    -- Canonical identity
    canonical_type         VARCHAR(64) NOT NULL,           -- nodevocab topic subtype
    canonical_name         TEXT NOT NULL,
    normalized_name        TEXT NOT NULL,
    aliases                TEXT[] DEFAULT '{}',

    -- Hierarchy (sub-topics)
    parent_topic_id        BIGINT REFERENCES topics(topic_id) ON DELETE SET NULL,

    -- Embedding for topic similarity / clustering
    embedding              VECTOR(384),

    -- Display + scoring
    envelope               JSONB DEFAULT '{}'::jsonb,
    envelope_version       VARCHAR(16) DEFAULT 'v1',
    completeness_score     NUMERIC(3,2) DEFAULT 0.00,
    completeness_tier      VARCHAR(16),

    -- Activity
    first_seen             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    mention_count          INT NOT NULL DEFAULT 0,

    -- LDU lineage
    admitted_under_active_set     VARCHAR(32),
    admitted_under_pack_versions  JSONB,
    gdu_canonical                 BOOLEAN NOT NULL DEFAULT TRUE,
    ldu_t_id                      BIGINT,

    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_topics_tenant_type
    ON topics (tenant_id, canonical_type);
CREATE INDEX IF NOT EXISTS idx_topics_normalized_name
    ON topics (tenant_id, normalized_name);
CREATE INDEX IF NOT EXISTS idx_topics_aliases
    ON topics USING GIN (aliases);
CREATE INDEX IF NOT EXISTS idx_topics_parent
    ON topics (parent_topic_id) WHERE parent_topic_id IS NOT NULL;


-- ═══════════════════════════════════════════════════════════════════════════
-- EXTRACTION_PASSES — provenance log for every extraction stage that ever
-- ran on every artifact. Idempotent passes can be re-run safely; the log
-- shows which model wrote which fields when. Required for audit, debugging,
-- and re-running passes when prompts or models change.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS extraction_passes (
    pass_id                BIGSERIAL PRIMARY KEY,
    tenant_id              VARCHAR(255) NOT NULL,

    -- Polymorphic target — exactly one of these is set per row
    signal_rid             BIGINT,
    entity_id              BIGINT REFERENCES entities(entity_id) ON DELETE CASCADE,
    place_id               BIGINT REFERENCES places(place_id) ON DELETE CASCADE,
    topic_id               BIGINT REFERENCES topics(topic_id) ON DELETE CASCADE,
    knowledge_node_id      VARCHAR(255),                   -- knowledge_nodes uses string PK

    -- Stage and model
    stage                  VARCHAR(32) NOT NULL,           -- connector|cheap|body_fetch|translate|deep|resolution|corroboration|chronicle|human|scoring
    model_name             VARCHAR(128),                   -- 'gemma-4-it-q4', 'fasttext-lid', null for rule-based
    model_version          VARCHAR(64),
    prompt_version         VARCHAR(32),

    -- What this pass produced
    fields_filled          JSONB DEFAULT '[]'::jsonb,      -- array of envelope field paths
    duration_ms            INT,
    success                BOOLEAN NOT NULL DEFAULT TRUE,
    error                  TEXT,

    -- LDU lineage at the time the pass ran (so we know which packs governed it)
    admitted_under_active_set     VARCHAR(32),
    admitted_under_pack_versions  JSONB,
    gdu_canonical                 BOOLEAN NOT NULL DEFAULT TRUE,

    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Exactly one target type must be set
    CONSTRAINT extraction_passes_one_target CHECK (
        (signal_rid IS NOT NULL)::INT
      + (entity_id IS NOT NULL)::INT
      + (place_id IS NOT NULL)::INT
      + (topic_id IS NOT NULL)::INT
      + (knowledge_node_id IS NOT NULL)::INT = 1
    )
);

CREATE INDEX IF NOT EXISTS idx_extpasses_signal
    ON extraction_passes (signal_rid) WHERE signal_rid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_extpasses_entity
    ON extraction_passes (entity_id) WHERE entity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_extpasses_place
    ON extraction_passes (place_id) WHERE place_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_extpasses_topic
    ON extraction_passes (topic_id) WHERE topic_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_extpasses_node
    ON extraction_passes (knowledge_node_id) WHERE knowledge_node_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_extpasses_stage_time
    ON extraction_passes (stage, created_at DESC);


-- ═══════════════════════════════════════════════════════════════════════════
-- KNOWLEDGE_NODES — additive columns for the universal envelope, completeness,
-- master-table FKs, and LDU lineage. Existing columns from schema_v2.sql are
-- preserved; the envelope column will eventually become the source of truth
-- and the legacy columns will be deprecated, but during the slice both
-- coexist for backwards compatibility.
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE knowledge_nodes
    ADD COLUMN IF NOT EXISTS envelope JSONB DEFAULT '{}'::jsonb;
ALTER TABLE knowledge_nodes
    ADD COLUMN IF NOT EXISTS envelope_version VARCHAR(16) DEFAULT 'v1';
ALTER TABLE knowledge_nodes
    ADD COLUMN IF NOT EXISTS node_type VARCHAR(32);                 -- nodevocab top-level (entity|place|event|topic|source|document|chronicle|assessment|pattern)
ALTER TABLE knowledge_nodes
    ADD COLUMN IF NOT EXISTS subtype VARCHAR(64);                   -- nodevocab subtype
ALTER TABLE knowledge_nodes
    ADD COLUMN IF NOT EXISTS completeness_score NUMERIC(3,2) DEFAULT 0.00;
ALTER TABLE knowledge_nodes
    ADD COLUMN IF NOT EXISTS completeness_tier VARCHAR(16);         -- stub | partial | solid | verified

-- FKs to master tables (one of these is non-null when the node represents one)
ALTER TABLE knowledge_nodes
    ADD COLUMN IF NOT EXISTS master_entity_id BIGINT REFERENCES entities(entity_id) ON DELETE SET NULL;
ALTER TABLE knowledge_nodes
    ADD COLUMN IF NOT EXISTS master_place_id BIGINT REFERENCES places(place_id) ON DELETE SET NULL;
ALTER TABLE knowledge_nodes
    ADD COLUMN IF NOT EXISTS master_topic_id BIGINT REFERENCES topics(topic_id) ON DELETE SET NULL;

-- LDU lineage on knowledge_nodes themselves
ALTER TABLE knowledge_nodes
    ADD COLUMN IF NOT EXISTS admitted_under_active_set VARCHAR(32);
ALTER TABLE knowledge_nodes
    ADD COLUMN IF NOT EXISTS admitted_under_pack_versions JSONB;
ALTER TABLE knowledge_nodes
    ADD COLUMN IF NOT EXISTS gdu_canonical BOOLEAN DEFAULT TRUE;
ALTER TABLE knowledge_nodes
    ADD COLUMN IF NOT EXISTS ldu_t_id BIGINT;

CREATE INDEX IF NOT EXISTS idx_nodes_node_type
    ON knowledge_nodes (project_id, node_type);
CREATE INDEX IF NOT EXISTS idx_nodes_completeness
    ON knowledge_nodes (project_id, completeness_tier);
CREATE INDEX IF NOT EXISTS idx_nodes_master_entity
    ON knowledge_nodes (master_entity_id) WHERE master_entity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_nodes_master_place
    ON knowledge_nodes (master_place_id) WHERE master_place_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_nodes_master_topic
    ON knowledge_nodes (master_topic_id) WHERE master_topic_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_nodes_envelope_search
    ON knowledge_nodes USING GIN (envelope jsonb_path_ops);


-- ═══════════════════════════════════════════════════════════════════════════
-- Track this migration
-- ═══════════════════════════════════════════════════════════════════════════

INSERT INTO schema_migrations (version, description)
VALUES (
    '003_envelope_rebuild',
    'Add entities, places, topics, extraction_passes tables + envelope JSONB on knowledge_nodes (universal extraction pipeline substrate)'
)
ON CONFLICT (version) DO NOTHING;
