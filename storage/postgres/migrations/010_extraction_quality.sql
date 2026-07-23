-- Migration 010 — Extraction quality upgrades
--
-- Adds infrastructure for:
--   Phase 2a — per-mention role/context (entity_mentions)
--   Phase 2b — trigram alias linking (pg_trgm + GIN index)
--   Phase 3  — event nodes as first-class (events table)
--   Phase 4  — address extraction hooks (addresses table + place hierarchy)
--   Phase 5  — source-attributed conflicts (node_conflicts)
--   Phase 6  — multi-hop reasoning indexes on knowledge_edges
--
-- All new tables carry LDU lineage columns so any pack-version change
-- can trace which rows were admitted under which vocab state.

-- ─── pg_trgm for fuzzy alias matching ───────────────────────────────
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Trigram similarity index on entity normalized_name for Phase 2b
-- soft dedup: "Vladimir Putin" finds existing "Putin" via similarity.
CREATE INDEX IF NOT EXISTS idx_entities_normalized_name_trgm
    ON entities USING gin (normalized_name gin_trgm_ops);

-- GIN index on aliases array so lookup by alias is O(log n)
CREATE INDEX IF NOT EXISTS idx_entities_aliases_gin
    ON entities USING gin (aliases);


-- ─── entity_mentions — Phase 2a ─────────────────────────────────────
-- One row per (article, entity) occurrence. Stores role, context
-- snippet, offset, confidence. This is the bridge between the
-- canonical entity row (entities) and the specific article that
-- mentioned it — the thing that lets us answer "in which 47 articles
-- was Putin the subject, and what did each say about him?"
CREATE TABLE IF NOT EXISTS entity_mentions (
    mention_id          BIGSERIAL PRIMARY KEY,
    tenant_id           VARCHAR(255) NOT NULL DEFAULT 'default',

    -- What entity + where
    entity_id           BIGINT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    signal_rid          BIGINT,                       -- which signal (article) mentioned it
    node_id             VARCHAR(255),                 -- which knowledge_node
    source_url          TEXT,                         -- for research derivatives without signal_rid

    -- How it was mentioned
    name_raw            TEXT,                         -- surface form in this article
    role                VARCHAR(32),                  -- subject|target|actor|author|witness|mention
    context_snippet     TEXT,                         -- sentence around the mention
    first_offset        INT,                          -- char offset in source body
    confidence          NUMERIC(3,2) DEFAULT 0.7,

    -- Temporal marker (the time the mention is ABOUT, not when extracted)
    event_time          TIMESTAMPTZ,

    -- LDU lineage
    admitted_under_active_set     VARCHAR(32),
    admitted_under_pack_versions  JSONB DEFAULT '{}'::jsonb,
    gdu_canonical                 BOOLEAN NOT NULL DEFAULT TRUE,
    ldu_t_id                      BIGINT,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One mention per (entity, signal, offset) — idempotent upsert
CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_mentions_unique
    ON entity_mentions (entity_id, signal_rid, COALESCE(first_offset, 0))
    WHERE signal_rid IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_entity_mentions_entity
    ON entity_mentions (entity_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_entity_mentions_signal
    ON entity_mentions (signal_rid);

CREATE INDEX IF NOT EXISTS idx_entity_mentions_node
    ON entity_mentions (node_id);

-- Role filter index — "show me every article where Putin was SUBJECT"
CREATE INDEX IF NOT EXISTS idx_entity_mentions_role
    ON entity_mentions (entity_id, role)
    WHERE role IN ('subject', 'target', 'actor', 'perpetrator', 'author');


-- ─── events — Phase 3 ───────────────────────────────────────────────
-- First-class events. When a story mentions "strike on Kharkiv on Oct 3",
-- we extract it as an event node with its own identity, temporal +
-- spatial anchor, and participant links via knowledge_edges.
CREATE TABLE IF NOT EXISTS events (
    event_id            BIGSERIAL PRIMARY KEY,
    tenant_id           VARCHAR(255) NOT NULL DEFAULT 'default',
    node_id             VARCHAR(255),                 -- knowledge_nodes.node_id when materialized

    -- Classification (nodevocab.event.subtypes)
    event_type          VARCHAR(64) NOT NULL,         -- strike|meeting|arrival|departure|broadcast|publication|alert|sighting|transaction|statement
    title               TEXT NOT NULL,
    narrative           TEXT,

    -- Temporal anchor (precise when known, window when not)
    event_time          TIMESTAMPTZ,
    event_end_time      TIMESTAMPTZ,
    temporal_precision  VARCHAR(16),                  -- exact|hour|day|week|month

    -- Spatial anchor
    latitude            DOUBLE PRECISION,
    longitude           DOUBLE PRECISION,
    place_text          TEXT[] DEFAULT '{}',
    place_node_id       VARCHAR(255),                 -- links to place node

    -- Participants (denormalized for fast queries; canonical via knowledge_edges)
    actor_entity_ids    BIGINT[] DEFAULT '{}',
    target_entity_ids   BIGINT[] DEFAULT '{}',

    -- Evidence
    source_signal_rids  BIGINT[] DEFAULT '{}',
    source_count        INT DEFAULT 1,
    confidence          NUMERIC(3,2) DEFAULT 0.6,

    -- LDU lineage
    admitted_under_active_set     VARCHAR(32),
    admitted_under_pack_versions  JSONB DEFAULT '{}'::jsonb,
    gdu_canonical                 BOOLEAN NOT NULL DEFAULT TRUE,
    ldu_t_id                      BIGINT,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_events_time
    ON events (event_time DESC);

CREATE INDEX IF NOT EXISTS idx_events_type
    ON events (event_type, event_time DESC);

CREATE INDEX IF NOT EXISTS idx_events_actor
    ON events USING gin (actor_entity_ids);

CREATE INDEX IF NOT EXISTS idx_events_place
    ON events (place_node_id, event_time DESC);


-- ─── addresses — Phase 4 ────────────────────────────────────────────
-- Structured addresses. An address can belong to a person (home), an
-- organization (HQ), an asset (property), or stand alone as a place.
-- It's polymorphic via entity_id or place_node_id.
CREATE TABLE IF NOT EXISTS addresses (
    address_id          BIGSERIAL PRIMARY KEY,
    tenant_id           VARCHAR(255) NOT NULL DEFAULT 'default',

    -- Owner (polymorphic — one of these is typically set)
    entity_id           BIGINT REFERENCES entities(entity_id) ON DELETE CASCADE,
    place_node_id       VARCHAR(255),                 -- when address IS the place
    asset_id            UUID,                          -- references assets table if exists

    -- Structured fields
    line1               TEXT,
    line2               TEXT,
    city                TEXT,
    admin_region        TEXT,                         -- state/province/prefecture
    postal_code         VARCHAR(32),
    country             VARCHAR(64),
    country_code        CHAR(2),

    -- Geocoded
    latitude            DOUBLE PRECISION,
    longitude           DOUBLE PRECISION,
    geocoded_at         TIMESTAMPTZ,
    geocoder            VARCHAR(32),                  -- nominatim|google|manual

    -- Classification
    address_type        VARCHAR(32),                  -- home|hq|registered|mailing|property|facility
    is_primary          BOOLEAN DEFAULT FALSE,
    verified            BOOLEAN DEFAULT FALSE,

    -- Evidence
    source_signal_rids  BIGINT[] DEFAULT '{}',
    confidence          NUMERIC(3,2) DEFAULT 0.7,

    -- LDU lineage
    admitted_under_active_set     VARCHAR(32),
    gdu_canonical                 BOOLEAN NOT NULL DEFAULT TRUE,
    ldu_t_id                      BIGINT,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_addresses_entity
    ON addresses (entity_id, is_primary DESC);

CREATE INDEX IF NOT EXISTS idx_addresses_place
    ON addresses (place_node_id);

CREATE INDEX IF NOT EXISTS idx_addresses_country
    ON addresses (country_code, city);


-- ─── node_conflicts — Phase 5 ───────────────────────────────────────
-- When two sources disagree on a field (date, place, spelling,
-- attribution), record it with FULL source attribution. UI can show
-- "CNN says Oct 3, Reuters says Oct 4 — unresolved". Resolution
-- closes the conflict with an attribution to who/what decided.
CREATE TABLE IF NOT EXISTS node_conflicts (
    conflict_id         BIGSERIAL PRIMARY KEY,
    tenant_id           VARCHAR(255) NOT NULL DEFAULT 'default',

    -- Which node is in conflict (an entity, event, claim, etc.)
    node_id             VARCHAR(255) NOT NULL,
    entity_id           BIGINT,
    event_id            BIGINT,

    -- What field disagrees
    field_path          VARCHAR(128) NOT NULL,        -- envelope dotted path: "temporal.event_time", "spatial.coordinates", "identity.title_display"
    field_kind          VARCHAR(32),                  -- date|place|attribution|spelling|number|stance

    -- The two (or more) competing values
    value_a             JSONB NOT NULL,
    sources_a           JSONB NOT NULL DEFAULT '[]'::jsonb,  -- [{signal_rid, url, outlet, confidence, extracted_at}]
    value_b             JSONB NOT NULL,
    sources_b           JSONB NOT NULL DEFAULT '[]'::jsonb,
    additional_values   JSONB DEFAULT '[]'::jsonb,    -- [{value, sources[]}] when >2 competing values

    -- Lifecycle
    detected_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    detected_by         VARCHAR(64),                  -- scanner_v1|llm_review|user
    resolution          VARCHAR(32) NOT NULL DEFAULT 'unresolved',
                        -- unresolved|a|b|additional|both_valid|reconciled
    resolution_note     TEXT,
    resolved_at         TIMESTAMPTZ,
    resolved_by         VARCHAR(64),

    severity            VARCHAR(16) DEFAULT 'warning', -- info|warning|critical

    -- LDU lineage
    admitted_under_active_set     VARCHAR(32),
    gdu_canonical                 BOOLEAN NOT NULL DEFAULT TRUE,
    ldu_t_id                      BIGINT,

    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conflicts_node
    ON node_conflicts (node_id, resolution);

CREATE INDEX IF NOT EXISTS idx_conflicts_open
    ON node_conflicts (tenant_id, resolution, severity, detected_at DESC)
    WHERE resolution = 'unresolved';

CREATE INDEX IF NOT EXISTS idx_conflicts_entity
    ON node_conflicts (entity_id)
    WHERE entity_id IS NOT NULL;


-- ─── knowledge_edges — Phase 6 multi-hop reasoning ──────────────────
-- Two indexes so recursive CTE traversals are cheap in both directions.
CREATE INDEX IF NOT EXISTS idx_edges_source_rel
    ON knowledge_edges (source_node_id, relationship, strength DESC);

CREATE INDEX IF NOT EXISTS idx_edges_target_rel
    ON knowledge_edges (target_node_id, relationship, strength DESC);

-- Partial index on strong edges only — most path queries filter weak
CREATE INDEX IF NOT EXISTS idx_edges_strong
    ON knowledge_edges (source_node_id, target_node_id)
    WHERE strength >= 0.7;
