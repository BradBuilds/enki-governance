-- 093_form_concept_substrate.sql
--
-- Cyclical base-60 substrate for the LDU rebuild.
--
-- Three layers:
--   FORM layer    — every surface string we've ever seen, addressable
--   CONCEPT layer — the canonical referents that forms bind to
--   INSTANCE layer — entities (already exists; promoted concepts get one)
--
-- Plus the structural governance:
--   subtype_divisors — which divisor of 60 governs each subtree
--                      (state='provisional' during learning,
--                       state='locked' after GDU ratification)
--   concept_address_sequences — next free slot per subtype_path,
--                                so admission knows when to cascade.
--
-- Addresses extend the existing 3-part `cat.sub.seq` format to variable
-- depth: `cat.sub.A.B.C...`. Each segment is 0-59. When a level fills,
-- admission consults subtype_divisors for the cascade rule.

BEGIN;

-- ── FORM LAYER ──────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS form_nodes (
  form_id                  BIGSERIAL PRIMARY KEY,
  tenant_id                VARCHAR(64) NOT NULL DEFAULT 'default',
  -- Variable-depth base-60 address. Form category is 11 per nodevocab.
  -- Example: "11.0.A7C" or "11.0.A7.0.5" if the subtree overflowed.
  form_address             VARCHAR(64) NOT NULL,
  -- The actual surface string as captured.
  surface_form             TEXT NOT NULL,
  -- Normalized form per entityvocab.normalize_rules.
  surface_form_normalized  TEXT NOT NULL,
  -- Deterministic 16-char hash of surface_form_normalized. Lets two
  -- nodes (or two processes) independently compute the same form_id
  -- lookup key for the same string.
  surface_form_hash        CHAR(16) NOT NULL,
  -- Optional script hint for future multilingual work: latin / cyrillic
  -- / arabic / hebrew / cjk / ... NULL until populated.
  script                   VARCHAR(32),
  signal_count             INT NOT NULL DEFAULT 1,
  source_signal_rids       BIGINT[] NOT NULL DEFAULT '{}',
  first_seen_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, surface_form_hash)
);

CREATE INDEX IF NOT EXISTS idx_form_nodes_norm
  ON form_nodes (tenant_id, surface_form_normalized);

CREATE INDEX IF NOT EXISTS idx_form_nodes_address
  ON form_nodes (form_address);


-- ── CONCEPT LAYER ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS concept_nodes (
  concept_id               BIGSERIAL PRIMARY KEY,
  tenant_id                VARCHAR(64) NOT NULL DEFAULT 'default',
  -- Variable-depth base-60 address. Concept category is 12.
  -- Example: "12.5.0F" (a person) or "12.A.0.5" (a country in a
  -- post-cascade Africa subtree).
  concept_address          VARCHAR(64) NOT NULL,
  -- The subtree path this concept lives in. Used as the join key for
  -- subtype_divisors and concept_address_sequences.
  -- Example: "entity.country" or "entity.country.africa".
  subtype_path             VARCHAR(128) NOT NULL,
  -- Display label for the concept. Comes from the dictionary hit that
  -- minted it, or from the LLM/cluster that synthesized it.
  canonical_name           TEXT NOT NULL,
  -- The leaf subtype name (e.g., "country"). Matches nodevocab.
  canonical_subtype        VARCHAR(64),
  -- Depth in the address tree. 1 = root subtype (cat.sub.X),
  -- 2 = first overflow level (cat.sub.X.Y), etc.
  depth                    SMALLINT NOT NULL DEFAULT 1,
  -- Once the concept promotes, this points at the entities row.
  promoted_to_entity_id    BIGINT REFERENCES entities(entity_id)
                             ON DELETE SET NULL,
  -- External identifiers accumulated on this concept (QID, CIK, etc.).
  identifiers              JSONB NOT NULL DEFAULT '{}'::jsonb,
  -- Per-source evidence stamps (canonicalpack_match, gazetteer_match,
  -- wikidata_id, sec_cik, llm_decision, ...).
  type_evidence            JSONB NOT NULL DEFAULT '{}'::jsonb,
  -- Highest tier this concept has reached (A..G per the evidence
  -- model). NULL until first evidence stamped.
  evidence_tier            CHAR(1) CHECK (evidence_tier IN
                             ('A','B','C','D','E','F','G')),
  signal_count             INT NOT NULL DEFAULT 0,
  source_signal_rids       BIGINT[] NOT NULL DEFAULT '{}',
  -- Logical merge pointer (mirrors the ldu_candidates merge model).
  -- Set when concept B was found to refer to the same referent as A.
  merged_to_concept_id     BIGINT REFERENCES concept_nodes(concept_id)
                             ON DELETE SET NULL,
  promotion_status         VARCHAR(32) NOT NULL DEFAULT 'pending'
                             CHECK (promotion_status IN
                               ('pending','promoted','merged','rejected')),
  -- Why this concept was merged or rejected. Free-form, includes the
  -- strategy that fired (canonicalpack, gazetteer, cluster, llm, ...).
  merge_reason             VARCHAR(128),
  pack_versions            JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, concept_address)
);

CREATE INDEX IF NOT EXISTS idx_concept_subtype_path
  ON concept_nodes (subtype_path, promotion_status);

CREATE INDEX IF NOT EXISTS idx_concept_canonical_name
  ON concept_nodes (tenant_id, canonical_name);

CREATE INDEX IF NOT EXISTS idx_concept_promoted_entity
  ON concept_nodes (promoted_to_entity_id)
  WHERE promoted_to_entity_id IS NOT NULL;

-- Identifier lookup: enables O(1) "is there a concept with this QID
-- already?" check during admission.
CREATE INDEX IF NOT EXISTS idx_concept_identifiers
  ON concept_nodes USING GIN (identifiers);


-- ── FORM ↔ CONCEPT BINDINGS ─────────────────────────────────────────────

-- A form can bind to multiple concepts (ambiguous form like "Tim Cook"
-- → Apple CEO + British academic). A concept can have many forms (Vale
-- + DJV + Dana J. Vale → one Vale concept). Many-to-many.

CREATE TABLE IF NOT EXISTS form_concept_bindings (
  binding_id               BIGSERIAL PRIMARY KEY,
  form_id                  BIGINT NOT NULL REFERENCES form_nodes(form_id)
                             ON DELETE CASCADE,
  concept_id               BIGINT NOT NULL REFERENCES concept_nodes(concept_id)
                             ON DELETE CASCADE,
  -- Confidence in this specific binding, 0..1. Reflects which strategy
  -- bound them (tier-A identifier match → 0.99, cluster → 0.75,
  -- LLM → 0.85, etc.).
  binding_confidence       REAL NOT NULL DEFAULT 0.5,
  -- Which strategy/source bound this form to this concept.
  binding_source           VARCHAR(32) NOT NULL,
  binding_evidence         JSONB,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- Bindings become inactive when split/un-merged. Keep the row for
  -- audit but stop using it for resolution.
  active                   BOOLEAN NOT NULL DEFAULT TRUE,
  UNIQUE (form_id, concept_id)
);

CREATE INDEX IF NOT EXISTS idx_fcb_form_active
  ON form_concept_bindings (form_id)
  WHERE active = TRUE;

CREATE INDEX IF NOT EXISTS idx_fcb_concept_active
  ON form_concept_bindings (concept_id)
  WHERE active = TRUE;


-- ── DIVISOR GOVERNANCE ──────────────────────────────────────────────────

-- One row per subtree that has a divisor election (provisional or
-- locked). Subtrees without a row have no overflow yet; on first
-- overflow, a row gets inserted by admission code (or by a governance
-- election if pre-declared in nodevocab).

CREATE TABLE IF NOT EXISTS subtype_divisors (
  subtype_path             VARCHAR(128) PRIMARY KEY,
  divisor                  SMALLINT NOT NULL
                             CHECK (divisor IN (2,3,4,5,6,10,12,15,20,30)),
  -- 'provisional' = governance can re-elect, cascades on change.
  -- 'locked'      = permanent, divisor_reelect proposals rejected.
  state                    VARCHAR(16) NOT NULL DEFAULT 'provisional'
                             CHECK (state IN ('provisional','locked')),
  -- Human labels for the resulting partitions, e.g.
  -- [{"index":0,"label":"africa"},{"index":1,"label":"asia"},...].
  partition_labels         JSONB NOT NULL DEFAULT '[]'::jsonb,
  reasoning                TEXT,
  -- Where this election came from.
  declared_in              VARCHAR(64) NOT NULL DEFAULT 'default_overflow'
                             CHECK (declared_in IN
                               ('nodevocab','default_overflow',
                                'governance_election')),
  elected_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- Filled when state flips to 'locked' via a divisor_ratify proposal.
  ratified_at              TIMESTAMPTZ,
  ratifying_proposal_id    BIGINT REFERENCES governance_proposals(proposal_id)
                             ON DELETE SET NULL,
  -- How many re-election cascades this subtree has been through.
  -- Each re-election emits a cascade event in gdu_admission_log.
  cascade_count            INT NOT NULL DEFAULT 0,
  last_cascade_at          TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_subtype_divisors_state
  ON subtype_divisors (state, elected_at);


-- ── ADDRESS SLOT SEQUENCES ──────────────────────────────────────────────

-- Tracks the next free slot per subtype_path. When a subtree's slot
-- count hits 60, admission consults subtype_divisors to decide how to
-- cascade. If no divisor row exists yet, admission inserts one with
-- the nodevocab-declared divisor (or D=6 default) and state='provisional'.

CREATE TABLE IF NOT EXISTS concept_address_sequences (
  subtype_path             VARCHAR(128) PRIMARY KEY,
  next_slot                SMALLINT NOT NULL DEFAULT 0
                             CHECK (next_slot >= 0 AND next_slot <= 60),
  -- True the moment next_slot would otherwise become 60. Drives the
  -- cascade decision on the very next admission.
  overflowed               BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ── governance proposal categories ───────────────────────────────────

-- Two new category values for the existing governance_proposals table:
--   'divisor_ratify'    — proposes flipping a provisional divisor to locked
--   'divisor_reelect'   — proposes changing a provisional divisor
--                         (triggers cascade on pass)
--
-- No schema change needed — governance_proposals.category is free-form
-- VARCHAR(64). Documented here for discoverability.

COMMIT;
