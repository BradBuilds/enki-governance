-- 083_entity_fission.sql
--
-- The fission operator's persistence layer.
--
-- Mirror of entity_redirects (which records collapse/merge), this records
-- the inverse: when one contaminated entity is split into multiple
-- distinct entities, the operation is audited here.
--
-- Plus a queue table for proposals that don't meet auto-apply thresholds
-- and need human review.
--
-- Auto-apply policy (conservative):
--   partition_score >= 0.95 AND signal_count >= 2 AND first_name_conflict
-- Everything else lands in fission_proposals.

BEGIN;

-- ─── Audit log: every fission that has ever been applied ──────────────────
CREATE TABLE IF NOT EXISTS entity_fissions (
  split_id          BIGSERIAL PRIMARY KEY,
  tenant_id         VARCHAR(64) NOT NULL DEFAULT 'default',
  old_entity_id     BIGINT NOT NULL,        -- the original conflated entity (kept post-fission)
  new_entity_ids    BIGINT[] NOT NULL,      -- entities created by the split (does NOT include old_entity_id)
  reason            TEXT NOT NULL,
  partition_score   REAL NOT NULL,          -- 0.0..1.0; how cleanly the partition separated
  signals_fired     TEXT[] NOT NULL DEFAULT '{}',  -- which detection signals fired ('alias_divergence', 'document_partition', ...)
  evidence          JSONB NOT NULL DEFAULT '{}'::jsonb,  -- per-entity which docs/mentions/assessments were assigned
  applied_by        VARCHAR(64) NOT NULL DEFAULT 'fission_detector',
  applied_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fissions_old ON entity_fissions (old_entity_id);
CREATE INDEX IF NOT EXISTS idx_fissions_new ON entity_fissions USING gin (new_entity_ids);
CREATE INDEX IF NOT EXISTS idx_fissions_applied ON entity_fissions (applied_at DESC);

COMMENT ON TABLE entity_fissions IS
  'Audit log of fission operations — the inverse of entity_redirects. '
  'When an entity that conflated two real-world identities is split, '
  'this records WHAT was split, WHY (which signals fired), and HOW '
  '(which mentions/assessments went to which side).';
COMMENT ON COLUMN entity_fissions.partition_score IS
  'How cleanly the partition separated the original entity. 1.0 = '
  'completely disjoint source clusters (the Acme case). 0.5 = '
  'partition is murky, manual review preferred. Auto-apply threshold '
  'is 0.95.';

-- ─── Review queue: proposals that need a human ─────────────────────────────
CREATE TABLE IF NOT EXISTS fission_proposals (
  proposal_id       BIGSERIAL PRIMARY KEY,
  tenant_id         VARCHAR(64) NOT NULL DEFAULT 'default',
  entity_id         BIGINT NOT NULL,
  partition_score   REAL NOT NULL,
  signals_fired     TEXT[] NOT NULL DEFAULT '{}',
  proposed_split    JSONB NOT NULL,        -- {"cluster_a": {...}, "cluster_b": {...}}
  status            VARCHAR(32) NOT NULL DEFAULT 'pending',  -- pending | applied | rejected | superseded
  reviewer          VARCHAR(255),
  reviewer_note     TEXT,
  detected_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  reviewed_at       TIMESTAMPTZ,
  applied_fission_id BIGINT REFERENCES entity_fissions(split_id)
);

CREATE INDEX IF NOT EXISTS idx_proposals_entity ON fission_proposals (entity_id);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON fission_proposals (status, partition_score DESC);
CREATE INDEX IF NOT EXISTS idx_proposals_detected ON fission_proposals (detected_at DESC);

COMMENT ON TABLE fission_proposals IS
  'Pending fission proposals that did not meet auto-apply thresholds. '
  'A human reviews each via /governance UI, accepts/rejects, and the '
  'accepted ones become entity_fissions rows.';

-- ─── Helper view: undisputed fissions in the last 24h ─────────────────────
CREATE OR REPLACE VIEW recent_fissions AS
SELECT s.split_id, s.old_entity_id,
       array_length(s.new_entity_ids, 1) AS n_new,
       s.partition_score, s.reason, s.applied_at,
       (SELECT canonical_name FROM entities WHERE entity_id = s.old_entity_id) AS old_name
  FROM entity_fissions s
 WHERE s.applied_at > NOW() - INTERVAL '24 hours'
 ORDER BY s.applied_at DESC;

COMMIT;
