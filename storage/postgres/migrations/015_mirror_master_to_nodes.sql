-- Migration 015: mirror master entities/places/topics into knowledge_nodes
--
-- Phase 4 surfaced the gap: events became first-class knowledge_nodes rows
-- in Phase 1.1, but entities/places/topics never got the same treatment.
-- Result: chip-click on an entity name 404s because there's no
-- knowledge_nodes row to render at /nodes/entity:42.
--
-- Fix: a tiny per-row trigger that upserts a knowledge_nodes row whenever
-- an entity/place/topic is inserted or its name changes. The card detail
-- page can then render the master node uniformly with everything else.
-- The triggers also handle the backfill of existing rows in this migration.

BEGIN;

-- ── helper: build a synthetic envelope from a master-table row ────────
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
    -- Pull lat/lon from PostGIS geography column when present
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


-- ── triggers (drop+create so re-runs are idempotent) ──────────────────
DROP TRIGGER IF EXISTS trg_mirror_entity ON entities;
CREATE TRIGGER trg_mirror_entity
    AFTER INSERT OR UPDATE ON entities
    FOR EACH ROW EXECUTE FUNCTION mirror_entity_to_knowledge_node();

DROP TRIGGER IF EXISTS trg_mirror_place ON places;
CREATE TRIGGER trg_mirror_place
    AFTER INSERT OR UPDATE ON places
    FOR EACH ROW EXECUTE FUNCTION mirror_place_to_knowledge_node();

DROP TRIGGER IF EXISTS trg_mirror_topic ON topics;
CREATE TRIGGER trg_mirror_topic
    AFTER INSERT OR UPDATE ON topics
    FOR EACH ROW EXECUTE FUNCTION mirror_topic_to_knowledge_node();


-- ── backfill existing master rows ─────────────────────────────────────
-- Touching them with a no-op UPDATE fires the trigger, mirroring each
-- into knowledge_nodes. Uses the trigger code path so the envelope
-- shape stays consistent with future inserts.
UPDATE entities SET updated_at = NOW();
UPDATE places   SET last_seen  = COALESCE(last_seen, NOW());
UPDATE topics   SET last_seen  = COALESCE(last_seen, NOW());

COMMIT;
