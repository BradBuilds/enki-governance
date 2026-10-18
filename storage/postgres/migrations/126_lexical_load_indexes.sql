-- 126_lexical_load_indexes.sql
--
-- One index that enables CROSS-LIBRARY relation resolution. (The concept_address
-- join used by the binding upsert is already served by the existing unique
-- constraint concept_nodes_tenant_id_concept_address_key — no extra index needed.)
--
--  * (library, sense_id) functional index on concepts — lets a relation in one
--    library resolve a target sense in ANOTHER library by its stable ref, so the
--    expansion graph crosses library boundaries (demonym→place, the slang and
--    literal "fire" senses, glossary term→wordnet sense).

-- NOT partial: the cross-library relation-link join resolves endpoints by
-- (library, sense_id) WITHOUT a subtype_path predicate, so a partial index can't
-- be matched (planner falls back to a seq scan). A full functional index is used.
CREATE INDEX IF NOT EXISTS idx_concept_nodes_senseref
    ON concept_nodes ((type_evidence->>'library'), (type_evidence->>'sense_id'));
