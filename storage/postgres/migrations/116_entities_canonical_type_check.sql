-- 116_entities_canonical_type_check.sql
--
-- Storage-layer hard reject for non-entity canonical_type on entities.
-- Final layer of the four-layer LDU lockdown (after the write-time
-- validator in admit_candidate, the trigger trg_enforce_entities_governance,
-- and the continuous cleanup workers).
--
-- Excludes rows already marked as reclassified or normalized — those
-- are tombstones the migrator left behind on purpose and can keep their
-- old canonical_type until the next cleanup sweep.
--
-- Applied with NOT VALID so the existing population (which may still
-- have envelope-marked legacy rows) is not re-scanned at deploy time.

ALTER TABLE entities
  ADD CONSTRAINT entities_canonical_type_check
  CHECK (
    canonical_type IS NULL
    OR canonical_type IN (
      'account', 'aircraft', 'device', 'facility', 'government_agency',
      'mass_media', 'organization', 'person', 'product', 'unit',
      'vehicle', 'vessel', 'weapon_system'
    )
    OR (envelope IS NOT NULL AND envelope ? 'reclassification')
    OR (envelope IS NOT NULL AND envelope ? 'normalized_from')
  )
  NOT VALID;
