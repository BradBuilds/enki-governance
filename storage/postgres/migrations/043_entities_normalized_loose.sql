-- Migration 043: entities.normalized_name_loose for variant collapse
--
-- The May 4 audit found "Dana Vale", "Dana j. Vale", "Dana james Vale",
-- and "Vale" all created as separate entities — 7 Vale rows splitting 3,254
-- signals. Root cause: core/gdu/entity_resolution.py used a duplicate
-- normalizer that didn't strip person suffixes (Jr/Sr/II/III) and treated
-- middle initials as significant.
--
-- Migration 043 + the entity_resolution.py change route both writers through
-- the pack-driven normalize.py (one source of truth for strict normalization),
-- AND add a loose form that strips middle initials. The resolver uses the
-- loose form as a layer-2.5 fallback: if strict misses but loose hits, mark
-- the new mention as `review` (not auto-merge) — different middle initials
-- might be different people, requiring identifier verification.
--
-- normalized_name      = strict pack normalization (no initials stripped)
-- normalized_name_loose = strict + standalone middle-initial strip
--                         e.g. "Dana j. Vale" → "Dana Vale"
--                              "vicki a. roe" → "vicki roe"
--
-- Indexed but NOT unique (variants intentionally share loose forms).

ALTER TABLE entities
    ADD COLUMN IF NOT EXISTS normalized_name_loose TEXT;

CREATE INDEX IF NOT EXISTS idx_entities_normalized_loose
    ON entities (tenant_id, canonical_type, normalized_name_loose)
    WHERE normalized_name_loose IS NOT NULL
      AND normalized_name_loose <> ''
      AND gdu_canonical = TRUE;

-- Backfill: existing rows get their loose form computed lazily by the
-- resolver on next touch (signal_count increment) — too expensive to do
-- in a single ALTER on a large entities table. The resolver will populate
-- normalized_name_loose whenever it touches a row whose value is NULL.
