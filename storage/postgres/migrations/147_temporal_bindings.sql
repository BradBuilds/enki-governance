-- Migration 147 — meaning moves at the binding layer
--
-- Phase 4 of the internal temporal constitutive substrate plan. The only
-- invasive phase in that plan, and a PREREQUISITE rather than a feature: Phase 5
-- (the resolution clock and the anachronism veto) cannot exist without it.
--
-- THE PRINCIPLE
--
--   Concepts do not drift; bindings do.
--
-- The string "sick" is timeless. The referents [illness] and [excellent] are
-- timeless. What changed between 1800 and 2005 is WHICH FORM BINDS TO WHICH
-- CONCEPT. So meaning-change is an interval on `form_concept_bindings` — never
-- a mutation of `concept_nodes`, and never a new address.
--
-- That last clause is the load-bearing one. Base-60 addresses must be stable or
-- nothing is comparable across time or across nodes; deterministic collapse and
-- federation both depend on two machines computing the same address for the same
-- referent. STRUCTURE HOLDS AT THE COORDINATE LAYER PRECISELY SO MEANING CAN MOVE
-- AT THE BINDING LAYER. Nothing here touches an address.
--
-- LEXICAL DRIFT AND ATTRIBUTE CHANGE ARE ONE MECHANISM
--
--   "sick" -> [excellent]                            valid 1980 -> present
--   Acme Mobile Corp -> reg. agent John Smith   valid 2019-09-11 -> 2021-10-05
--
-- Same row shape: a binding, an interval, a source. Migration 051 already built
-- it for one slot (`entity_addresses.valid_from`/`valid_to`, DATE, NULLable,
-- populated only where a source like SEC publishes effective dates). This is
-- that pattern, generalised to the layer where meaning lives. The types and the
-- nullability deliberately match 051 so the two read the same way.
--
-- ── OPEN QUESTION 2, DECIDED: A DEAD REFERENT IS NOT A DEAD WORD ───────────
--
-- Yugoslavia stopped existing in 1992. The WORD did not stop being used, and it
-- is still the correct binding for a 1985 document — indeed for a 2026 history
-- of the Balkans. Those are two different facts:
--
--   concept_nodes.existed_from / existed_to        the REFERENT's lifetime
--   form_concept_bindings.valid_from / valid_to    the BINDING's validity
--
-- They get different columns AND deliberately different NAMES. Had both pairs
-- been called `valid_*`, a join across the two tables would produce a query that
-- reads correctly and answers the wrong question — "nobody says this any more"
-- returned for "this thing stopped existing". Naming them apart makes that
-- confusion unreachable rather than merely discouraged.
--
-- Nothing needs existence intervals today; Phase 5's anachronism veto reads the
-- BINDING interval. They are declared here anyway because without a column the
-- only way to express "Yugoslavia ended in 1992" is `valid_to` on its bindings,
-- which asserts something false. Two NULLable columns with no writer are cheap;
-- a semantic conflation discovered after it has data is not.
--
-- ── OPEN QUESTION 4, DECIDED: ONE OBSERVATION IS NOT AN INTERVAL ───────────
--
-- A binding seen once gets NULL/NULL, not `valid_from = valid_to = observed_at`:
--
--   1. `created_at` on this table already records when we saw it, so a point
--      interval stores nothing that is not already stored.
--   2. A closed point asserts the binding was valid at that instant and NOT
--      before or after — the strongest of the available claims, and almost
--      always false.
--   3. Phase 5 VETOES on these intervals. A `valid_from` manufactured from a
--      single sighting would refuse every earlier document on the strength of
--      one data point: an anachronism veto firing on evidence we invented.
--
-- An interval is therefore written ONLY when a source publishes one. When it is,
-- the publisher is recorded in `binding_evidence->>'interval_source'`, and
-- `scripts/exit_criteria_phase4.py` [5] refuses an asserted interval that names
-- no publisher — the same rule as `slot_blocks.source NOT NULL` (migration 145):
-- an unattributed claim is indistinguishable from an excuse.
--
-- ── NO BACKFILL ───────────────────────────────────────────────────────────
--
-- 274,774 bindings exist and we do not know when any of them became valid. NULL
-- is not a gap here, it is the honest reading: "no interval asserted". Same rule
-- as migrations 140, 141, 143 and 145 — this migration contains DDL and nothing
-- else, and `exit_criteria_phase4.py` [3] fails if an UPDATE ever appears in it.
--
-- The columns carry NO DEFAULT for the same reason. A DEFAULT is the quiet form
-- of a backfill: it would invent an interval for every row written after it,
-- with no source that published one.
--
-- ── WHAT THIS MIGRATION DELIBERATELY DOES NOT DO ──────────────────────────
--
-- No index. Phase 4's exit criterion is "bindings can carry intervals; nothing
-- yet reads them", so an index would be tuning for a query that does not exist,
-- against zero asserted intervals. It belongs in the same change as the Phase 5
-- reader that needs it, where its shape can be chosen from a real query plan.
--
-- ROLLBACK: four DROP COLUMNs. Nothing reads these, so dropping them loses the
-- capability and never a record.

BEGIN;

-- ── the binding's validity: when did this FORM mean this CONCEPT? ──────────

ALTER TABLE form_concept_bindings
    ADD COLUMN IF NOT EXISTS valid_from DATE,
    ADD COLUMN IF NOT EXISTS valid_to   DATE;

COMMENT ON COLUMN form_concept_bindings.valid_from IS
    'Start of the interval over which this form binds to this concept. NULL = no '
    'interval asserted, which is the honest reading of a binding observed once. '
    'Written only where a source publishes an effective date; the publisher is '
    'recorded in binding_evidence->>''interval_source''. Never backfilled.';

COMMENT ON COLUMN form_concept_bindings.valid_to IS
    'End of the interval. NULL = no end asserted (the binding is current, or we '
    'simply do not know). This is NOT the referent''s lifetime — see '
    'concept_nodes.existed_to. A dead referent is not a dead word.';

-- An interval that ends before it starts is not a weak claim; it is not a claim
-- at all. Cheap to refuse at write time, and impossible to reason about later.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'form_concept_bindings_interval_ordered') THEN
        ALTER TABLE form_concept_bindings
            ADD CONSTRAINT form_concept_bindings_interval_ordered
            CHECK (valid_from IS NULL OR valid_to IS NULL OR valid_from <= valid_to);
    END IF;
END $$;

-- ── the referent's lifetime: when did this THING exist? ───────────────────

ALTER TABLE concept_nodes
    ADD COLUMN IF NOT EXISTS existed_from DATE,
    ADD COLUMN IF NOT EXISTS existed_to   DATE;

COMMENT ON COLUMN concept_nodes.existed_from IS
    'Start of the REFERENT''s existence (Yugoslavia: 1918). Not the binding''s '
    'validity — see form_concept_bindings.valid_from. NULL = not asserted. No '
    'writer today; declared so that "this thing stopped existing" has somewhere '
    'true to go instead of being expressed as "nobody says this any more".';

COMMENT ON COLUMN concept_nodes.existed_to IS
    'End of the REFERENT''s existence (Yugoslavia: 1992). The word remains the '
    'correct binding for documents written before, during and after.';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'concept_nodes_existence_ordered') THEN
        ALTER TABLE concept_nodes
            ADD CONSTRAINT concept_nodes_existence_ordered
            CHECK (existed_from IS NULL OR existed_to IS NULL
                   OR existed_from <= existed_to);
    END IF;
END $$;

COMMIT;
