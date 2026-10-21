-- 141: stamp WHICH regime produced a completeness score.
--
-- completeness_score has had eight independent authors, none of which read the
-- governed slot specs:
--
--   extraction/extractors/events.py:180   score = the event's CONFIDENCE, tier = 'bronze'
--   workers/event_validator.py:160-278    0.4 + inline +0.1/+0.15/+0.25/+0.05
--   workers/assessment_validator.py:240   0.3 base, +/- corroboration
--   workers/auto_validator.py:170         0.1 -> 0.4 Wikipedia -> 0.6 Wikidata
--   workers/place_validator.py:172        same source-presence shape
--   workers/document_validator.py:219     SQL floor GREATEST(score, 0.5)
--   gdu/cross_category_migrator.py:222    function parameter, default 0.6
--   extraction/auto_node.py:266           envelope passthrough, default 0.0/'empty'
--
-- while core/gdu/completeness.py — the pack-governed scorer that reads
-- completenessvocab.slot_specs — has never had a caller. The consequence is
-- measurable: on the production node only `document` and `event` carry any score
-- at all (assessment.fact 55,437 nodes, entity.person 32,665 and entity.vessel 84
-- all sit at 0.00), 47,540 event rows carry the tier 'bronze' which no pack
-- declares, and score and tier have drifted out of correspondence — event 0.90
-- reads 'bronze' while event 0.83 reads 'solid'.
--
-- A score whose author is unknown cannot be compared to one that is, so this
-- records the author rather than trying to repair the number.
--
-- NO BACKFILL of the scores themselves. We do not know what most of the existing
-- values meant and must not restate them as slot-derived — the same rule as
-- migration 140 (blocked != empty) and the binding-interval rule that follows it.
-- Existing rows are marked, not recomputed:
--
--   'legacy:unattributable'  something wrote a score; which of the eight is not
--                            recoverable from the row
--   NULL                     never scored at all. An absent score, which is the
--                            honest reading of a 0.00 with no tier — not a zero
--                            score, and not a gap to be filled by assumption.
--
-- Rows scored from here on carry the pack versions that produced them, e.g.
-- 'slot_specs@0.1.1/tiers@0.1.0'. When Phase 1 bumps slot_specs to make
-- entity.vessel a constitutive decomposition, every node still stamped with the
-- old version is visibly stale rather than silently so.

ALTER TABLE knowledge_nodes
    ADD COLUMN IF NOT EXISTS completeness_regime TEXT;
ALTER TABLE entities
    ADD COLUMN IF NOT EXISTS completeness_regime TEXT;

COMMENT ON COLUMN knowledge_nodes.completeness_regime IS
    'Which regime produced completeness_score/_tier: the pack versions that '
    'computed it (e.g. slot_specs@0.1.1/tiers@0.1.0), ''legacy:unattributable'' '
    'for scores predating the governed scorer, or NULL for never scored. '
    'NULL is an absent score, NOT a zero.';
COMMENT ON COLUMN entities.completeness_regime IS
    'See knowledge_nodes.completeness_regime.';

-- Mark, do not recompute. Only rows that actually carry a score are attributed;
-- a 0.00 with no tier was never scored and stays NULL.
UPDATE knowledge_nodes
   SET completeness_regime = 'legacy:unattributable'
 WHERE completeness_regime IS NULL
   AND (COALESCE(completeness_score, 0) > 0
        OR COALESCE(completeness_tier, '') <> '');

UPDATE entities
   SET completeness_regime = 'legacy:unattributable'
 WHERE completeness_regime IS NULL
   AND (COALESCE(completeness_score, 0) > 0
        OR COALESCE(completeness_tier, '') <> '');

CREATE INDEX IF NOT EXISTS idx_knowledge_nodes_completeness_regime
    ON knowledge_nodes (completeness_regime);
