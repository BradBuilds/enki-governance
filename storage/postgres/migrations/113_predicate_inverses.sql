-- Migration 113: declare common predicate inverse pairs.
--
-- The validator at core.validation.data_integrity.check_predicate_direction
-- uses gdu_type_constraints.inverse_edge_type to detect direction errors:
--
--   Given assessment (X employed Y), if a worked_for pair is also declared,
--   the validator can spot (X worked_for Y) elsewhere as the same fact
--   asserted both ways — one of them is backwards.
--
-- We add the directional pairs that are most common in any
-- intelligence / legal / financial corpus and were missing from the
-- shipped edgevocab pack. Bidirectional UPDATEs so both rows know
-- about each other.

-- ── employed / worked_for ────────────────────────────────────────────
UPDATE gdu_type_constraints
   SET inverse_edge_type = 'worked_for',
       updated_at = NOW()
 WHERE edge_type = 'employed';

INSERT INTO gdu_type_constraints (
    edge_type, label, description,
    source_types, target_types,
    is_symmetric, fires_automatically, inverse_edge_type,
    pack_source, pack_version
)
SELECT 'worked_for',
       'Worked For',
       'Employment relationship (inverse of employed). Source was employed by target.',
       ARRAY['entity','person']::TEXT[],
       ARRAY['entity','organization','company']::TEXT[],
       FALSE, TRUE, 'employed',
       'edgevocab', '0.2.1'
 WHERE NOT EXISTS (SELECT 1 FROM gdu_type_constraints WHERE edge_type = 'worked_for');

-- ── paid / received_from already exist as separate rows — link them ──
UPDATE gdu_type_constraints
   SET inverse_edge_type = 'received_from',
       updated_at = NOW()
 WHERE edge_type = 'paid' AND inverse_edge_type IS NULL;

UPDATE gdu_type_constraints
   SET inverse_edge_type = 'paid',
       updated_at = NOW()
 WHERE edge_type = 'received_from' AND inverse_edge_type IS NULL;

-- ── controls / controlled_by ─────────────────────────────────────────
UPDATE gdu_type_constraints
   SET inverse_edge_type = 'controlled_by',
       updated_at = NOW()
 WHERE edge_type = 'controls' AND inverse_edge_type IS NULL;

INSERT INTO gdu_type_constraints (
    edge_type, label, description,
    source_types, target_types,
    is_symmetric, fires_automatically, inverse_edge_type,
    pack_source, pack_version
)
SELECT 'controlled_by',
       'Controlled By',
       'Inverse of controls.',
       ARRAY['entity']::TEXT[],
       ARRAY['entity']::TEXT[],
       FALSE, TRUE, 'controls',
       'edgevocab', '0.2.1'
 WHERE NOT EXISTS (SELECT 1 FROM gdu_type_constraints WHERE edge_type = 'controlled_by');

-- ── traveled_to / traveled_from ──────────────────────────────────────
UPDATE gdu_type_constraints
   SET inverse_edge_type = 'traveled_from',
       updated_at = NOW()
 WHERE edge_type = 'traveled_to' AND inverse_edge_type IS NULL;

INSERT INTO gdu_type_constraints (
    edge_type, label, description,
    source_types, target_types,
    is_symmetric, fires_automatically, inverse_edge_type,
    pack_source, pack_version
)
SELECT 'traveled_from',
       'Traveled From',
       'Origin point of a travel event (inverse of traveled_to).',
       ARRAY['entity','person']::TEXT[],
       ARRAY['place']::TEXT[],
       FALSE, TRUE, 'traveled_to',
       'edgevocab', '0.2.1'
 WHERE NOT EXISTS (SELECT 1 FROM gdu_type_constraints WHERE edge_type = 'traveled_from');

-- ── departed_from / arrived_at — already typed as place-target,
--    they're loose inverses (departed_from X usually pairs with
--    arrived_at Y, not arrived_at X). Skip linking them.
