-- Migration 146 — stop minting a base-60 address on every UPDATE
--
-- `trg_mirror_entity` and `trg_mirror_topic` fire on INSERT **OR UPDATE**, and
-- both mirror functions call `gdu_assign_address()` as their FIRST statement,
-- unconditionally. The INSERT that follows then does
-- `ON CONFLICT ... base60_address = COALESCE(knowledge_nodes.base60_address,
-- EXCLUDED.base60_address)` — so on any update the freshly minted address is
-- immediately discarded.
--
-- Entities are updated constantly: every mention bumps `signal_count` and
-- `last_seen`. So the sequence advances on every observation of every entity,
-- and almost none of those numbers is ever attached to anything.
--
-- MEASURED ON node-A, 2026-08-09:
--
--   entity.person                533,081 minted   32,820 nodes   500,261 burned
--   entity.organization          246,709 minted   14,535 nodes   232,174 burned
--   document.bibliographic_ref    55,116 minted        0 nodes    55,116 burned
--   entity.facility               30,340 minted    3,223 nodes    27,117 burned
--
-- Roughly 94% of every address ever minted for a person was thrown away.
--
-- WHY THIS IS WORTH FIXING, AND WHAT IT IS NOT
--
-- It is NOT running out of space: the sequence is an integer and nothing is
-- close to exhausting it. Nor does it corrupt anything — the discarded numbers
-- were never written to a row, and no stored address changes when this lands.
--
-- What it costs is MEANING. The plan's principle is that "addresses are
-- coordinates, not claims", and a coordinate whose sequence advances on every
-- observation is not a coordinate of anything — the 500,000th person address
-- does not mean the 500,000th person, it means the 500,000th time any person row
-- was touched. It also puts a write into `gdu_address_sequences` on the hot path
-- of every mention, which serialises ingestion behind a counter nobody reads.
--
-- THE FIX: mint only when the node does not already have an address. Strictly
-- conservative — it mints a subset of what it minted before, and the addresses
-- it no longer mints are exactly the ones that were being discarded. No stored
-- value changes.
--
-- NOT BACKFILLED. The 500,261 burned numbers stay burned; the sequence is not
-- reset. Rewinding it would hand out addresses that this system has already
-- issued once, and a coordinate that has meant two things is worse than a gap in
-- the numbering. The gap is honest history — it records that we used to do this.

BEGIN;

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
    -- Reuse the address this node already holds. Only mint when there is none.
    -- The ON CONFLICT below keeps the incumbent anyway, so every address minted
    -- for an existing node was being discarded.
    SELECT base60_address INTO v_address
      FROM knowledge_nodes WHERE node_id = v_node_id;

    IF v_address IS NULL THEN
        v_address := gdu_assign_address('entity', NEW.canonical_type);
    END IF;

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
        -- Migration 142 removed the COALESCE defaults here: NULL must propagate
        -- as NULL so the governed scorer's abstention survives the mirror.
        'completeness', jsonb_build_object(
            'score', NEW.completeness_score::float,
            'tier',  NEW.completeness_tier
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
    SELECT base60_address INTO v_address
      FROM knowledge_nodes WHERE node_id = v_node_id;

    IF v_address IS NULL THEN
        v_address := gdu_assign_address('topic', NEW.canonical_type);
    END IF;

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
        'evidence',     jsonb_build_object('source_count', NEW.mention_count,
                                           'epistemic_type', 'observation'),
        'completeness', jsonb_build_object('tier', NEW.completeness_tier,
                                           'score', NEW.completeness_score::float)
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

COMMIT;
