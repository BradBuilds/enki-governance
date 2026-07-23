-- 061: Two-layer entity model + GDU base-60 addressing
--
-- Until now, every entity row was an "instance" — a specific real-world thing
-- the docs were about. But the docs also reference ABSTRACT CONCEPTS:
-- the word "aircraft" anchors the page to the AIRCRAFT type/concept; the
-- word "person" anchors to PERSON. These concept-level matches are NOT
-- noise — they're the dictionary that gives the entire dossier shared
-- vocabulary AND makes cross-ENKI sharing work (we both know
-- "concept/aircraft" means the same thing).
--
-- Two-layer model:
--   entity_kind='concept'   abstract class node (Wikidata metaclass)
--                           e.g. "aircraft" (Q11436), "person" (Q5),
--                                "city" (Q515), "politician" (Q82955)
--   entity_kind='instance'  specific real thing
--                           e.g. "Alex Acme" (Q2222222, instance of person),
--                                "Boeing 727 N123AA" (instance of aircraft),
--                                "New Mexico" (Q1522, instance of U.S. state)
--
-- Each instance points UP to its concept via concept_entity_id, forming
-- the taxonomy that the address book uses for cross-corpus collapse.
--
-- base60_address: the existing GDU [category].[subtype].[sequence] format
-- (core/gdu/address.py). Document-promoted entities now get one too, so
-- the entity layer shares the same address space as knowledge_nodes.

ALTER TABLE entities
    ADD COLUMN IF NOT EXISTS entity_kind TEXT NOT NULL DEFAULT 'instance'
        CHECK (entity_kind IN ('concept', 'instance'));

ALTER TABLE entities
    ADD COLUMN IF NOT EXISTS concept_entity_id BIGINT
        REFERENCES entities(entity_id) ON DELETE SET NULL;

ALTER TABLE entities
    ADD COLUMN IF NOT EXISTS base60_address TEXT;

CREATE INDEX IF NOT EXISTS idx_entities_kind ON entities(entity_kind);
CREATE INDEX IF NOT EXISTS idx_entities_concept ON entities(concept_entity_id)
    WHERE concept_entity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_entities_base60 ON entities(base60_address)
    WHERE base60_address IS NOT NULL;

-- Concept entities are unique per (tenant, identifier_hash) — same logic as
-- existing entities_identifier_hash_unique, just clarified for the concept
-- layer where the identifier is the Wikidata QID of the metaclass.

-- We also extend wikidata_type_cache with an is_concept flag so the
-- promoter can ask "is this QID a class/metaclass?" without re-fetching
-- the Wikidata claims every time.
ALTER TABLE wikidata_type_cache
    ADD COLUMN IF NOT EXISTS is_concept BOOLEAN DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_wd_cache_concept ON wikidata_type_cache(is_concept)
    WHERE is_concept;
