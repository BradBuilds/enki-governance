-- 062: Form layer — base-60 cyclical language model substrate
--
-- Every distinct surface form (token / lemma) that has appeared in any
-- document gets a stable base-60 address. Resolution decisions made on
-- that form across the corpus aggregate into an empirical, addressable,
-- reversible distribution. This is the substrate for the cyclical
-- language model described in
-- the internal base60 cyclical language model notes.
--
-- A `form` is NOT an entity. It is the physical text. When a form
-- occurrence is resolved (e.g. "General" → entity.person rank prefix,
-- or "general" → adjective / topic-modifier), that decision is recorded
-- in collapse_decisions and the form's distribution updates. Over time
-- the distribution IS the language model — fully queryable, fully
-- auditable, fully reversible.
--
-- Eight subtypes, chosen for disambiguation utility:
--   noun, verb, adjective, function_word, initialism,
--   number_word, name_token, unknown (parking lot)
--
-- Indexes:
--   form_id            primary key
--   (surface_norm)     fast lookup by form text
--   base60_address     unique, for cross-ENKI shareability
--   subtype            for slicing the grid by linguistic role

CREATE TABLE IF NOT EXISTS language_forms (
    form_id          BIGSERIAL PRIMARY KEY,
    surface_norm     TEXT NOT NULL,
    surface_display  TEXT NOT NULL,
    subtype          TEXT NOT NULL CHECK (subtype IN (
                       'noun','verb','adjective','function_word',
                       'initialism','number_word','name_token','unknown')),
    base60_address   TEXT NOT NULL UNIQUE,
    first_seen       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    occurrence_count BIGINT NOT NULL DEFAULT 0,
    classification_source TEXT,
    notes            TEXT,
    UNIQUE (surface_norm, subtype)
);

CREATE INDEX IF NOT EXISTS idx_language_forms_norm
    ON language_forms (surface_norm);
CREATE INDEX IF NOT EXISTS idx_language_forms_subtype
    ON language_forms (subtype);
CREATE INDEX IF NOT EXISTS idx_language_forms_addr
    ON language_forms (base60_address);

-- Seed the address sequence slots for `form` category subtypes.
-- Subtype index within `form` follows nodevocab subtype order:
--   noun=0, verb=1, adjective=2, function_word=3,
--   initialism=4, number_word=5, name_token=6, unknown=7
INSERT INTO gdu_address_sequences (category, subtype, next_seq)
VALUES
    ('form', 'noun',          1),
    ('form', 'verb',          1),
    ('form', 'adjective',     1),
    ('form', 'function_word', 1),
    ('form', 'initialism',    1),
    ('form', 'number_word',   1),
    ('form', 'name_token',    1),
    ('form', 'unknown',       1)
ON CONFLICT (category, subtype) DO NOTHING;

-- View: a form's resolution distribution from collapse_decisions.
-- "For this form, where has the resolver been pointing across the
--  corpus, and how confidently?"
-- This is the empirical posterior. Future occurrences use it as
-- a prior, closing the cyclical model loop.
DROP VIEW IF EXISTS language_form_distributions;
CREATE VIEW language_form_distributions AS
SELECT f.form_id,
       f.surface_norm,
       f.subtype                                  AS form_subtype,
       f.base60_address                           AS form_address,
       cd.action,
       cd.confidence_tier,
       COUNT(*)                                    AS observations,
       AVG(cd.score)                               AS avg_score,
       MIN(cd.decided_at)                          AS first_observed,
       MAX(cd.decided_at)                          AS last_observed
  FROM language_forms f
  LEFT JOIN collapse_decisions cd
    ON cd.surface_form_norm = f.surface_norm
   AND cd.reverted_at IS NULL
 GROUP BY f.form_id, f.surface_norm, f.subtype, f.base60_address,
          cd.action, cd.confidence_tier;
