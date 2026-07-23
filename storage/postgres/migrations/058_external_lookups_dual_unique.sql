-- 058: dual unique constraints on entity_external_lookups
--
-- The original schema had a single UNIQUE (corpus_id, surface_form_norm, source).
-- After cluster-aware validation (migration 057), the same surface form can
-- have MULTIPLE clusters that resolve to different entities (e.g. "John #1"
-- → King John vs "John #2" → John the Apostle). The old unique key
-- prevents storing both. Replace it with a PARTIAL unique key (only when
-- cluster_id IS NULL), letting the cluster-aware unique key cover the rest.

ALTER TABLE entity_external_lookups
    DROP CONSTRAINT IF EXISTS entity_external_lookups_corpus_id_surface_form_norm_source_key;

CREATE UNIQUE INDEX IF NOT EXISTS idx_external_lookups_surfaceform_legacy
    ON entity_external_lookups(corpus_id, surface_form_norm, source)
    WHERE cluster_id IS NULL;

-- (idx_external_lookups_cluster_unique from migration 057 already covers
-- the cluster_id IS NOT NULL case.)
