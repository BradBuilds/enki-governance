-- ─────────────────────────────────────────────────────────────────────────────
-- Migration 026: Append-Only Enrichment Architecture
--
-- Fixes the pipeline break where workers tried to UPDATE append-only tables.
-- Instead of modifying signals or entities, workers APPEND enrichment records.
-- Every enrichment and entity change is a new row, hash-chained, auditable.
--
-- Tables created:
--   signal_enrichments  — LLM extraction results (versioned per signal)
--   entity_events       — entity lifecycle (create, validate, merge, update)
--   entity_redirects    — merge pointers (entity A absorbed into entity B)
--   node_revisions      — knowledge node edit history
-- ─────────────────────────────────────────────────────────────────────────────

-- ─── Signal Enrichments ──────────────────────────────────────────────────────
-- Each LLM run on a signal produces one row. Latest tier wins for display.
-- Chain: each enrichment hashes the previous one for tamper detection.

CREATE TABLE IF NOT EXISTS signal_enrichments (
    enrichment_id       BIGSERIAL PRIMARY KEY,
    signal_rid          BIGINT NOT NULL,          -- FK to signals.rid
    tenant_id           VARCHAR(255) NOT NULL,
    tier                INT NOT NULL DEFAULT 1,   -- 1=keyword, 2=llm, 3=contextual
    method              VARCHAR(50) NOT NULL,     -- keyword, llm, llm_contextual, manual
    model               VARCHAR(255),             -- gemma4:e4b, null for keyword

    -- Extracted data
    entities            JSONB DEFAULT '[]',       -- [{name, type, role, significance, quote}]
    relationships       JSONB DEFAULT '[]',       -- [{source, target, type, direction, confidence}]
    event_type          VARCHAR(100),             -- political, military, cyber, etc.
    importance          INT,                      -- 1-5
    keywords            TEXT[],                   -- extracted keywords
    topics              TEXT[],                   -- matched topic IDs
    summary             TEXT,                     -- LLM-generated summary

    -- Context (what was the LLM looking at)
    context_signals     BIGINT[],                 -- other signals used for contextual enrichment
    prompt_version      VARCHAR(50),              -- track prompt template changes

    -- Chain
    enrichment_hash     VARCHAR(64),              -- SHA256(previous_hash + this record)
    previous_hash       VARCHAR(64),              -- hash of previous enrichment for this signal

    -- Timestamps
    extracted_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processing_ms       INT                       -- how long the LLM took
);

-- Fast lookup: latest enrichment per signal
CREATE INDEX IF NOT EXISTS idx_enrichments_signal
    ON signal_enrichments(signal_rid, tier DESC, extracted_at DESC);

-- Fast lookup: all enrichments for a tenant (feed display)
CREATE INDEX IF NOT EXISTS idx_enrichments_tenant
    ON signal_enrichments(tenant_id, extracted_at DESC);

-- Find un-enriched signals (what the worker needs)
CREATE INDEX IF NOT EXISTS idx_enrichments_tier
    ON signal_enrichments(tenant_id, tier);

-- Append-only trigger (no updates, no deletes)
DROP TRIGGER IF EXISTS no_mutate_signal_enrichments ON signal_enrichments;
CREATE TRIGGER no_mutate_signal_enrichments
    BEFORE UPDATE OR DELETE ON signal_enrichments
    FOR EACH ROW EXECUTE FUNCTION prevent_mutation();


-- ─── Entity Events ───────────────────────────────────────────────────────────
-- Every change to an entity is a new row. The entity "is" the sum of its events.
-- Event types: created, enriched, validated, merged, split, rejected, updated, linked

CREATE TABLE IF NOT EXISTS entity_events (
    event_id            BIGSERIAL PRIMARY KEY,
    entity_id           BIGINT NOT NULL,          -- FK to entities.entity_id
    tenant_id           VARCHAR(255) NOT NULL,
    event_type          VARCHAR(50) NOT NULL,      -- created, enriched, validated, merged, etc.
    source              VARCHAR(100) NOT NULL,     -- feed_enrichment, llm_confirmer, wikidata, user, etc.

    -- What changed
    data                JSONB NOT NULL DEFAULT '{}', -- event-specific payload
    -- For 'created':   { canonical_name, canonical_type, confidence, source_signal_rid }
    -- For 'enriched':  { added_fields: {title, nationality, ...}, confidence_delta }
    -- For 'validated': { wikidata_id, validated_fields, validation_confidence }
    -- For 'merged':    { absorbed_entity_id, absorbed_name, signals_transferred }
    -- For 'rejected':  { reason, rejected_by }
    -- For 'linked':    { signal_rid, role, significance, quote }

    -- Context
    signal_rid          BIGINT,                    -- signal that triggered this event (if any)
    caused_by           VARCHAR(255),              -- worker name or user identity_id

    -- Chain
    event_hash          VARCHAR(64),
    previous_hash       VARCHAR(64),

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_entity_events_entity
    ON entity_events(entity_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_entity_events_tenant
    ON entity_events(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_entity_events_type
    ON entity_events(entity_id, event_type);

-- Append-only
DROP TRIGGER IF EXISTS no_mutate_entity_events ON entity_events;
CREATE TRIGGER no_mutate_entity_events
    BEFORE UPDATE OR DELETE ON entity_events
    FOR EACH ROW EXECUTE FUNCTION prevent_mutation();


-- ─── Entity Redirects ────────────────────────────────────────────────────────
-- entity_redirects is owned by migration 006_entity_resolution.sql with
-- columns (old_entity_id, new_entity_id, merged_at, merged_by, merge_reason).
-- An earlier draft of this migration tried to redefine the table with
-- (source_entity_id, target_entity_id, …); that attempt silently no-op'd on
-- the CREATE TABLE IF NOT EXISTS and then crashed on the CREATE INDEX,
-- breaking fresh-DB bootstrap. The table's shape is whatever 006 created;
-- this migration only adds the append-only trigger.
DROP TRIGGER IF EXISTS no_mutate_entity_redirects ON entity_redirects;
CREATE TRIGGER no_mutate_entity_redirects
    BEFORE UPDATE OR DELETE ON entity_redirects
    FOR EACH ROW EXECUTE FUNCTION prevent_mutation();


-- ─── Node Revisions ──────────────────────────────────────────────────────────
-- Every change to a knowledge node (card) is a new revision.
-- Latest revision = current display. All revisions preserved.

CREATE TABLE IF NOT EXISTS node_revisions (
    revision_id         BIGSERIAL PRIMARY KEY,
    node_id             VARCHAR(255) NOT NULL,
    tenant_id           VARCHAR(255) NOT NULL,
    revision_number     INT NOT NULL DEFAULT 1,

    -- Snapshot of node state at this revision
    title               TEXT,
    summary             TEXT,
    facts               JSONB,
    node_type           VARCHAR(50),
    subtype             VARCHAR(50),

    -- What changed
    change_type         VARCHAR(50),               -- created, enriched, merged, manual_edit
    change_source       VARCHAR(255),              -- worker name or user
    change_detail       TEXT,                       -- human-readable description

    -- Chain
    revision_hash       VARCHAR(64),
    previous_hash       VARCHAR(64),

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_node_revisions_node
    ON node_revisions(node_id, revision_number DESC);

-- Append-only
DROP TRIGGER IF EXISTS no_mutate_node_revisions ON node_revisions;
CREATE TRIGGER no_mutate_node_revisions
    BEFORE UPDATE OR DELETE ON node_revisions
    FOR EACH ROW EXECUTE FUNCTION prevent_mutation();


-- ─── Helper View: Latest Enrichment Per Signal ──────────────────────────────
-- Used by the feed display to join signals with their latest enrichment.

CREATE OR REPLACE VIEW signal_latest_enrichment AS
SELECT DISTINCT ON (signal_rid)
    signal_rid,
    tenant_id,
    tier,
    method,
    model,
    entities,
    relationships,
    event_type,
    importance,
    keywords,
    topics,
    summary,
    extracted_at
FROM signal_enrichments
ORDER BY signal_rid, tier DESC, extracted_at DESC;


-- ─── Helper View: Entity Current State ───────────────────────────────────────
-- Reconstructs the "current" entity from its event chain.
-- For display purposes — the authoritative record is the event chain itself.

CREATE OR REPLACE VIEW entity_current_state AS
SELECT DISTINCT ON (entity_id)
    entity_id,
    tenant_id,
    event_type AS last_event_type,
    data AS last_event_data,
    source AS last_event_source,
    created_at AS last_event_at
FROM entity_events
ORDER BY entity_id, created_at DESC;
