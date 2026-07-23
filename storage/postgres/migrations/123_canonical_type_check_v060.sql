-- 123_canonical_type_check_v060.sql
--
-- Reconcile the entities.canonical_type storage constraint with the
-- entityvocab pack (now v0.6.0) so the two can never disagree again.
--
-- THE BUG THIS FIXES
-- ------------------
-- Migration 116 hand-wrote a 13-type entity whitelist. Migration 118 then
-- unified the places table INTO entities (country/city/region/... became
-- first-class entity rows) but never updated 116's constraint. Result:
-- ~5,500 place-typed rows the constraint rejects on ANY update. event/auto
-- _validator crash-looped because a CHECK constraint re-validates the WHOLE
-- row on UPDATE — touching Kuwait's envelope re-checked its existing
-- canonical_type='country' and failed.
--
-- THE FIX
-- -------
-- entityvocab v0.6.0 promotes the 6 place subtypes to accepted entity types.
-- This constraint mirrors subtype_validator.valid_entity_subtypes() exactly:
--   13 entity types + 6 place types = 19. (date/event/concept/work_of_art/
--   number are NOT here — they correctly route out via cross_category_routes.)
--
-- The 19-value list below is GENERATED from the pack. If you edit the pack,
-- regenerate this and bump the version. core/validation/data_integrity.py
-- check_constraint_matches_pack() asserts the live constraint == the pack at
-- runtime, so drift is caught loudly instead of crash-looping a worker.
--
-- APPLIED NOT VALID — ON PURPOSE
-- ------------------------------
-- NOT VALID skips the one-time scan of existing rows, so this applies cleanly
-- even on the current dirty DB (which still holds date/concept/event leaker
-- rows pending the clean reload). It STILL enforces every new INSERT/UPDATE,
-- which is what immediately stops the validator crash: 'country' is now legal,
-- so Kuwait's UPDATE succeeds.
--
-- After the clean reload (enki-wipe-data.sh) leaves only admittable rows, the
-- reload runbook promotes this to fully enforced with:
--     ALTER TABLE entities VALIDATE CONSTRAINT entities_canonical_type_check;
-- (a cheap scan of the now-clean table). Do NOT VALIDATE before the wipe — it
-- would error on the leaker rows.

ALTER TABLE entities
  DROP CONSTRAINT IF EXISTS entities_canonical_type_check;

ALTER TABLE entities
  ADD CONSTRAINT entities_canonical_type_check
  CHECK (
    canonical_type IS NULL
    OR canonical_type IN (
      -- ── 13 entity subtypes ──
      'account', 'aircraft', 'device', 'facility', 'government_agency',
      'mass_media', 'organization', 'person', 'product', 'unit',
      'vehicle', 'vessel', 'weapon_system',
      -- ── 6 place subtypes (first-class as of migration 118 / pack v0.6.0) ──
      'country', 'city', 'admin_region', 'named_area',
      'maritime_area', 'facility_location'
    )
    -- Tombstones the migrators leave behind keep their legacy type until the
    -- next cleanup sweep; never block on them.
    OR (envelope IS NOT NULL AND envelope ? 'reclassification')
    OR (envelope IS NOT NULL AND envelope ? 'normalized_from')
  )
  NOT VALID;

COMMENT ON CONSTRAINT entities_canonical_type_check ON entities IS
  'Generated from entityvocab.entity_subtypes v0.6.0 (subtype_validator.valid_entity_subtypes()). 13 entity + 6 place subtypes. Keep in sync with the pack; data_integrity.check_constraint_matches_pack() asserts equality at runtime. VALIDATE after a clean reload.';
