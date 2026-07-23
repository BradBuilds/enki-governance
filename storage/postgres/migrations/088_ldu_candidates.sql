-- 088_ldu_candidates.sql
--
-- LDU rebuild Phase 2 — the new capture-time substrate.
--
-- Before this migration, every extracted surface form went straight to
-- `entities` via core.gdu.gateway.admit_entity. That made every
-- side-effect (federation publish, investigation queue, address
-- assignment, entity_addresses join) fire on uncurated input. When the
-- entity later turned out to be a sentence fragment, the wrong type, or
-- a duplicate, those side-effects had already cascaded.
--
-- ldu_candidates is the holding zone. Extraction writes here. A worker
-- runs LDU collapse + promotion criteria. Only candidates that meet
-- promotion criteria cross into `entities` — and only at that point do
-- downstream side-effects fire.
--
-- Schema is append-only: this migration creates the table and its
-- indexes. promotion_status transitions are managed by core.ldu.

BEGIN;

CREATE TABLE IF NOT EXISTS ldu_candidates (
  candidate_id            BIGSERIAL PRIMARY KEY,
  tenant_id               VARCHAR(64) NOT NULL DEFAULT 'default',

  -- Surface + normalized forms
  surface_form            TEXT NOT NULL,
  normalized_name         TEXT NOT NULL,
  proposed_type           VARCHAR(64),
  proposed_subtype        VARCHAR(64),

  -- Provenance
  first_seen_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  signal_count            INT NOT NULL DEFAULT 1,
  source_signal_rids      BIGINT[] NOT NULL DEFAULT '{}',
  source_statement_ids    BIGINT[] NOT NULL DEFAULT '{}',

  -- LDU state machine
  --   pending   — accumulating signal, not yet ready to promote
  --   rejected  — short-circuited by reject_patterns / normalize rejection
  --   promoted  — crossed into GDU; promoted_to_entity_id points there
  --   merged    — collapsed into another candidate; merged_to_candidate_id
  --               points there
  promotion_status        VARCHAR(32) NOT NULL DEFAULT 'pending',
  rejection_reason        TEXT,
  promoted_to_entity_id   BIGINT REFERENCES entities(entity_id)
                            ON DELETE SET NULL,
  merged_to_candidate_id  BIGINT REFERENCES ldu_candidates(candidate_id)
                            ON DELETE SET NULL,

  -- Pack lineage stamps. JSON keyed by pack name -> version string.
  -- Captures which packs were active when the candidate first landed.
  admitted_under_pack_versions JSONB NOT NULL DEFAULT '{}'::jsonb,

  -- LDU collapse evidence
  aliases_observed        TEXT[] NOT NULL DEFAULT '{}',
  -- type_evidence holds whatever signal the caller gave us about type:
  --   {"wikidata_id": "Q11696", ...}
  --   {"gazetteer_match": "geonames:6252001"}
  --   {"sec_filing": "0001318605"}
  --   {"trigram_match": true}
  --   {"llm_decision": {"confidence": 0.92, "reasoning": "..."}}
  type_evidence           JSONB,

  created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Lookup by normalized_name within a tenant — the hot path for
-- admit_candidate's "is there already a pending candidate?" check.
CREATE INDEX IF NOT EXISTS idx_ldu_candidates_norm
  ON ldu_candidates (tenant_id, normalized_name);

-- Walk pending candidates by recency — the promoter worker scans this.
CREATE INDEX IF NOT EXISTS idx_ldu_candidates_status
  ON ldu_candidates (promotion_status, updated_at);

-- Type-bucketed scans (e.g., promote all pending persons) — partial
-- index keeps it small.
CREATE INDEX IF NOT EXISTS idx_ldu_candidates_type
  ON ldu_candidates (proposed_type)
  WHERE promotion_status = 'pending';

COMMIT;
