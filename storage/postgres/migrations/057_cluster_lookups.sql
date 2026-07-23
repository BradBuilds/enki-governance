-- 057: Cluster-aware external lookups
--
-- Phase 2.5c shipped entity_external_lookups keyed on (corpus, surface
-- form, source). After context-bag clustering (migration 056) we have
-- multiple clusters per surface form when paragraph contexts diverge,
-- so the lookup also needs to be per-cluster (different clusters of
-- "John" can resolve to different Wikipedia entities).
--
-- This migration adds cluster_id to entity_external_lookups so the
-- cluster-aware validator can store per-cluster routings without
-- replacing the surface-form-level rows that the corroboration view
-- already depends on.

ALTER TABLE entity_external_lookups
    ADD COLUMN IF NOT EXISTS cluster_id BIGINT
        REFERENCES document_mention_clusters(cluster_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_external_lookups_cluster
    ON entity_external_lookups(cluster_id)
    WHERE cluster_id IS NOT NULL;

-- Make the per-cluster route distinct from the per-surface-form route.
-- The original unique key was (corpus_id, surface_form_norm, source),
-- which means only one Wikipedia lookup per surface form globally.
-- We need (corpus_id, cluster_id, source) to allow distinct lookups
-- when clusters of the same form resolve differently.
--
-- We DON'T drop the old unique key — surface-form-level rows still get
-- written by the legacy validate_corpus path. Instead we add a new
-- partial unique index for the cluster-aware rows.

CREATE UNIQUE INDEX IF NOT EXISTS idx_external_lookups_cluster_unique
    ON entity_external_lookups(corpus_id, cluster_id, source)
    WHERE cluster_id IS NOT NULL;
