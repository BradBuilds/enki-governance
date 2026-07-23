-- 064: Type-pair enforcement + edge discovery/validation substrate
--
-- Until now, edges (entity_relationships / knowledge_edges) were written
-- without validating the declared edgevocab type contract. The result is
-- garbage like "Jane Doe capital_of [Acme note]" — every layer
-- of the system is type-aware, but no gate checked at write time.
--
-- This migration installs the enforcement substrate. It does NOT block
-- existing edges (that would break the running system). Instead it
-- creates the rails so new edges can be validated, existing edges can
-- be audited, and the cyclical model gains an empirical posterior over
-- which (src_type, edge_type, dst_type) combos actually appear in
-- real evidence.
--
-- Three pieces:
--   A. gdu_type_constraints — the canonical edge-schema spine,
--      loaded from edgevocab. Single row per edge_type.
--   B. edge_proposals — LLM-extracted edges awaiting validation. The
--      validator promotes to entity_relationships or quarantines to
--      issue.type_conflict.
--   C. edge_type_observations — every observed (src_type, edge_type,
--      dst_type) triple with counts. This IS the empirical schema —
--      over time it surfaces NEW candidate edge types when the corpus
--      consistently proposes them.

-- ── A. Canonical type constraints (loaded from edgevocab) ─────────────

CREATE TABLE IF NOT EXISTS gdu_type_constraints (
    constraint_id        BIGSERIAL PRIMARY KEY,
    edge_type            TEXT NOT NULL UNIQUE,
    label                TEXT,
    description          TEXT,
    source_types         TEXT[] NOT NULL,
    target_types         TEXT[] NOT NULL,
    sub_roles            TEXT[],
    is_symmetric         BOOLEAN NOT NULL DEFAULT FALSE,
    fires_automatically  BOOLEAN NOT NULL DEFAULT TRUE,
    inverse_edge_type    TEXT,
    pack_source          TEXT,
    pack_version         TEXT,
    base60_address       TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gdu_type_constraints_addr
    ON gdu_type_constraints (base60_address);


-- canonical_type_category(): resolves a subtype name to its category.
-- edgevocab declares source_types/target_types at CATEGORY level
-- ("entity","place","event") but entities.canonical_type holds the
-- SUBTYPE ("person","organization","city"). The validator below uses
-- this to accept an endpoint whose subtype name OR category name
-- matches the contract.
CREATE OR REPLACE FUNCTION canonical_type_category(p_type TEXT) RETURNS TEXT AS $$
DECLARE
    cat TEXT;
BEGIN
    IF p_type IS NULL THEN RETURN NULL; END IF;
    SELECT category INTO cat
      FROM gdu_address_sequences
     WHERE subtype = p_type
     LIMIT 1;
    RETURN cat;
END;
$$ LANGUAGE plpgsql STABLE;

-- Validation function. Returns NULL when the edge is valid; otherwise
-- returns a short rejection reason. Used both by triggers and by the
-- batch auditor.
CREATE OR REPLACE FUNCTION validate_edge_type_pair(
    p_src_type TEXT,
    p_edge_type TEXT,
    p_dst_type TEXT
) RETURNS TEXT AS $$
DECLARE
    c gdu_type_constraints;
    src_cat TEXT := canonical_type_category(p_src_type);
    dst_cat TEXT := canonical_type_category(p_dst_type);
BEGIN
    SELECT * INTO c FROM gdu_type_constraints WHERE edge_type = p_edge_type;
    IF NOT FOUND THEN
        RETURN 'edge_type_unknown:' || COALESCE(p_edge_type, '<null>');
    END IF;
    IF p_src_type IS NULL OR NOT (
         p_src_type = ANY(c.source_types) OR
         src_cat    = ANY(c.source_types)
       ) THEN
        RETURN 'src_type_invalid:' || COALESCE(p_src_type, '<null>')
               || ' not in [' || array_to_string(c.source_types, ',') || ']';
    END IF;
    IF p_dst_type IS NULL OR NOT (
         p_dst_type = ANY(c.target_types) OR
         dst_cat    = ANY(c.target_types)
       ) THEN
        RETURN 'dst_type_invalid:' || COALESCE(p_dst_type, '<null>')
               || ' not in [' || array_to_string(c.target_types, ',') || ']';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql STABLE;


-- Concept-instance match validator. Every instance entity must point
-- UP to a concept of the SAME canonical_type. Returns NULL when valid,
-- otherwise a rejection reason. Used by audit + future enforcement.
CREATE OR REPLACE FUNCTION validate_concept_instance_match(
    p_entity_id BIGINT
) RETURNS TEXT AS $$
DECLARE
    inst_type   TEXT;
    inst_kind   TEXT;
    concept_id  BIGINT;
    concept_type TEXT;
    concept_kind TEXT;
BEGIN
    SELECT canonical_type, entity_kind, concept_entity_id
      INTO inst_type, inst_kind, concept_id
      FROM entities WHERE entity_id = p_entity_id;
    IF NOT FOUND THEN
        RETURN 'entity_not_found:' || p_entity_id;
    END IF;
    IF inst_kind = 'concept' THEN
        IF concept_id IS NOT NULL THEN
            RETURN 'concept_has_parent_concept:not_a_top_level_concept';
        END IF;
        RETURN NULL;
    END IF;
    IF concept_id IS NULL THEN
        RETURN 'instance_unlinked:no_concept';
    END IF;
    SELECT canonical_type, entity_kind INTO concept_type, concept_kind
      FROM entities WHERE entity_id = concept_id;
    IF NOT FOUND THEN
        RETURN 'concept_not_found:' || concept_id;
    END IF;
    IF concept_kind <> 'concept' THEN
        RETURN 'parent_not_a_concept:kind=' || concept_kind;
    END IF;
    IF concept_type <> inst_type THEN
        RETURN 'type_mismatch:instance=' || inst_type
               || ',concept=' || concept_type;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql STABLE;


-- ── B. Edge proposals (the validation queue) ──────────────────────────

CREATE TABLE IF NOT EXISTS edge_proposals (
    proposal_id          BIGSERIAL PRIMARY KEY,
    proposed_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    tenant_id            TEXT NOT NULL DEFAULT 'default',
    source_entity_id     BIGINT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    target_entity_id     BIGINT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    src_canonical_type   TEXT,
    dst_canonical_type   TEXT,
    proposed_edge_type   TEXT NOT NULL,
    proposed_by          TEXT,
    confidence           REAL,
    evidence             JSONB,
    document_id          BIGINT,
    paragraph_id         BIGINT,
    surface_form         TEXT,
    status               TEXT NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending','accepted','rejected',
                                              'split','superseded','quarantined')),
    validation_result    TEXT,
    promoted_to_rid      BIGINT,
    quarantined_issue_id BIGINT,
    decided_at           TIMESTAMPTZ,
    decided_by           TEXT
);

CREATE INDEX IF NOT EXISTS idx_edge_proposals_pending
    ON edge_proposals(status) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_edge_proposals_src ON edge_proposals(source_entity_id);
CREATE INDEX IF NOT EXISTS idx_edge_proposals_dst ON edge_proposals(target_entity_id);
CREATE INDEX IF NOT EXISTS idx_edge_proposals_type ON edge_proposals(proposed_edge_type);


-- ── C. Empirical edge-type observations ───────────────────────────────
--
-- Every observed (src_type, edge_type, dst_type) triple gets one row.
-- Counts accumulate as proposals are seen and decided. This is the
-- empirical schema: the cyclical model's view of "which type-pairs
-- actually show up in real evidence vs which were merely declared in
-- edgevocab vs which appear consistently but aren't yet declared (new
-- edge type candidates)".

CREATE TABLE IF NOT EXISTS edge_type_observations (
    obs_id               BIGSERIAL PRIMARY KEY,
    src_canonical_type   TEXT NOT NULL,
    edge_type            TEXT NOT NULL,
    dst_canonical_type   TEXT NOT NULL,
    proposal_count       BIGINT NOT NULL DEFAULT 0,
    accepted_count       BIGINT NOT NULL DEFAULT 0,
    rejected_count       BIGINT NOT NULL DEFAULT 0,
    pending_count        BIGINT NOT NULL DEFAULT 0,
    first_seen           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    schema_status        TEXT NOT NULL DEFAULT 'observed'
                           CHECK (schema_status IN ('observed','candidate',
                                                     'accepted','rejected_consistent')),
    canonical_in_vocab   BOOLEAN NOT NULL DEFAULT FALSE,
    base60_address       TEXT,
    notes                TEXT,
    UNIQUE (src_canonical_type, edge_type, dst_canonical_type)
);

CREATE INDEX IF NOT EXISTS idx_edge_obs_status
    ON edge_type_observations(schema_status);
CREATE INDEX IF NOT EXISTS idx_edge_obs_not_vocab
    ON edge_type_observations(canonical_in_vocab, proposal_count DESC)
    WHERE NOT canonical_in_vocab;


-- recompute_edge_observations() — sweeps the proposal table + existing
-- edges and refreshes the observation counts. Cheap; idempotent.
CREATE OR REPLACE FUNCTION recompute_edge_observations() RETURNS INTEGER AS $$
DECLARE
    n INTEGER := 0;
BEGIN
    -- From proposals
    WITH agg AS (
        SELECT COALESCE(src_canonical_type,'<unknown>') AS s,
               proposed_edge_type AS e,
               COALESCE(dst_canonical_type,'<unknown>') AS d,
               COUNT(*) AS prop_n,
               COUNT(*) FILTER (WHERE status='accepted')    AS acc_n,
               COUNT(*) FILTER (WHERE status='rejected')    AS rej_n,
               COUNT(*) FILTER (WHERE status='pending')     AS pend_n,
               MIN(proposed_at) AS first_seen,
               MAX(proposed_at) AS last_seen
          FROM edge_proposals
         GROUP BY s, e, d
    )
    INSERT INTO edge_type_observations
        (src_canonical_type, edge_type, dst_canonical_type,
         proposal_count, accepted_count, rejected_count, pending_count,
         first_seen, last_seen)
    SELECT s, e, d, prop_n, acc_n, rej_n, pend_n, first_seen, last_seen FROM agg
    ON CONFLICT (src_canonical_type, edge_type, dst_canonical_type) DO UPDATE
       SET proposal_count = EXCLUDED.proposal_count,
           accepted_count = EXCLUDED.accepted_count,
           rejected_count = EXCLUDED.rejected_count,
           pending_count  = EXCLUDED.pending_count,
           last_seen      = GREATEST(edge_type_observations.last_seen,
                                     EXCLUDED.last_seen);

    -- Flag the canonical_in_vocab column based on gdu_type_constraints
    UPDATE edge_type_observations o
       SET canonical_in_vocab = (
             SELECT TRUE FROM gdu_type_constraints c
              WHERE c.edge_type = o.edge_type
                AND o.src_canonical_type = ANY(c.source_types)
                AND o.dst_canonical_type = ANY(c.target_types)
              LIMIT 1
           ) IS TRUE;

    -- Promote schema_status:
    --   ≥ 10 accepted, never rejected  → 'accepted'
    --   ≥ 3 proposals, not in vocab    → 'candidate'   (vocab extension proposal)
    --   ≥ 5 rejected, no accepts       → 'rejected_consistent'
    UPDATE edge_type_observations
       SET schema_status = CASE
             WHEN accepted_count >= 10 AND rejected_count = 0 THEN 'accepted'
             WHEN proposal_count >= 3 AND NOT canonical_in_vocab THEN 'candidate'
             WHEN rejected_count >= 5 AND accepted_count = 0 THEN 'rejected_consistent'
             ELSE 'observed'
           END;

    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END;
$$ LANGUAGE plpgsql;


-- ── Convenience view: "what should we change in edgevocab?" ──────────

CREATE OR REPLACE VIEW edge_vocab_proposals AS
SELECT src_canonical_type,
       edge_type,
       dst_canonical_type,
       proposal_count,
       accepted_count,
       rejected_count,
       schema_status,
       canonical_in_vocab,
       first_seen,
       last_seen
  FROM edge_type_observations
 WHERE schema_status IN ('candidate','rejected_consistent')
 ORDER BY proposal_count DESC;


-- Convenience view: "which existing entity_relationships rows would
-- the validator reject if we ran the gate today?"
CREATE OR REPLACE VIEW invalid_entity_relationships AS
SELECT er.rid,
       e1.canonical_name AS src_name, e1.canonical_type AS src_type,
       er.relationship,
       e2.canonical_name AS dst_name, e2.canonical_type AS dst_type,
       validate_edge_type_pair(e1.canonical_type, er.relationship, e2.canonical_type) AS reason
  FROM entity_relationships er
  JOIN entities e1 ON e1.entity_id = er.source_entity_id
  JOIN entities e2 ON e2.entity_id = er.target_entity_id
 WHERE validate_edge_type_pair(e1.canonical_type, er.relationship, e2.canonical_type) IS NOT NULL;
