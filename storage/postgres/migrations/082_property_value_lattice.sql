-- 082_property_value_lattice.sql
--
-- The property/value lattice substrate.
--
-- Implements the algebra from propertyvocab + valuevocab + precisionvocab:
--   * `properties` table — one row per property concept (color, time, …)
--   * `values` table — value nodes, each living in one property's lattice
--                       at a specific depth (precision tier from precisionvocab)
--   * `value_relations` table — refines / alternative edges between values
--   * extend `assessments` with property_id, value_id, precision_tier columns
--     so the collapse operator has the typed fields it needs
--
-- Safe to apply to a live system: all changes are ADDITIVE. Existing
-- assessment rows keep working; new property_id / value_id are NULL on
-- legacy rows and get filled by a background re-tagging worker.
--
-- Once the seed pack rows are admitted on first boot (via a separate
-- bootstrap step), the four new packs (precisionvocab, propertyvocab,
-- valuevocab, topicvocab) become load-bearing.

BEGIN;

-- ─── Properties ────────────────────────────────────────────────────────────
-- One row per property CONCEPT (e.g. "color", "manufacturer", "time").
-- Seeded from propertyvocab.properties.json by the pack-bootstrap pass.

CREATE TABLE IF NOT EXISTS properties (
  property_id        BIGSERIAL PRIMARY KEY,
  tenant_id          VARCHAR(64) NOT NULL DEFAULT 'default',
  property_key       TEXT NOT NULL,            -- e.g. 'color', 'manufacturer'
  cycle              INTEGER NOT NULL,         -- base60 cycle assignment
  label              TEXT NOT NULL,
  description        TEXT,
  applicable_to      TEXT[] NOT NULL DEFAULT '{}',
  precision_tiers    TEXT[] NOT NULL DEFAULT '{}',
  wikidata_pid       VARCHAR(32),
  units              VARCHAR(64),
  deprecated         BOOLEAN NOT NULL DEFAULT FALSE,
  replaced_by        BIGINT REFERENCES properties(property_id),
  -- GDU governance
  admitted_under_pack_version TEXT NOT NULL DEFAULT 'propertyvocab@0.1.0',
  base60_address     VARCHAR(16),
  node_id            VARCHAR(64),
  gdu_canonical      BOOLEAN NOT NULL DEFAULT TRUE,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (tenant_id, property_key),
  UNIQUE (tenant_id, cycle)
);

CREATE INDEX IF NOT EXISTS idx_properties_key ON properties (property_key);
CREATE INDEX IF NOT EXISTS idx_properties_cycle ON properties (cycle);

COMMENT ON TABLE properties IS
  'Property concepts. Each row corresponds to one property in propertyvocab. '
  'cycle is the base60 cycle this property OWNS — all values for this '
  'property carry this cycle in their base60 address.';

-- ─── Values ───────────────────────────────────────────────────────────────
-- One row per value node. Lives in one property's lattice, at one depth.
-- The substrate admits new values at runtime via the gateway as data flows in.

CREATE TABLE IF NOT EXISTS property_values (
  value_id           BIGSERIAL PRIMARY KEY,
  tenant_id          VARCHAR(64) NOT NULL DEFAULT 'default',
  property_id        BIGINT NOT NULL REFERENCES properties(property_id) ON DELETE CASCADE,
  parent_value_id    BIGINT REFERENCES property_values(value_id),
  depth              INTEGER NOT NULL,                     -- precision tier depth
  precision_tier     TEXT NOT NULL,                        -- 'named' | 'qualified' | …
  label              TEXT NOT NULL,                        -- canonical name (e.g. 'red')
  normalized_label   TEXT NOT NULL,                        -- lowercased, trimmed
  aliases            TEXT[] NOT NULL DEFAULT '{}',         -- alternative spellings
  formal_id          TEXT,                                 -- registry id when formal (Pantone 199C, ISO en, etc.)
  numeric_value      NUMERIC,                              -- for quantitative props
  units              VARCHAR(64),
  -- GDU governance
  base60_address     VARCHAR(32),
  node_id            VARCHAR(64),
  admitted_under_pack_version TEXT NOT NULL DEFAULT 'valuevocab@0.1.0',
  gdu_canonical      BOOLEAN NOT NULL DEFAULT TRUE,
  -- Audit
  mention_count      INTEGER NOT NULL DEFAULT 0,
  first_seen         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_seen          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (tenant_id, property_id, normalized_label, depth)
);

CREATE INDEX IF NOT EXISTS idx_values_property ON property_values (property_id, depth);
CREATE INDEX IF NOT EXISTS idx_values_parent ON property_values (parent_value_id);
CREATE INDEX IF NOT EXISTS idx_values_normalized ON property_values (normalized_label);

COMMENT ON TABLE property_values IS
  'Value nodes in property lattices. parent_value_id refines edge (NULL = root). '
  'depth comes from precisionvocab.tiers. Same (property, depth, parent) but '
  'different normalized_label = alternatives. The same (property, normalized_label, depth) '
  'is a unique value — case-insensitive collapse happens at write-time.';

-- ─── Value relations (beyond simple refines) ──────────────────────────────
-- Refinement edges live as parent_value_id on the value row itself. This
-- table holds OTHER relations between values: contradicted_by, same_as,
-- alias_of (cross-property), unit-conversion, etc.

CREATE TABLE IF NOT EXISTS value_relations (
  relation_id        BIGSERIAL PRIMARY KEY,
  source_value_id    BIGINT NOT NULL REFERENCES property_values(value_id) ON DELETE CASCADE,
  target_value_id    BIGINT NOT NULL REFERENCES property_values(value_id) ON DELETE CASCADE,
  relation_type      VARCHAR(32) NOT NULL,                 -- 'contradicts', 'same_as', 'alias_of', 'converts_to'
  weight             REAL NOT NULL DEFAULT 1.0,
  via                VARCHAR(32) NOT NULL DEFAULT 'manual',
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (source_value_id, target_value_id, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_vrel_source ON value_relations (source_value_id, relation_type);
CREATE INDEX IF NOT EXISTS idx_vrel_target ON value_relations (target_value_id, relation_type);

-- ─── Assessments — add typed fields for the new algebra ────────────────────
-- Existing rows keep working: NULL property_id means 'legacy untyped assertion'.
-- New extractions will fill these. A background re-tagging worker can lift
-- legacy rows when LLM cycles are available.

ALTER TABLE assessments
  ADD COLUMN IF NOT EXISTS property_id      BIGINT REFERENCES properties(property_id),
  ADD COLUMN IF NOT EXISTS value_id         BIGINT REFERENCES property_values(value_id),
  ADD COLUMN IF NOT EXISTS precision_tier   VARCHAR(16) DEFAULT 'unspecified',
  ADD COLUMN IF NOT EXISTS precision_depth  INTEGER;

CREATE INDEX IF NOT EXISTS idx_assess_property ON assessments (property_id) WHERE property_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_assess_value ON assessments (value_id) WHERE value_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_assess_precision ON assessments (precision_tier) WHERE precision_tier <> 'unspecified';

COMMENT ON COLUMN assessments.property_id IS
  'The property this assessment is about (color, time, …). NULL on legacy rows.';
COMMENT ON COLUMN assessments.value_id IS
  'The specific value asserted. NULL on legacy rows or when obj_value is free-form.';
COMMENT ON COLUMN assessments.precision_tier IS
  'Precision tier from precisionvocab.tiers (named|qualified|formal|spectral|enumerated|unspecified).';

-- ─── Topic depth (concept/instance + temporal_span) ───────────────────────
ALTER TABLE topics
  ADD COLUMN IF NOT EXISTS topic_kind        VARCHAR(16) DEFAULT 'instance',
  ADD COLUMN IF NOT EXISTS concept_topic_id  BIGINT REFERENCES topics(topic_id),
  ADD COLUMN IF NOT EXISTS temporal_span     TSTZRANGE,
  ADD COLUMN IF NOT EXISTS temporal_anchor   VARCHAR(16);

CREATE INDEX IF NOT EXISTS idx_topics_concept ON topics (concept_topic_id) WHERE concept_topic_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_topics_kind ON topics (topic_kind);
CREATE INDEX IF NOT EXISTS idx_topics_temporal ON topics USING GIST (temporal_span) WHERE temporal_span IS NOT NULL;

COMMENT ON COLUMN topics.topic_kind IS
  'concept (abstract category) | instance (specific case). Mirrors entity_kind.';
COMMENT ON COLUMN topics.concept_topic_id IS
  'If topic_kind=instance, points at the parent concept topic.';
COMMENT ON COLUMN topics.temporal_span IS
  'When this topic is active. Lower bound inclusive, upper exclusive. Open-ended valid.';

-- ─── Topic relations (the lattice between topics) ──────────────────────────
CREATE TABLE IF NOT EXISTS topic_relations (
  relation_id        BIGSERIAL PRIMARY KEY,
  source_topic_id    BIGINT NOT NULL REFERENCES topics(topic_id) ON DELETE CASCADE,
  target_topic_id    BIGINT NOT NULL REFERENCES topics(topic_id) ON DELETE CASCADE,
  relation_type      VARCHAR(32) NOT NULL,                 -- from topicvocab.relations
  weight             REAL NOT NULL DEFAULT 1.0,
  via                VARCHAR(32) NOT NULL DEFAULT 'manual',
  -- Federation: when this relation was inferred from another node's assertions
  source_node_id     VARCHAR(64),
  admitted_under_pack_version TEXT NOT NULL DEFAULT 'topicvocab@0.1.0',
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (source_topic_id, target_topic_id, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_trel_source ON topic_relations (source_topic_id, relation_type);
CREATE INDEX IF NOT EXISTS idx_trel_target ON topic_relations (target_topic_id, relation_type);

COMMENT ON TABLE topic_relations IS
  'Topic-to-topic relations from topicvocab. broader_than, narrower_than, '
  'peer_of, explains, contradicts, follows, references, same_as.';

COMMIT;
