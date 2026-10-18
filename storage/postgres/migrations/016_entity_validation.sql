-- Migration 016: entity validation state (Phase 5A)
--
-- Every entity/place/topic that gets created carries a validation_status
-- indicating whether the node has been cross-referenced against an
-- authoritative external source (Wikidata, Wikipedia, GeoNames, etc.).
--
--   pending       — created, queued for validator chain
--   validated     — confirmed by an authoritative source
--   unvalidated   — no source could confirm (rendered with warning badge)
--   rejected      — explicitly marked not-a-real-entity (by user or validator)
--
-- Once validated, the canonical_name/canonical_type may be UPDATED from
-- the source (e.g. "German military" → "Bundeswehr", type person → org).
-- The validated_by_source + validation_source_url + validation_source_id
-- triple lets the UI render "Validated via Wikidata Q165121".
--
-- Also extends the mirror triggers from migration 015 so knowledge_nodes
-- envelope carries the validation fields — the UI reads from envelope_summary.

BEGIN;

-- ── entities ──────────────────────────────────────────────────────────
ALTER TABLE entities
    ADD COLUMN IF NOT EXISTS validation_status      VARCHAR(16) NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS validated_by_source    VARCHAR(64),
    ADD COLUMN IF NOT EXISTS validation_source_id   VARCHAR(128),
    ADD COLUMN IF NOT EXISTS validation_source_url  TEXT,
    ADD COLUMN IF NOT EXISTS validation_confidence  NUMERIC(3,2),
    ADD COLUMN IF NOT EXISTS validated_at           TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS validation_attempts    INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS validation_last_error  TEXT;

CREATE INDEX IF NOT EXISTS idx_entities_validation_pending
    ON entities (validation_attempts, created_at)
    WHERE validation_status = 'pending';

CREATE INDEX IF NOT EXISTS idx_entities_validation_source
    ON entities (validated_by_source, validation_source_id)
    WHERE validated_by_source IS NOT NULL;

-- ── places ────────────────────────────────────────────────────────────
ALTER TABLE places
    ADD COLUMN IF NOT EXISTS validation_status      VARCHAR(16) NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS validated_by_source    VARCHAR(64),
    ADD COLUMN IF NOT EXISTS validation_source_id   VARCHAR(128),
    ADD COLUMN IF NOT EXISTS validation_source_url  TEXT,
    ADD COLUMN IF NOT EXISTS validation_confidence  NUMERIC(3,2),
    ADD COLUMN IF NOT EXISTS validated_at           TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS validation_attempts    INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS validation_last_error  TEXT;

CREATE INDEX IF NOT EXISTS idx_places_validation_pending
    ON places (validation_attempts, created_at)
    WHERE validation_status = 'pending';

-- ── topics ────────────────────────────────────────────────────────────
ALTER TABLE topics
    ADD COLUMN IF NOT EXISTS validation_status      VARCHAR(16) NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS validated_by_source    VARCHAR(64),
    ADD COLUMN IF NOT EXISTS validation_source_id   VARCHAR(128),
    ADD COLUMN IF NOT EXISTS validation_source_url  TEXT,
    ADD COLUMN IF NOT EXISTS validation_confidence  NUMERIC(3,2),
    ADD COLUMN IF NOT EXISTS validated_at           TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS validation_attempts    INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS validation_last_error  TEXT;

CREATE INDEX IF NOT EXISTS idx_topics_validation_pending
    ON topics (validation_attempts, created_at)
    WHERE validation_status = 'pending';

-- ── rebuild mirror trigger functions to propagate validation state into
--    knowledge_nodes.envelope so envelope_summary / UI can see it ──────
CREATE OR REPLACE FUNCTION mirror_entity_to_knowledge_node()
RETURNS TRIGGER AS $$
DECLARE
    v_node_id    text := 'entity:' || NEW.entity_id::text;
    v_envelope   jsonb;
BEGIN
    v_envelope := jsonb_build_object(
        'envelope_version', 'v1',
        'node_id',          v_node_id,
        'node_type',        'entity',
        'subtype',          NEW.canonical_type,
        'identity', jsonb_build_object(
            'title_display',  NEW.canonical_name,
            'canonical_name', NEW.canonical_name,
            'aliases',        COALESCE(to_jsonb(NEW.aliases), '[]'::jsonb)
        ),
        'evidence', jsonb_build_object(
            'epistemic_type', 'observation',
            'source_count',   NEW.signal_count
        ),
        'validation', jsonb_build_object(
            'status',      NEW.validation_status,
            'source',      NEW.validated_by_source,
            'source_id',   NEW.validation_source_id,
            'source_url',  NEW.validation_source_url,
            'confidence',  NEW.validation_confidence,
            'validated_at', NEW.validated_at
        ),
        'completeness', jsonb_build_object(
            'score', COALESCE(NEW.completeness_score, 0)::float,
            'tier',  COALESCE(NEW.completeness_tier, 'bronze')
        )
    );

    INSERT INTO knowledge_nodes (
        node_id, project_id, tenant_id,
        title, description, epistemic_type,
        envelope, envelope_version, node_type, subtype,
        master_entity_id,
        admitted_under_active_set, admitted_under_pack_versions,
        gdu_canonical, ldu_t_id
    ) VALUES (
        v_node_id, 'default', NEW.tenant_id,
        NEW.canonical_name, '', 'observation',
        v_envelope, 'v1', 'entity', NEW.canonical_type,
        NEW.entity_id,
        NEW.admitted_under_active_set, COALESCE(NEW.admitted_under_pack_versions, '{}'::jsonb),
        COALESCE(NEW.gdu_canonical, true), NEW.ldu_t_id
    )
    ON CONFLICT (node_id) DO UPDATE SET
        title         = EXCLUDED.title,
        envelope      = EXCLUDED.envelope,
        node_type     = EXCLUDED.node_type,
        subtype       = EXCLUDED.subtype,
        master_entity_id = EXCLUDED.master_entity_id,
        updated_at    = NOW();

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


CREATE OR REPLACE FUNCTION mirror_place_to_knowledge_node()
RETURNS TRIGGER AS $$
DECLARE
    v_node_id  text := 'place:' || NEW.place_id::text;
    v_envelope jsonb;
    v_lat double precision;
    v_lon double precision;
BEGIN
    IF NEW.coordinates IS NOT NULL THEN
        v_lat := ST_Y(NEW.coordinates::geometry);
        v_lon := ST_X(NEW.coordinates::geometry);
    END IF;

    v_envelope := jsonb_build_object(
        'envelope_version', 'v1',
        'node_id',          v_node_id,
        'node_type',        'place',
        'subtype',          NEW.canonical_type,
        'identity', jsonb_build_object(
            'title_display',  NEW.canonical_name,
            'canonical_name', NEW.canonical_name,
            'aliases',        COALESCE(to_jsonb(NEW.aliases), '[]'::jsonb)
        ),
        'spatial', jsonb_build_object(
            'coordinates', CASE
                WHEN v_lat IS NOT NULL THEN jsonb_build_object('lat', v_lat, 'lon', v_lon)
                ELSE NULL
            END,
            'place_text',  jsonb_build_array(NEW.canonical_name)
        ),
        'evidence',     jsonb_build_object('source_count', NEW.mention_count, 'epistemic_type', 'observation'),
        'validation', jsonb_build_object(
            'status',      NEW.validation_status,
            'source',      NEW.validated_by_source,
            'source_id',   NEW.validation_source_id,
            'source_url',  NEW.validation_source_url,
            'confidence',  NEW.validation_confidence,
            'validated_at', NEW.validated_at
        ),
        'completeness', jsonb_build_object('tier', 'bronze', 'score', 0.5)
    );

    INSERT INTO knowledge_nodes (
        node_id, project_id, tenant_id,
        title, description, epistemic_type,
        envelope, envelope_version, node_type, subtype,
        master_place_id, latitude, longitude,
        admitted_under_active_set, gdu_canonical, ldu_t_id
    ) VALUES (
        v_node_id, 'default', NEW.tenant_id,
        NEW.canonical_name, '', 'observation',
        v_envelope, 'v1', 'place', NEW.canonical_type,
        NEW.place_id, v_lat, v_lon,
        NEW.admitted_under_active_set, COALESCE(NEW.gdu_canonical, true), NEW.ldu_t_id
    )
    ON CONFLICT (node_id) DO UPDATE SET
        title         = EXCLUDED.title,
        envelope      = EXCLUDED.envelope,
        latitude      = EXCLUDED.latitude,
        longitude     = EXCLUDED.longitude,
        master_place_id = EXCLUDED.master_place_id,
        updated_at    = NOW();

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


CREATE OR REPLACE FUNCTION mirror_topic_to_knowledge_node()
RETURNS TRIGGER AS $$
DECLARE
    v_node_id  text := 'topic:' || NEW.topic_id::text;
    v_envelope jsonb;
BEGIN
    v_envelope := jsonb_build_object(
        'envelope_version', 'v1',
        'node_id',          v_node_id,
        'node_type',        'topic',
        'subtype',          NEW.canonical_type,
        'identity', jsonb_build_object(
            'title_display',  NEW.canonical_name,
            'canonical_name', NEW.canonical_name
        ),
        'evidence',     jsonb_build_object('source_count', NEW.mention_count, 'epistemic_type', 'observation'),
        'validation', jsonb_build_object(
            'status',      NEW.validation_status,
            'source',      NEW.validated_by_source,
            'source_id',   NEW.validation_source_id,
            'source_url',  NEW.validation_source_url,
            'confidence',  NEW.validation_confidence,
            'validated_at', NEW.validated_at
        ),
        'completeness', jsonb_build_object('tier', 'bronze', 'score', 0.5)
    );

    INSERT INTO knowledge_nodes (
        node_id, project_id, tenant_id,
        title, description, epistemic_type,
        envelope, envelope_version, node_type, subtype,
        master_topic_id,
        admitted_under_active_set, gdu_canonical, ldu_t_id
    ) VALUES (
        v_node_id, 'default', NEW.tenant_id,
        NEW.canonical_name, '', 'observation',
        v_envelope, 'v1', 'topic', NEW.canonical_type,
        NEW.topic_id,
        NEW.admitted_under_active_set, COALESCE(NEW.gdu_canonical, true), NEW.ldu_t_id
    )
    ON CONFLICT (node_id) DO UPDATE SET
        title         = EXCLUDED.title,
        envelope      = EXCLUDED.envelope,
        master_topic_id = EXCLUDED.master_topic_id,
        updated_at    = NOW();

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- Triggers already exist from migration 015; no need to recreate them.
-- Re-firing them once so the new envelope shape (with validation block)
-- replaces the old one on every existing row.
UPDATE entities SET updated_at = NOW();
UPDATE places   SET last_seen  = COALESCE(last_seen, NOW());
UPDATE topics   SET last_seen  = COALESCE(last_seen, NOW());

COMMIT;
