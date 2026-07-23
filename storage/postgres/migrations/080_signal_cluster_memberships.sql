-- 080_signal_cluster_memberships.sql
-- Fix: signals is append-only via prevent_mutation trigger, so we can't
-- UPDATE signals.cluster_id. Drop that column and use a separate
-- membership table so clustering can be done without touching the
-- audit-protected signals table.

ALTER TABLE signals DROP COLUMN IF EXISTS cluster_id;

CREATE TABLE IF NOT EXISTS signal_cluster_memberships (
    signal_rid  BIGINT  PRIMARY KEY REFERENCES signals(rid),
    cluster_id  BIGINT  NOT NULL REFERENCES signal_clusters(cluster_id) ON DELETE CASCADE,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_scm_cluster ON signal_cluster_memberships (cluster_id);
