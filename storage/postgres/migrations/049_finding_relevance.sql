-- Migration 049: per-finding relevance scoring.
--
-- Phase 7d of the internal research validation plan.
--
-- Worked example: David Harlow (Acme Widgets CEO) investigation
-- discovered 5 court cases via CourtListener:
--   * Brian David Harlow bankruptcy → wrong David (different first name)
--   * REYES v. GLOBEX TECHNOLOGIES → wrong David (Globex vs Acme)
--   * Moore v. Carter → unclear (name not in case_name)
--   * VARGAS v. ACME WIDGETS CORP → CORRECT (Acme Widgets)
--   * generic case-count summary
--
-- Without per-finding relevance, the dossier shows all 5 with equal
-- weight and the user has to manually click each to disambiguate.
--
-- This migration adds the columns the scorer worker writes; the
-- companion finding_relevance_scorer.py worker (Phase 7d code) calls
-- Gemma to compare each finding to its parent entity and emits
-- a 0.0-1.0 score + reason. The dossier endpoint then filters by
-- relevance >= 0.5 by default with a UI toggle to show all.

BEGIN;

ALTER TABLE investigation_findings
    ADD COLUMN IF NOT EXISTS relevance_score   REAL,
    ADD COLUMN IF NOT EXISTS relevance_reason  TEXT,
    ADD COLUMN IF NOT EXISTS relevance_scored_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS relevance_model   VARCHAR(64);

-- Filter index: dossier query uses relevance_score >= 0.5 most of the time.
CREATE INDEX IF NOT EXISTS idx_findings_relevance
    ON investigation_findings (entity_id, relevance_score DESC NULLS LAST)
    WHERE entity_id IS NOT NULL;

-- The scorer worker uses this index to find unscored rows efficiently.
CREATE INDEX IF NOT EXISTS idx_findings_unscored
    ON investigation_findings (created_at DESC)
    WHERE relevance_score IS NULL;

COMMENT ON COLUMN investigation_findings.relevance_score IS
    '0.0-1.0 score from Phase 7d relevance scorer. 1.0 = clearly the same entity, '
    '0.7 = plausible (name + context match), 0.3 = weak (substring only), '
    '0.0 = clearly wrong (different person/org with same name). NULL = unscored.';

COMMENT ON COLUMN investigation_findings.relevance_reason IS
    'Free-text reasoning from the scorer model. Used in the dossier UI '
    'when the user hovers a low-relevance finding to see WHY the system '
    'thinks it might not belong.';

COMMENT ON COLUMN investigation_findings.relevance_model IS
    'Which model produced the score (e.g. gemma4:e2b). Lets us re-score '
    'when the model upgrades and tells the UI what to attribute the score to.';

COMMIT;
