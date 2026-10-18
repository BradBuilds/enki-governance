-- 091_node_yield.sql
--
-- Phase 11 of the internal ldu gdu and public network plan.
--
-- yield_score is the non-monetary reputation currency for the
-- public-library subscription network. It shapes:
--   - which libraries a node can subscribe to (yield_gated policy)
--   - governance vote weight (Phase 12)
--   - leaderboard rank
--
-- yield_score is stored on federation_node_trust (one row per node,
-- already exists). yield_history is a rolling JSONB log of recent
-- deltas so the leaderboard can show "last 24h delta" without
-- replaying every contribution.
--
-- node_contributions records every yield-moving event so we can audit
-- and decay.

BEGIN;

-- ── federation_node_trust: yield columns ────────────────────────────────────

ALTER TABLE federation_node_trust
  ADD COLUMN IF NOT EXISTS yield_score REAL NOT NULL DEFAULT 0.0;

ALTER TABLE federation_node_trust
  ADD COLUMN IF NOT EXISTS yield_history JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE federation_node_trust
  ADD COLUMN IF NOT EXISTS last_yield_update_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_fnt_yield_score
  ON federation_node_trust (yield_score DESC);


-- ── node_contributions ──────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS node_contributions (
  contribution_id  BIGSERIAL PRIMARY KEY,
  node_id          VARCHAR(128) NOT NULL,
  -- contribution_type values (extensible):
  --   doc_processed_consensus   +1.0
  --   doc_processed_disputed_won +2.0
  --   doc_processed_disputed_lost -3.0
  --   entity_discovered_corroborated +5.0
  --   inactivity_decay          (negative, applied by decay worker)
  contribution_type VARCHAR(64) NOT NULL,
  -- For library work: target_id = assignment_id. For discovery:
  -- target_id = entity_id. The schema is intentionally narrow — the
  -- contribution_type tells you which table target_id refers to.
  target_id        BIGINT,
  weight           REAL NOT NULL,
  verified_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  metadata         JSONB
);

CREATE INDEX IF NOT EXISTS idx_node_contrib_node_time
  ON node_contributions (node_id, verified_at DESC);

CREATE INDEX IF NOT EXISTS idx_node_contrib_type
  ON node_contributions (contribution_type, verified_at DESC);

-- A node should not be credited twice for the same assignment.
CREATE UNIQUE INDEX IF NOT EXISTS uq_node_contrib_assignment
  ON node_contributions (node_id, target_id, contribution_type)
  WHERE contribution_type IN
    ('doc_processed_consensus', 'doc_processed_disputed_won',
     'doc_processed_disputed_lost');

COMMIT;
