-- 076_placeholder_expansion.sql
-- Expand the anaphora placeholder dictionary with the variants we saw
-- in real court-record libraries. Adds:
--   * "his/her" possessive variants
--   * "the X" variants for common nouns
--   * common short first names that are very often partial references
--     to a fuller "FirstName LastName" entity in the same doc
--   * common generic-person references
--
-- These rows enable the resolver to attempt a binding when these strings
-- appear as canonical_name of a noisy entity OR as subj_name/obj_name of
-- an assessment. The resolver still requires a dominant doc-level
-- candidate before writing a binding (no auto-bind on multi-candidate).

INSERT INTO anaphora_placeholders (placeholder, expected_type, notes) VALUES
    -- Possessive house/property variants
    ('his ranch',       'facility', 'possessive ranch reference'),
    ('her ranch',       'facility', 'possessive ranch reference'),
    ('his island',      'facility', 'possessive island reference'),
    ('her island',      'facility', 'possessive island reference'),
    ('the place',       'facility', 'generic place reference'),
    ('the residence',   'facility', 'residence reference'),
    ('his residence',   'facility', 'residence reference'),
    ('her residence',   'facility', 'residence reference'),
    ('his property',    'facility', 'property reference'),
    ('her property',    'facility', 'property reference'),
    ('the estate',      'facility', 'estate reference'),
    ('his estate',      'facility', 'estate reference'),
    ('the apartment',   'facility', 'apartment reference'),
    ('his apartment',   'facility', 'apartment reference'),
    ('her apartment',   'facility', 'apartment reference'),
    ('the place',       'facility', 'generic place'),

    -- Generic person references (resolvable when a paragraph names a person)
    ('the man',         'person',   'pronoun-like person reference'),
    ('the woman',       'person',   'pronoun-like person reference'),
    ('the girl',        'person',   'pronoun-like person reference'),
    ('the boy',         'person',   'pronoun-like person reference'),
    ('the lawyer',      'person',   'role reference'),
    ('the attorney',    'person',   'role reference'),
    ('the witness',     'person',   'role reference'),
    ('the victim',      'person',   'role reference'),
    ('the suspect',     'person',   'role reference'),
    ('the defendant',   'person',   'role reference'),
    ('the prosecutor',  'person',   'role reference'),
    ('the agent',       'person',   'role reference'),
    ('the officer',     'person',   'role reference'),
    ('the detective',   'person',   'role reference')
ON CONFLICT (placeholder) DO NOTHING;

-- Common short first-name forms. These need ALIAS-BRIDGE resolution
-- rather than doc-singleton resolution: "Al" + a document → look for
-- "Alex *" in same doc, pick the dominant one.
-- We mark these with expected_type='person' and a `notes` column hint;
-- the resolver routes them to a different binding strategy.
INSERT INTO anaphora_placeholders (placeholder, expected_type, notes) VALUES
    ('jeff',     'person', 'alias_bridge:Jeffrey'),
    ('jeffrey',  'person', 'alias_bridge:Jeffrey'),
    ('mike',     'person', 'alias_bridge:Michael'),
    ('michael',  'person', 'alias_bridge:Michael'),
    ('bob',      'person', 'alias_bridge:Robert'),
    ('rob',      'person', 'alias_bridge:Robert'),
    ('robert',   'person', 'alias_bridge:Robert'),
    ('bill',     'person', 'alias_bridge:William'),
    ('will',     'person', 'alias_bridge:William'),
    ('don',      'person', 'alias_bridge:Donald'),
    ('al',       'person', 'alias_bridge:Albert'),
    ('chris',    'person', 'alias_bridge:Christopher'),
    ('andy',     'person', 'alias_bridge:Andrew'),
    ('andrew',   'person', 'alias_bridge:Andrew'),
    ('jim',      'person', 'alias_bridge:James'),
    ('tom',      'person', 'alias_bridge:Thomas'),
    ('tony',     'person', 'alias_bridge:Anthony'),
    ('ron',      'person', 'alias_bridge:Ronald'),
    ('joe',      'person', 'alias_bridge:Joseph'),
    ('dick',     'person', 'alias_bridge:Richard'),
    ('rick',     'person', 'alias_bridge:Richard'),
    ('rich',     'person', 'alias_bridge:Richard'),
    ('alex',     'person', 'alias_bridge:Alexander'),
    ('ed',       'person', 'alias_bridge:Edward'),
    ('eddie',    'person', 'alias_bridge:Edward'),
    ('sam',      'person', 'alias_bridge:Samuel'),
    ('dan',      'person', 'alias_bridge:Daniel'),
    ('matt',     'person', 'alias_bridge:Matthew'),
    ('nick',     'person', 'alias_bridge:Nicholas')
ON CONFLICT (placeholder) DO NOTHING;
