-- 086_federation_memberships.sql
--
-- Federation Phase 3 — selective topic membership sharing across the network.
--
-- Phase 1 federated topic identity. Phase 2 federated entity identity.
-- Phase 3 closes the loop: a topic's members can now be referenced
-- BY CANONICAL ENTITY KEY across nodes, so the network gets a true
-- "who's in this topic" answer instead of just "this topic exists".
--
-- Redaction is first-class. Every topic carries a publish_membership_policy:
--   none           → identity only (Phase 1 default; members stay local)
--   identity_only  → alias for none
--   anonymized     → publish member COUNTS by entity_type, not member identities
--   full           → publish member canonical_entity_keys to the network
--
-- A LEA / corporate ORC node can publish its investigation topic's
-- IDENTITY (so the network knows the case exists) while keeping its
-- membership private. Or share anonymized counts ("this investigation
-- has 47 persons, 12 organizations, 3 facilities") without naming names.

BEGIN;

-- ─── Topic redaction policy on the topics table ────────────────────────────
ALTER TABLE topics
  ADD COLUMN IF NOT EXISTS publish_membership_policy VARCHAR(32)
    NOT NULL DEFAULT 'none';

COMMENT ON COLUMN topics.publish_membership_policy IS
  'How this topic shares membership with the federation: '
  'none | identity_only | anonymized | full. Default none — '
  'opt-in to share members. LEA / sensitive cases stay private by default.';

-- ─── Hub-side: aggregated topic members across nodes ──────────────────────
CREATE TABLE IF NOT EXISTS federated_topic_members (
  membership_id          BIGSERIAL PRIMARY KEY,
  canonical_topic_key    TEXT NOT NULL,
  canonical_entity_key   TEXT NOT NULL,
  contributing_nodes     JSONB NOT NULL DEFAULT '[]'::jsonb,  -- [{node_id, score, last_seen}]
  aggregate_score        REAL NOT NULL DEFAULT 0,
  first_seen_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (canonical_topic_key, canonical_entity_key)
);

CREATE INDEX IF NOT EXISTS idx_fedmember_topic
  ON federated_topic_members (canonical_topic_key, aggregate_score DESC);
CREATE INDEX IF NOT EXISTS idx_fedmember_entity
  ON federated_topic_members (canonical_entity_key);
CREATE INDEX IF NOT EXISTS idx_fedmember_updated
  ON federated_topic_members (last_updated_at DESC);

COMMENT ON TABLE federated_topic_members IS
  'Hub-side aggregated topic membership. One row per '
  '(canonical_topic_key, canonical_entity_key) pair. contributing_nodes '
  'JSONB tracks each node that asserted this membership, with its '
  'local score. aggregate_score = max or sum of contributors (configurable).';

-- ─── Hub-side: anonymized counts when full members are private ─────────────
-- For 'anonymized' policy: publish per-type counts, not identities.
CREATE TABLE IF NOT EXISTS federated_topic_counts (
  count_id               BIGSERIAL PRIMARY KEY,
  canonical_topic_key    TEXT NOT NULL,
  contributing_node_id   VARCHAR(128) NOT NULL,
  member_type            VARCHAR(64) NOT NULL,
  count                  INTEGER NOT NULL DEFAULT 0,
  last_updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (canonical_topic_key, contributing_node_id, member_type)
);

CREATE INDEX IF NOT EXISTS idx_fedcount_topic
  ON federated_topic_counts (canonical_topic_key);

COMMENT ON TABLE federated_topic_counts IS
  'Anonymized topic membership stats — per-node, per-entity-type counts. '
  'For nodes that want to signal "this topic has membership" without '
  'revealing who. Phase 3 redaction tier.';

-- ─── Node-side: ledger of membership rows we''ve sent ──────────────────────
CREATE TABLE IF NOT EXISTS federation_membership_publications (
  publication_id         BIGSERIAL PRIMARY KEY,
  tenant_id              VARCHAR(64) NOT NULL DEFAULT 'default',
  local_topic_id         BIGINT NOT NULL,
  canonical_topic_key    TEXT NOT NULL,
  policy_at_publish      VARCHAR(32) NOT NULL,    -- snapshot of policy
  hub_url                TEXT,
  member_keys_published  INTEGER NOT NULL DEFAULT 0,
  status                 VARCHAR(32) NOT NULL DEFAULT 'pending',
  attempt_count          INTEGER NOT NULL DEFAULT 0,
  last_error             TEXT,
  first_published_at     TIMESTAMPTZ,
  last_published_at      TIMESTAMPTZ,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, local_topic_id, hub_url)
);

CREATE INDEX IF NOT EXISTS idx_fedmempub_status
  ON federation_membership_publications (status, updated_at);

COMMENT ON TABLE federation_membership_publications IS
  'Per-node ledger of membership publications. Tracks WHICH topic''s '
  'members got pushed to WHICH hub under WHICH policy.';

COMMIT;
