-- 056: Document mention clusters — the multi-signal resolver substrate
--
-- Mentions with the SAME surface_form_norm don't always refer to the
-- same real-world entity. "Doe" in an Acme paragraph and "Doe"
-- in a Roswell paragraph are different Does, even though the surface
-- form is identical.
--
-- This migration adds the structure to cluster mentions by CONTEXT — the
-- set of other surface forms that appear in the same paragraph. Mentions
-- of "Doe" that share co-occurring forms ({Acme, John, Springfield})
-- are clustered into ONE entity-candidate; mentions sharing different
-- co-occurring forms ({Roswell, Wright-Patterson, Project SIGN}) form a
-- separate cluster.
--
-- The resolver routes EACH CLUSTER (not each surface form globally)
-- through internal-node and Wikipedia lookups. The cluster's
-- context_keywords pass as disambiguation evidence.
--
-- See:
--   the internal refdata hierarchical collapse plan

CREATE TABLE IF NOT EXISTS document_mention_clusters (
    cluster_id            BIGSERIAL PRIMARY KEY,
    tenant_id             TEXT NOT NULL DEFAULT 'default',
    corpus_id             TEXT NOT NULL,
    surface_form_norm     TEXT NOT NULL,
    representative_form   TEXT,                    -- as written, sample
    mention_count         INT NOT NULL,
    document_count        INT NOT NULL,
    context_keywords      TEXT[],                  -- top co-occurring surface forms
    representative_mention_id BIGINT,              -- one mention to anchor the cluster
    suggested_type        TEXT,                    -- modal suggested_type across members
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (corpus_id, surface_form_norm, cluster_id)
);

CREATE INDEX IF NOT EXISTS idx_mc_corpus ON document_mention_clusters(corpus_id);
CREATE INDEX IF NOT EXISTS idx_mc_form ON document_mention_clusters(corpus_id, surface_form_norm);

-- Each mention belongs to AT MOST one cluster. NULL means "not yet
-- clustered" — the resolver should re-run clustering before validating.
ALTER TABLE document_mentions
    ADD COLUMN IF NOT EXISTS cluster_id BIGINT
        REFERENCES document_mention_clusters(cluster_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_mention_cluster
    ON document_mentions(cluster_id)
    WHERE cluster_id IS NOT NULL;
