-- 065: `definition` category — the schema becomes addressable in the
--      base-60 cyclical model.
--
-- Until now, the SCHEMA of the system (edge types, type-pair contracts,
-- concept-instance rules, lemma↔concept dictionary edges) lived in
-- JSON pack files. They were configuration, not data. They had no
-- base-60 addresses, no provenance, no reversibility, no evidence
-- counts.
--
-- For the cyclical model to be self-validating, definitions need to be
-- first-class addressable nodes too. This migration adds a 13th
-- top-level category — `definition` — whose subtypes hold the schema
-- itself.
--
-- Subtypes (mint order = address order within category 12):
--   edge_type_def      — a defined edge type (one per edgevocab entry)
--   type_pair_def      — a canonical (src_type, edge_type, dst_type) triple
--                        promoted from edge_type_observations
--   concept_link_def   — the rule that links instances of a type to
--                        their concept parent
--   lemma_concept_def  — the dictionary edge: lemma → canonical concept
--   constraint_def     — any other governance rule (validation, etc.)
--
-- Each definition row carries: pack source + version, evidence count,
-- first/last seen, base60_address, reversible flag. The cyclical model
-- now has a complete arrangement: form → lemma → concept → instance →
-- edge → definition. Every layer is addressable, every fact is
-- traceable.

INSERT INTO gdu_address_sequences (category, subtype, next_seq)
VALUES
    ('definition', 'edge_type_def',     1),
    ('definition', 'type_pair_def',     1),
    ('definition', 'concept_link_def',  1),
    ('definition', 'lemma_concept_def', 1),
    ('definition', 'constraint_def',    1)
ON CONFLICT (category, subtype) DO NOTHING;


CREATE TABLE IF NOT EXISTS definitions (
    definition_id         BIGSERIAL PRIMARY KEY,
    definition_kind       TEXT NOT NULL CHECK (definition_kind IN (
                            'edge_type_def','type_pair_def',
                            'concept_link_def','lemma_concept_def',
                            'constraint_def')),
    base60_address        TEXT NOT NULL UNIQUE,
    canonical_name        TEXT NOT NULL,
    description           TEXT,
    pack_source           TEXT,
    pack_version          TEXT,
    payload               JSONB NOT NULL,
    evidence_count        BIGINT NOT NULL DEFAULT 0,
    first_seen            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status                TEXT NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active','deprecated','candidate','rejected')),
    governance_tier       TEXT,
    reversible            BOOLEAN NOT NULL DEFAULT TRUE,
    reverted_at           TIMESTAMPTZ,
    reverted_by           TEXT,
    notes                 TEXT
);

CREATE INDEX IF NOT EXISTS idx_definitions_kind ON definitions(definition_kind);
CREATE INDEX IF NOT EXISTS idx_definitions_status ON definitions(status);
CREATE INDEX IF NOT EXISTS idx_definitions_pack ON definitions(pack_source, pack_version);
CREATE INDEX IF NOT EXISTS idx_definitions_addr ON definitions(base60_address);
CREATE UNIQUE INDEX IF NOT EXISTS uq_definitions_kind_name
    ON definitions(definition_kind, canonical_name);


-- The address link from gdu_type_constraints back to definitions —
-- a constraint row's definition is the source of truth.
ALTER TABLE gdu_type_constraints
    ADD COLUMN IF NOT EXISTS definition_id BIGINT REFERENCES definitions(definition_id);


-- ──────────────────────────────────────────────────────────────────────
-- Definition discovery view: pending candidates that could be promoted
-- to first-class definitions from corpus evidence.
-- ──────────────────────────────────────────────────────────────────────

CREATE OR REPLACE VIEW definition_candidates AS
-- Candidate edge_type_def: edge types appearing in observations that
-- are NOT yet declared in edgevocab
SELECT 'edge_type_def' AS proposed_kind,
       edge_type AS proposed_name,
       proposal_count AS evidence,
       NULL::REAL AS confidence,
       jsonb_build_object(
         'src_types', array_agg(DISTINCT src_canonical_type),
         'dst_types', array_agg(DISTINCT dst_canonical_type),
         'first_seen', MIN(first_seen),
         'last_seen', MAX(last_seen)
       ) AS payload
  FROM edge_type_observations
 WHERE NOT canonical_in_vocab
   AND schema_status = 'candidate'
 GROUP BY edge_type, proposal_count
UNION ALL
-- Candidate type_pair_def: (src_type, edge_type, dst_type) triples
-- consistently observed but never declared
SELECT 'type_pair_def' AS proposed_kind,
       src_canonical_type || '|' || edge_type || '|' || dst_canonical_type AS proposed_name,
       proposal_count AS evidence,
       NULL::REAL AS confidence,
       jsonb_build_object(
         'src_type', src_canonical_type,
         'edge_type', edge_type,
         'dst_type', dst_canonical_type,
         'accepted', accepted_count,
         'rejected', rejected_count
       ) AS payload
  FROM edge_type_observations
 WHERE schema_status = 'accepted'
ORDER BY evidence DESC;
