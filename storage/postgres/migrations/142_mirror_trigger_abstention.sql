-- 142: stop the mirror triggers from rewriting abstention into a tier.
--
-- Migration 141 collapsed twelve independent authors of completeness_score onto
-- one governed scorer and gave it the vocabulary to say "I do not know": when no
-- slot spec declares what a subtype should contain, core/gdu/completeness.py
-- returns None, and score_node() writes NULL score, NULL tier, stamped
-- 'unscoreable:no_spec'. NULL with a reason is the only reading that is true.
--
-- That consolidation was defeated one layer down, in SQL, where the Python-level
-- audit could not see it. Two triggers are live on this database:
--
--   trg_mirror_entity  AFTER INSERT OR UPDATE ON entities  (migration 015 / 032)
--   trg_mirror_topic   AFTER INSERT OR UPDATE ON topics
--
-- and both write a completeness sub-object into the mirrored knowledge_node
-- envelope. The entity one read:
--
--   'score', COALESCE(NEW.completeness_score, 0)::float,
--   'tier',  COALESCE(NEW.completeness_tier, 'bronze')
--
-- score_node() ends with `UPDATE entities SET completeness_score = %s,
-- completeness_tier = %s, ...`, which FIRES THIS TRIGGER on every scoring write.
-- So each time the governed scorer abstained, the COALESCE immediately converted
-- that abstention into score 0 and tier 'bronze' in the envelope. The scorer said
-- "undefined"; the trigger said "empty, bronze".
--
-- This is the project's first principle inverted at the storage layer: a gap is
-- not a negative. An absent score is not a zero, and an absent tier is not the
-- bottom of the ladder. 'bronze' is also a value NO pack declares — the governed
-- ladder in completenessvocab.tiers@0.1.0 is exactly stub / partial / solid /
-- verified — so the trigger was not defaulting to a governed floor, it was
-- minting a tier out of nothing.
--
-- The topic trigger was worse: it never read the row at all.
--
--   'completeness', jsonb_build_object('tier', 'bronze', 'score', 0.5)
--
-- Every topic node, unconditionally, regardless of what the row held. 0.5 is not
-- a default, it is a fabricated measurement — half-complete asserted about a
-- thing nothing had measured. `topics` has carried completeness_score and
-- completeness_tier columns all along; the trigger simply ignored them.
--
-- THE FIX is to propagate the row's own values and let NULL stay NULL. A JSON
-- null in the envelope reads as "not measured", which is what it is, and is
-- distinguishable from both a real value and an absent key. No consumer breaks:
-- the one reader of this sub-object, core/extraction/signal_extraction_store.py:196,
-- already coerces a missing tier to None.
--
-- NO BACKFILL here either. This migration changes what happens from now on; it
-- does not touch the 47,540 event rows and other legacy values already carrying
-- 'bronze'/'empty'. Retiring those invalid tier VALUES is a separate, deliberate
-- step (B2) — and the treatment there is NULL plus a 'legacy:invalid_tier' stamp,
-- never reassignment to a governed tier, because reassigning would invent exactly
-- the judgement the no-backfill rule exists to prevent. An unattributable score is
-- honest history. An undeclared tier is a value the vocabulary says cannot exist.
-- Those are different objects and get different treatment.
--
-- A THIRD function, mirror_place_to_knowledge_node(), carries the same fabricated
-- literal ('tier','bronze','score',0.5). It is deliberately NOT touched here: it is
-- an orphan. Its trigger was trg_mirror_place ON places (migration 015), and the
-- places table no longer exists on either stack — places became entities in
-- migration 118. Verified 2026-08-05: to_regclass('public.places') is NULL on both
-- the laptop and node-A, and zero triggers reference the function. It cannot fire.
-- Left in place rather than dropped so this migration stays a behaviour fix with no
-- schema demolition in it; if places is ever reintroduced, the function must be
-- fixed to read the row before any trigger is attached to it.

BEGIN;

-- ── entities → knowledge_nodes ──────────────────────────────────────────────
-- Body preserved verbatim from the live definition except the completeness
-- sub-object. Recreated in full rather than patched so the function's text is
-- self-describing at its current version.
CREATE OR REPLACE FUNCTION public.mirror_entity_to_knowledge_node()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
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
        -- CHANGED (142): was COALESCE(..., 0) / COALESCE(..., 'bronze').
        -- NULL now propagates as JSON null — the scorer's abstention survives
        -- instead of being rewritten as a measured zero at an invented tier.
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
$function$;

-- ── topics → knowledge_nodes ────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.mirror_topic_to_knowledge_node()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
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
        -- CHANGED (142): was the literal ('tier','bronze','score',0.5) for every
        -- topic ever mirrored, ignoring the row entirely. 0.5 was not a default,
        -- it was a fabricated measurement. Read the columns that already exist.
        'completeness', jsonb_build_object(
            'tier',  NEW.completeness_tier,
            'score', NEW.completeness_score::float
        )
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
$function$;

COMMIT;
