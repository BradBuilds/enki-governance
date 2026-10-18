-- 125_concept_relations.sql
--
-- Concept→concept relation edges — the EXPANSION half of the language model.
-- Collapse narrows a form to one sense; expansion walks these edges to related
-- senses. WordNet supplies ontological relations (hypernym/hyponym/meronym/
-- synonym); a thesaurus library supplies associative ones (related). Both land
-- here, governed by source_library + rel_type. Populates the relation types that
-- language.semantics names but leaves empty.
--
-- Symmetric relations (synonym/antonym/related) store endpoints canonicalized
-- (a < b) so A~B and B~A are ONE edge — same determinism rule as undirected
-- knowledge edges. Directional relations (hypernym→hyponym) keep their direction.

CREATE TABLE IF NOT EXISTS concept_relations (
    relation_id     BIGSERIAL PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    concept_id_a    BIGINT NOT NULL REFERENCES concept_nodes(concept_id) ON DELETE CASCADE,
    concept_id_b    BIGINT NOT NULL REFERENCES concept_nodes(concept_id) ON DELETE CASCADE,
    rel_type        VARCHAR(32) NOT NULL,   -- synonym|antonym|hypernym|hyponym|meronym|holonym|related
    is_symmetric       BOOLEAN NOT NULL DEFAULT FALSE,
    source_library  VARCHAR(64) NOT NULL,
    confidence      REAL NOT NULL DEFAULT 1.0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (concept_id_a, concept_id_b, rel_type)
);

CREATE INDEX IF NOT EXISTS idx_concept_relations_a ON concept_relations(concept_id_a, rel_type);
CREATE INDEX IF NOT EXISTS idx_concept_relations_b ON concept_relations(concept_id_b, rel_type);
