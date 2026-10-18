-- 063: Lemma layer + materialized priors + form↔concept canonical edge
--
-- Three additions to the base-60 cyclical language model substrate:
--
-- A. Lemma layer — orthographic stems group surface forms together.
--    The lemma carries the canonical_concept_address (the "dictionary
--    entry"): the concept this lemma most strongly refers to. This is
--    what closes the form ↔ concept ↔ instance cycle: form is the
--    physical text, lemma is the lexical handle, concept is the
--    ontological referent, instance is the specific real-world thing.
--
-- B. Materialized priors on language_forms — caching the empirical
--    distribution over resolution addresses so the resolver can
--    consult it in microseconds instead of recomputing the
--    language_form_distributions view on every mention.
--
-- C. Lemma subtype on the form category — lemmas themselves are
--    addressable in the base-60 grid. Subtype index 8 within `form`.

-- ── A. Lemma layer ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS language_lemmas (
    lemma_id                  BIGSERIAL PRIMARY KEY,
    lemma_norm                TEXT NOT NULL UNIQUE,
    primary_pos               TEXT NOT NULL CHECK (primary_pos IN (
                                'noun','verb','adjective','function_word',
                                'initialism','number_word','name_token','unknown')),
    base60_address            TEXT NOT NULL UNIQUE,
    canonical_concept_address TEXT,
    canonical_concept_entity_id BIGINT REFERENCES entities(entity_id) ON DELETE SET NULL,
    surface_variant_count     INTEGER NOT NULL DEFAULT 0,
    total_occurrences         BIGINT  NOT NULL DEFAULT 0,
    first_seen                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    classification_source     TEXT,
    notes                     TEXT
);

CREATE INDEX IF NOT EXISTS idx_language_lemmas_norm
    ON language_lemmas (lemma_norm);
CREATE INDEX IF NOT EXISTS idx_language_lemmas_addr
    ON language_lemmas (base60_address);
CREATE INDEX IF NOT EXISTS idx_language_lemmas_concept
    ON language_lemmas (canonical_concept_address)
    WHERE canonical_concept_address IS NOT NULL;

-- Each form points UP to its lemma. Many forms → one lemma.
ALTER TABLE language_forms
    ADD COLUMN IF NOT EXISTS lemma_id BIGINT
        REFERENCES language_lemmas(lemma_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_language_forms_lemma
    ON language_forms (lemma_id)
    WHERE lemma_id IS NOT NULL;

-- Add `lemma` subtype (index 8) to the form category. Sequence row
-- so addresses can mint normally via assign_address(conn,'form','lemma').
INSERT INTO gdu_address_sequences (category, subtype, next_seq)
VALUES ('form', 'lemma', 1)
ON CONFLICT (category, subtype) DO NOTHING;


-- ── B. Materialized priors on language_forms ──────────────────────────

ALTER TABLE language_forms
    ADD COLUMN IF NOT EXISTS top_resolution_address TEXT,
    ADD COLUMN IF NOT EXISTS top_resolution_count BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS resolution_entropy REAL,
    ADD COLUMN IF NOT EXISTS n_decisions BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS priors_updated_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_language_forms_priors
    ON language_forms (priors_updated_at)
    WHERE priors_updated_at IS NOT NULL;


-- recompute_form_priors() — refresh the cached prior on each form
-- from the collapse_decisions log. Cheap to call after a promotion
-- batch; idempotent; safe to call from anywhere.
--
-- "decided_address" doesn't exist on collapse_decisions yet — for now
-- we proxy with merged_into_entity_id's base60_address. When the
-- resolver formally records decided_address (Phase 2), this function
-- will read from that column directly.
CREATE OR REPLACE FUNCTION recompute_form_priors() RETURNS INTEGER AS $$
DECLARE
    updated_count INTEGER := 0;
BEGIN
    WITH per_form_per_address AS (
        SELECT cd.surface_form_norm,
               COALESCE(e2.base60_address, e.base60_address, 'unresolved')
                 AS decided_address,
               COUNT(*) AS n
          FROM collapse_decisions cd
          LEFT JOIN entities e2 ON e2.entity_id = cd.merged_into_entity_id
          LEFT JOIN entities e  ON e.entity_id  = cd.entity_id
         WHERE cd.reverted_at IS NULL
           AND cd.surface_form_norm IS NOT NULL
         GROUP BY cd.surface_form_norm, decided_address
    ),
    per_form_totals AS (
        SELECT surface_form_norm, SUM(n)::numeric AS total_n
          FROM per_form_per_address
         GROUP BY surface_form_norm
    ),
    per_form_summary AS (
        SELECT a.surface_form_norm,
               t.total_n,
               (ARRAY_AGG(a.decided_address ORDER BY a.n DESC))[1] AS top_addr,
               (ARRAY_AGG(a.n               ORDER BY a.n DESC))[1] AS top_n,
               -SUM(
                   (a.n::numeric / t.total_n)
                   * LOG(2::numeric,
                         GREATEST(a.n::numeric / t.total_n, 1e-12::numeric))
               ) AS entropy
          FROM per_form_per_address a
          JOIN per_form_totals t USING (surface_form_norm)
         GROUP BY a.surface_form_norm, t.total_n
    )
    UPDATE language_forms f
       SET top_resolution_address = s.top_addr,
           top_resolution_count   = s.top_n,
           resolution_entropy     = s.entropy::REAL,
           n_decisions            = s.total_n::BIGINT,
           priors_updated_at      = NOW()
      FROM per_form_summary s
     WHERE f.surface_norm = s.surface_form_norm;

    GET DIAGNOSTICS updated_count = ROW_COUNT;
    RETURN updated_count;
END;
$$ LANGUAGE plpgsql;


-- View update: language_form_distributions now exposes the materialized
-- prior alongside the raw distribution, so consumers can pick whichever
-- shape they need.
DROP VIEW IF EXISTS language_form_distributions;
CREATE VIEW language_form_distributions AS
SELECT f.form_id,
       f.surface_norm,
       f.subtype                                  AS form_subtype,
       f.base60_address                           AS form_address,
       f.lemma_id,
       l.lemma_norm,
       l.base60_address                           AS lemma_address,
       l.canonical_concept_address,
       f.top_resolution_address,
       f.top_resolution_count,
       f.resolution_entropy,
       f.n_decisions,
       cd.action,
       cd.confidence_tier,
       COUNT(cd.decision_id)                       AS observations,
       AVG(cd.score)                               AS avg_score,
       MIN(cd.decided_at)                          AS first_observed,
       MAX(cd.decided_at)                          AS last_observed
  FROM language_forms f
  LEFT JOIN language_lemmas l ON l.lemma_id = f.lemma_id
  LEFT JOIN collapse_decisions cd
    ON cd.surface_form_norm = f.surface_norm
   AND cd.reverted_at IS NULL
 GROUP BY f.form_id, f.surface_norm, f.subtype, f.base60_address, f.lemma_id,
          l.lemma_norm, l.base60_address, l.canonical_concept_address,
          f.top_resolution_address, f.top_resolution_count,
          f.resolution_entropy, f.n_decisions,
          cd.action, cd.confidence_tier;
