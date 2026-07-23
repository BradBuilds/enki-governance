-- 092_governance_votes.sql
--
-- Phase 12 of the internal ldu gdu and public network plan.
--
-- Network-wide governance proposals + yield-weighted votes. Nodes vote
-- on proposals; their effective weight is yield_score (capped above
-- some floor so any registered node has a small voice).
--
-- Out of scope here: proposal authoring UI (admin/federation/yield
-- tab) — the autonomous runs only put the substrate in place.

BEGIN;

CREATE TABLE IF NOT EXISTS governance_proposals (
  proposal_id    BIGSERIAL PRIMARY KEY,
  proposer_node  VARCHAR(128) NOT NULL,
  title          TEXT NOT NULL,
  body           TEXT,
  -- e.g., "policy_change", "pack_version_pin", "library_unlist",
  --       "node_sanction", "deploy_directive", ...
  category       VARCHAR(64) NOT NULL DEFAULT 'policy_change',
  -- open | passed | failed | withdrawn
  status         VARCHAR(32) NOT NULL DEFAULT 'open',
  opens_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  closes_at      TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '7 days',
  resolved_at    TIMESTAMPTZ,
  -- Threshold to pass — fraction of cast yield weight, 0..1.
  pass_threshold REAL NOT NULL DEFAULT 0.5,
  metadata       JSONB
);

CREATE INDEX IF NOT EXISTS idx_gov_prop_status
  ON governance_proposals (status, closes_at);

CREATE TABLE IF NOT EXISTS governance_votes (
  vote_id      BIGSERIAL PRIMARY KEY,
  proposal_id  BIGINT NOT NULL REFERENCES governance_proposals(proposal_id)
                 ON DELETE CASCADE,
  node_id      VARCHAR(128) NOT NULL,
  -- yes | no | abstain
  ballot       VARCHAR(16) NOT NULL CHECK (ballot IN ('yes','no','abstain')),
  -- Weight stamped at vote-cast time = node's yield_score at the
  -- moment. Captured so a yield decay after voting doesn't
  -- retroactively change a tallied result.
  effective_weight REAL NOT NULL DEFAULT 0.0,
  cast_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  signature    TEXT,
  notes        TEXT,
  UNIQUE (proposal_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_gov_votes_prop
  ON governance_votes (proposal_id, ballot);

COMMIT;
