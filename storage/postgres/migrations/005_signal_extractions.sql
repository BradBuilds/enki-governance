-- Migration 005: signal_extractions — the extraction substrate.
--
-- Separates "extracted data" from "knowledge nodes". After this migration:
--
--   signals              — LDU-T truth (append-only, every signal forever)
--   signal_extractions   — structured envelope per signal (THIS TABLE, NEW)
--   knowledge_nodes      — curated research artifacts (dossiers, questions,
--                          theories, assessments, chronicles, cases, patterns)
--
-- signal_extractions is 1:1 with signals (one row per extracted signal).
-- Envelope JSONB, master_*_id FKs, LDU lineage, completeness score.
-- This is where articles, GPS pings, sensor readings, weather observations
-- land after the extraction runner processes them.
--
-- knowledge_nodes is now reserved EXCLUSIVELY for research artifacts —
-- things a human is tracking, questioning, or summarizing. Nothing in
-- knowledge_nodes comes from "one signal became one node". Nodes are
-- created by rules, research workers, chronicle workers, pattern
-- detectors, or humans.
--
-- Dedup primitives preserved: every signal_extraction carries
-- (master_entity_id, master_place_id, master_topic_id) FKs into the
-- existing master tables, so places and topics are still deduplicated
-- exactly as before. Research workers walk signal_extractions to find
-- new evidence for the nodes they maintain.
--
-- LDU lineage on every row: admitted_under_active_set + pack_versions
-- so we can audit which vocabulary version governed each extraction.
--
-- Idempotent. Additive only. Does not touch signals, knowledge_nodes,
-- or any other existing table.

CREATE TABLE IF NOT EXISTS signal_extractions (
    extraction_id          BIGSERIAL PRIMARY KEY,
    tenant_id              VARCHAR(255) NOT NULL DEFAULT 'default',

    -- Which signal this extraction came from. Always exactly one.
    signal_rid             BIGINT NOT NULL,

    -- Canonical signal type from signalvocab (validated at extraction time)
    signal_type            VARCHAR(128) NOT NULL,

    -- Deterministic node_id for dedup — sha1 of (signal_type, canonical_url,
    -- outlet_slug) or similar. Two signals with the same node_id are the
    -- same real-world event (e.g. two RSS feeds carrying the same article)
    -- and get UPSERTed into one row. THIS IS THE DEDUP KEY.
    node_id                VARCHAR(255) NOT NULL,

    -- The canonical envelope (identity, summary, temporal, spatial, etc.)
    envelope               JSONB NOT NULL DEFAULT '{}'::jsonb,
    envelope_version       VARCHAR(16) NOT NULL DEFAULT 'v1',

    -- Node type / subtype classification from the envelope
    node_type              VARCHAR(32),
    subtype                VARCHAR(64),

    -- Completeness scoring (populated by Phase 8 scorer)
    completeness_score     NUMERIC(4,3) DEFAULT 0.000,
    completeness_tier      VARCHAR(16),

    -- Master table references — resolver stamps these during extraction
    master_entity_id       BIGINT REFERENCES entities(entity_id) ON DELETE SET NULL,
    master_place_id        BIGINT REFERENCES places(place_id)   ON DELETE SET NULL,
    master_topic_id        BIGINT REFERENCES topics(topic_id)   ON DELETE SET NULL,

    -- Source + time cross-reference fields (promoted out of envelope for
    -- fast dedup queries: "do we already have this URL? this title+time?")
    source_url             TEXT,
    source_outlet          VARCHAR(255),
    content_hash           VARCHAR(64),        -- sha256 of title+body (detects content dup
                                               -- even when URLs differ)
    event_time             TIMESTAMPTZ,        -- envelope.temporal.event_time
    ingested_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- LDU lineage — governance at extraction time
    admitted_under_active_set     VARCHAR(32),
    admitted_under_pack_versions  JSONB,
    gdu_canonical                 BOOLEAN NOT NULL DEFAULT TRUE,
    ldu_t_id                      BIGINT,

    -- Flags that let the research worker and UI filter efficiently
    llm_processed          BOOLEAN NOT NULL DEFAULT FALSE,  -- has deep_pass run?
    feed_worthy            BOOLEAN NOT NULL DEFAULT TRUE,   -- appears in Stream tab?
    superseded_by          VARCHAR(255),                    -- node_id of a higher-level
                                                            -- summary (e.g. a track that
                                                            -- bundled this position)

    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT signal_extractions_unique_node UNIQUE (tenant_id, node_id)
);

-- Indexes for the hot query paths
CREATE INDEX IF NOT EXISTS idx_sig_extractions_tenant_time
    ON signal_extractions (tenant_id, event_time DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_sig_extractions_signal_rid
    ON signal_extractions (signal_rid);
CREATE INDEX IF NOT EXISTS idx_sig_extractions_signal_type
    ON signal_extractions (tenant_id, signal_type);
CREATE INDEX IF NOT EXISTS idx_sig_extractions_topic
    ON signal_extractions (master_topic_id) WHERE master_topic_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sig_extractions_place
    ON signal_extractions (master_place_id) WHERE master_place_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sig_extractions_entity
    ON signal_extractions (master_entity_id) WHERE master_entity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sig_extractions_source_url
    ON signal_extractions (source_url) WHERE source_url IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sig_extractions_content_hash
    ON signal_extractions (content_hash) WHERE content_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sig_extractions_feed_worthy
    ON signal_extractions (tenant_id, feed_worthy, event_time DESC)
    WHERE feed_worthy = TRUE AND superseded_by IS NULL;
CREATE INDEX IF NOT EXISTS idx_sig_extractions_envelope_gin
    ON signal_extractions USING GIN (envelope jsonb_path_ops);

-- ═══════════════════════════════════════════════════════════════════════════
-- RESEARCH_PASSES — log of every research worker run against a node.
--
-- Every time the research worker updates a knowledge_node with new evidence
-- from signal_extractions, it writes one row here. Lets us audit exactly
-- which extractions influenced which nodes and when, and lets the research
-- worker use last_research_at to only consider new evidence.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS research_passes (
    pass_id                BIGSERIAL PRIMARY KEY,
    tenant_id              VARCHAR(255) NOT NULL DEFAULT 'default',

    node_id                VARCHAR(255) NOT NULL,         -- knowledge_nodes.node_id
    started_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at           TIMESTAMPTZ,

    -- What evidence did this pass consider?
    extractions_considered INT NOT NULL DEFAULT 0,
    extraction_ids         BIGINT[] DEFAULT '{}',

    -- What did this pass change?
    fields_updated         JSONB DEFAULT '[]'::jsonb,    -- ["summary.narrative", ...]
    entities_added         INT DEFAULT 0,
    places_added           INT DEFAULT 0,
    topics_added           INT DEFAULT 0,
    key_facts_added        INT DEFAULT 0,
    contradictions_flagged INT DEFAULT 0,

    -- LLM details
    llm_model              VARCHAR(128),                  -- e.g. 'gemma4:e2b'
    llm_duration_ms        INT,
    llm_prompt_version     VARCHAR(32),

    -- Outcome
    success                BOOLEAN NOT NULL DEFAULT TRUE,
    error                  TEXT,
    new_completeness       NUMERIC(4,3),

    -- LDU lineage
    admitted_under_active_set     VARCHAR(32),
    admitted_under_pack_versions  JSONB,
    gdu_canonical                 BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_research_passes_node
    ON research_passes (node_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_research_passes_started
    ON research_passes (tenant_id, started_at DESC);

-- ═══════════════════════════════════════════════════════════════════════════
-- KNOWLEDGE_NODES additive columns for research tracking
--
-- The research worker reads last_research_at to decide which extractions
-- are "new since last pass". Node types from the updated taxonomy get
-- stamped here so the Intelligence tab can filter correctly.
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE knowledge_nodes
    ADD COLUMN IF NOT EXISTS last_research_at TIMESTAMPTZ;
ALTER TABLE knowledge_nodes
    ADD COLUMN IF NOT EXISTS research_pass_count INT DEFAULT 0;
ALTER TABLE knowledge_nodes
    ADD COLUMN IF NOT EXISTS research_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE knowledge_nodes
    ADD COLUMN IF NOT EXISTS cited_extraction_ids BIGINT[] DEFAULT '{}';

-- ═══════════════════════════════════════════════════════════════════════════
-- UNKNOWN_SIGNAL_TYPES — tracking table for signal types the system has
-- never seen before. Ingestion creates one row per novel type (not per
-- signal), shape profiler fills in the proposed mapping, user resolves
-- via Ignore / Promote in the Alerts tab.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS unknown_signal_types (
    type_id                BIGSERIAL PRIMARY KEY,
    tenant_id              VARCHAR(255) NOT NULL DEFAULT 'default',
    signal_type            VARCHAR(255) NOT NULL,
    status                 VARCHAR(32) NOT NULL DEFAULT 'pending',
                           -- pending | ignored | promoted
    first_seen_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    signal_count           BIGINT NOT NULL DEFAULT 1,
    sample_payloads        JSONB DEFAULT '[]'::jsonb,  -- up to 5 redacted samples
    proposed_mapping       JSONB,                      -- from mapping learner
    resolved_by            VARCHAR(255),
    resolved_at            TIMESTAMPTZ,
    notes                  TEXT,

    CONSTRAINT unknown_sig_types_unique UNIQUE (tenant_id, signal_type),
    CONSTRAINT unknown_sig_types_status_valid CHECK (
        status IN ('pending', 'ignored', 'promoted')
    )
);

CREATE INDEX IF NOT EXISTS idx_unknown_sig_types_status
    ON unknown_signal_types (status, last_seen_at DESC);

INSERT INTO schema_migrations (version, description)
VALUES ('005_signal_extractions',
        'Split extractions from nodes: signal_extractions table, research_passes log, unknown_signal_types tracking, knowledge_nodes research columns')
ON CONFLICT (version) DO NOTHING;
