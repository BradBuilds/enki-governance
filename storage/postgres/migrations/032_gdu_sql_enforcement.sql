-- Migration 032 — SQL-level GDU enforcement
--
-- Closes the structural leak where mirror_*_to_knowledge_node triggers
-- (from migration 015) created entity:N / place:N / topic:N rows with
-- gdu_canonical=TRUE but no base60_address — bypassing the gateway.
--
-- Three pieces:
--
--   1. gdu_pack_positions table — SQL-side mirror of (category, subtype,
--      cat_idx, sub_idx) from the active GDU pack registry. Populated by
--      core/gdu/sql_pack_sync.py at pack admission time.
--
--   2. gdu_assign_address(category, subtype) PL/pgSQL function — replicates
--      core.gdu.address.assign_address() so triggers can address rows
--      inline. Returns NULL if (category, subtype) is not in the active
--      pack — caller routes to LDU-P.
--
--   3. mirror_*_to_knowledge_node() functions REPLACED with versions that
--      call gdu_assign_address and store the result. Rows that don't map
--      get gdu_canonical=FALSE (provisional layer).
--
--   4. enforce_governance() BEFORE INSERT trigger on knowledge_nodes
--      that REJECTS any insert with gdu_canonical=TRUE AND
--      base60_address IS NULL. This is the invariant guard: it's
--      impossible to write an "ungoverned canonical" row from anywhere
--      after this migration applies.
--
-- All idempotent. CREATE OR REPLACE for functions, IF NOT EXISTS for
-- tables and triggers.

BEGIN;

-- ── 1. gdu_pack_positions ────────────────────────────────────────────────
-- SQL-side mirror of the pack registry indices. Python populates this
-- via core/gdu/sql_pack_sync.py whenever the pack changes (pack admission
-- ceremony writes a fresh row set after computing indices from the JSON).
CREATE TABLE IF NOT EXISTS gdu_pack_positions (
    category        VARCHAR(64)  NOT NULL,
    subtype         VARCHAR(128) NOT NULL,
    cat_idx         SMALLINT     NOT NULL CHECK (cat_idx >= 0 AND cat_idx < 60),
    sub_idx         SMALLINT     NOT NULL CHECK (sub_idx >= 0 AND sub_idx < 60),
    pack_version    VARCHAR(32)  NOT NULL,
    synced_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (category, subtype)
);

CREATE INDEX IF NOT EXISTS idx_gdu_pack_positions_indices
    ON gdu_pack_positions (cat_idx, sub_idx);


-- ── 2. gdu_assign_address SQL function ───────────────────────────────────
-- Mirrors core/gdu/address.py:assign_address. Atomically increments the
-- sequence counter for (category, subtype) and returns "cat.sub.seq".
-- Returns NULL if the (category, subtype) is not in the active pack —
-- caller is responsible for routing to LDU-P.
CREATE OR REPLACE FUNCTION gdu_assign_address(p_category TEXT, p_subtype TEXT)
RETURNS TEXT AS $$
DECLARE
    v_cat_idx SMALLINT;
    v_sub_idx SMALLINT;
    v_seq     INTEGER;
BEGIN
    IF p_category IS NULL OR p_subtype IS NULL THEN
        RETURN NULL;
    END IF;

    SELECT cat_idx, sub_idx INTO v_cat_idx, v_sub_idx
    FROM gdu_pack_positions
    WHERE category = p_category AND subtype = p_subtype;

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    -- Atomic increment + return; create the sequence row if it doesn't exist.
    INSERT INTO gdu_address_sequences (category, subtype, next_seq)
    VALUES (p_category, p_subtype, 2)
    ON CONFLICT (category, subtype) DO UPDATE
    SET next_seq = gdu_address_sequences.next_seq + 1,
        updated_at = NOW()
    RETURNING next_seq - 1 INTO v_seq;

    RETURN v_cat_idx::text || '.' || v_sub_idx::text || '.' || v_seq::text;
END;
$$ LANGUAGE plpgsql;


-- ── 3a. Mirror entity → knowledge_node (REPLACED with addressing) ────────
CREATE OR REPLACE FUNCTION mirror_entity_to_knowledge_node()
RETURNS TRIGGER AS $$
DECLARE
    v_node_id  text := 'entity:' || NEW.entity_id::text;
    v_envelope jsonb;
    v_address  text;
    v_cat_idx  SMALLINT;
    v_sub_idx  SMALLINT;
    v_canonical boolean;
BEGIN
    v_address := gdu_assign_address('entity', NEW.canonical_type);

    IF v_address IS NOT NULL THEN
        SELECT cat_idx, sub_idx INTO v_cat_idx, v_sub_idx
        FROM gdu_pack_positions
        WHERE category = 'entity' AND subtype = NEW.canonical_type;
        v_canonical := TRUE;
    ELSE
        v_cat_idx := NULL;
        v_sub_idx := NULL;
        v_canonical := FALSE;  -- not in active pack → provisional
    END IF;

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
        base60_address, base60_category, base60_subtype,
        admitted_under_active_set, admitted_under_pack_versions,
        gdu_canonical, ldu_t_id
    ) VALUES (
        v_node_id, 'default', NEW.tenant_id,
        NEW.canonical_name, '', 'observation',
        v_envelope, 'v1', 'entity', NEW.canonical_type,
        NEW.entity_id,
        v_address, v_cat_idx, v_sub_idx,
        NEW.admitted_under_active_set, COALESCE(NEW.admitted_under_pack_versions, '{}'::jsonb),
        v_canonical, NEW.ldu_t_id
    )
    ON CONFLICT (node_id) DO UPDATE SET
        title              = EXCLUDED.title,
        envelope           = EXCLUDED.envelope,
        node_type          = EXCLUDED.node_type,
        subtype            = EXCLUDED.subtype,
        master_entity_id   = EXCLUDED.master_entity_id,
        base60_address     = COALESCE(knowledge_nodes.base60_address, EXCLUDED.base60_address),
        base60_category    = COALESCE(knowledge_nodes.base60_category, EXCLUDED.base60_category),
        base60_subtype     = COALESCE(knowledge_nodes.base60_subtype, EXCLUDED.base60_subtype),
        gdu_canonical      = (knowledge_nodes.gdu_canonical OR EXCLUDED.gdu_canonical),
        updated_at         = NOW();

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- ── 3b. Mirror place → knowledge_node ────────────────────────────────────
CREATE OR REPLACE FUNCTION mirror_place_to_knowledge_node()
RETURNS TRIGGER AS $$
DECLARE
    v_node_id  text := 'place:' || NEW.place_id::text;
    v_envelope jsonb;
    v_lat double precision;
    v_lon double precision;
    v_address  text;
    v_cat_idx  SMALLINT;
    v_sub_idx  SMALLINT;
    v_canonical boolean;
BEGIN
    IF NEW.coordinates IS NOT NULL THEN
        v_lat := ST_Y(NEW.coordinates::geometry);
        v_lon := ST_X(NEW.coordinates::geometry);
    END IF;

    v_address := gdu_assign_address('place', NEW.canonical_type);
    IF v_address IS NOT NULL THEN
        SELECT cat_idx, sub_idx INTO v_cat_idx, v_sub_idx
        FROM gdu_pack_positions
        WHERE category = 'place' AND subtype = NEW.canonical_type;
        v_canonical := TRUE;
    ELSE
        v_cat_idx := NULL;
        v_sub_idx := NULL;
        v_canonical := FALSE;
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
        base60_address, base60_category, base60_subtype,
        admitted_under_active_set, gdu_canonical, ldu_t_id
    ) VALUES (
        v_node_id, 'default', NEW.tenant_id,
        NEW.canonical_name, '', 'observation',
        v_envelope, 'v1', 'place', NEW.canonical_type,
        NEW.place_id, v_lat, v_lon,
        v_address, v_cat_idx, v_sub_idx,
        NEW.admitted_under_active_set, v_canonical, NEW.ldu_t_id
    )
    ON CONFLICT (node_id) DO UPDATE SET
        title           = EXCLUDED.title,
        envelope        = EXCLUDED.envelope,
        latitude        = EXCLUDED.latitude,
        longitude       = EXCLUDED.longitude,
        master_place_id = EXCLUDED.master_place_id,
        base60_address  = COALESCE(knowledge_nodes.base60_address, EXCLUDED.base60_address),
        base60_category = COALESCE(knowledge_nodes.base60_category, EXCLUDED.base60_category),
        base60_subtype  = COALESCE(knowledge_nodes.base60_subtype, EXCLUDED.base60_subtype),
        gdu_canonical   = (knowledge_nodes.gdu_canonical OR EXCLUDED.gdu_canonical),
        updated_at      = NOW();

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- ── 3c. Mirror topic → knowledge_node ────────────────────────────────────
CREATE OR REPLACE FUNCTION mirror_topic_to_knowledge_node()
RETURNS TRIGGER AS $$
DECLARE
    v_node_id  text := 'topic:' || NEW.topic_id::text;
    v_envelope jsonb;
    v_address  text;
    v_cat_idx  SMALLINT;
    v_sub_idx  SMALLINT;
    v_canonical boolean;
BEGIN
    v_address := gdu_assign_address('topic', NEW.canonical_type);
    IF v_address IS NOT NULL THEN
        SELECT cat_idx, sub_idx INTO v_cat_idx, v_sub_idx
        FROM gdu_pack_positions
        WHERE category = 'topic' AND subtype = NEW.canonical_type;
        v_canonical := TRUE;
    ELSE
        v_cat_idx := NULL;
        v_sub_idx := NULL;
        v_canonical := FALSE;
    END IF;

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
        base60_address, base60_category, base60_subtype,
        admitted_under_active_set, gdu_canonical, ldu_t_id
    ) VALUES (
        v_node_id, 'default', NEW.tenant_id,
        NEW.canonical_name, '', 'observation',
        v_envelope, 'v1', 'topic', NEW.canonical_type,
        NEW.topic_id,
        v_address, v_cat_idx, v_sub_idx,
        NEW.admitted_under_active_set, v_canonical, NEW.ldu_t_id
    )
    ON CONFLICT (node_id) DO UPDATE SET
        title            = EXCLUDED.title,
        envelope         = EXCLUDED.envelope,
        master_topic_id  = EXCLUDED.master_topic_id,
        base60_address   = COALESCE(knowledge_nodes.base60_address, EXCLUDED.base60_address),
        base60_category  = COALESCE(knowledge_nodes.base60_category, EXCLUDED.base60_category),
        base60_subtype   = COALESCE(knowledge_nodes.base60_subtype, EXCLUDED.base60_subtype),
        gdu_canonical    = (knowledge_nodes.gdu_canonical OR EXCLUDED.gdu_canonical),
        updated_at       = NOW();

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- ── 4. enforce_governance — BEFORE INSERT/UPDATE trigger on knowledge_nodes
-- Refuses any row that claims gdu_canonical=TRUE without a base60_address.
-- This is the invariant guard. After this migration applies, it's
-- impossible to write an ungoverned canonical row from anywhere — Python,
-- SQL, manual psql, all blocked equally.
CREATE OR REPLACE FUNCTION enforce_governance()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.gdu_canonical IS TRUE AND NEW.base60_address IS NULL THEN
        RAISE EXCEPTION
            'Refusing ungoverned canonical row: node_id=%, type=%.%. '
            'Either route through gateway.admit_node() to assign an address, '
            'or set gdu_canonical=FALSE for LDU-P.',
            NEW.node_id, NEW.node_type, NEW.subtype;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_enforce_governance ON knowledge_nodes;
CREATE TRIGGER trg_enforce_governance
    BEFORE INSERT OR UPDATE ON knowledge_nodes
    FOR EACH ROW EXECUTE FUNCTION enforce_governance();

COMMIT;
