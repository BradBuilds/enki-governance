-- 138: structural grading for statements — everything survives, structure
--      decides trust.
--
-- `validation_status` conflates two different judgements: "this collapsed
-- into an assessment" and "we could not understand this". 8,300 of the 9,092
-- rejections are the second kind, and rejection is terminal — the only place
-- `type_pair_invalid` appears in the codebase is the line that writes it. A
-- statement rejected on a lemma that enters the dictionary tomorrow stays
-- rejected forever; 161 statements are in exactly that position today.
--
-- The grade is a SEPARATE dimension, deliberately not a replacement. It
-- records what is structurally true about a statement — resolved subject,
-- something to point at, a predicate with a verb reading, a source span that
-- is prose rather than a docket caption — and it records the reasons, which
-- is what makes a statement re-gradable when the substrate improves.
--
-- Additive and non-deciding on arrival: this migration adds columns and
-- writes nothing. The grading sweep fills them and asserts that the 2,449
-- promotions do not move. Collapse eligibility only becomes a function of the
-- grade once that is proven.

ALTER TABLE statements ADD COLUMN IF NOT EXISTS structure_grade    TEXT;
ALTER TABLE statements ADD COLUMN IF NOT EXISTS structure_signals  JSONB;
ALTER TABLE statements ADD COLUMN IF NOT EXISTS structure_reasons  TEXT[];
ALTER TABLE statements ADD COLUMN IF NOT EXISTS graded_at          TIMESTAMPTZ;
ALTER TABLE statements ADD COLUMN IF NOT EXISTS grade_version      TEXT;

COMMENT ON COLUMN statements.structure_grade IS
    'collapsible | untrusted | evidence_only. untrusted is first-class: queryable, attached to evidence, never counted as fact, and re-graded when the map changes. evidence_only means raw evidence only — never deletion.';
COMMENT ON COLUMN statements.structure_reasons IS
    'Why this grade. What makes a re-grade targetable: find every statement whose only failure was a missing verb reading, and re-grade it when the lexicon gains one.';

CREATE INDEX IF NOT EXISTS idx_statements_structure_grade
    ON statements (structure_grade) WHERE structure_grade IS NOT NULL;

-- The re-grade queue: anything graded under an older rule, or never graded.
CREATE INDEX IF NOT EXISTS idx_statements_grade_stale
    ON statements (grade_version, statement_id)
    WHERE structure_grade IS NULL OR grade_version IS DISTINCT FROM 'v1';

-- Targeted re-grade: every statement held back by one specific reason.
CREATE INDEX IF NOT EXISTS idx_statements_structure_reasons
    ON statements USING GIN (structure_reasons)
    WHERE structure_reasons IS NOT NULL;
