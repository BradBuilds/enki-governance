-- 084_federation_topics.sql
--
-- Federation Phase 1 — topic publish/subscribe between nodes and the hub.
--
-- Same migration applies to BOTH the hub (where federated_topics lives)
-- and the node (where federation_publications tracks what we've sent).
-- Tables are gated by CREATE TABLE IF NOT EXISTS so it's safe in both
-- environments — each side just has the table relevant to its role.
--
-- Design:
--   * The canonical_topic_key is a SHA256(normalized_name + '|' + subtype)
--     so two nodes publishing the same topic (regardless of local IDs)
--     produce the same key. The hub recognizes them as the same topic.
--   * publishing_nodes JSONB tracks every node that publishes this topic,
--     with its node_id, local_topic_id, base60, and member_count.
--   * network_member_count is the aggregated sum across nodes.
--   * pack_versions_seen tracks which versions each contributor was on.
--
-- Phase 1 publishes IDENTITY only — canonical name, subtype, member count.
-- No membership rows transit the federation in this phase. Phase 2 adds
-- selective membership sharing with redaction policies.

BEGIN;

-- ─── Hub-side: the global topic registry ──────────────────────────────────
-- (created on every node too, but only populated on the hub)
CREATE TABLE IF NOT EXISTS federated_topics (
  federation_id          BIGSERIAL PRIMARY KEY,
  canonical_topic_key    TEXT NOT NULL,
  canonical_name         TEXT NOT NULL,
  subtype                VARCHAR(32),
  publishing_nodes       JSONB NOT NULL DEFAULT '[]'::jsonb,
  network_member_count   INTEGER NOT NULL DEFAULT 0,
  pack_versions_seen     JSONB NOT NULL DEFAULT '{}'::jsonb,
  first_published_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (canonical_topic_key)
);

CREATE INDEX IF NOT EXISTS idx_fedtopic_subtype
  ON federated_topics (subtype, network_member_count DESC);
CREATE INDEX IF NOT EXISTS idx_fedtopic_updated
  ON federated_topics (last_updated_at DESC);

COMMENT ON TABLE federated_topics IS
  'Hub-side topic registry. Canonical identity of every topic any node '
  'in the network has published. publishing_nodes JSONB tracks who knows '
  'about each topic + their local view (member_count, base60).';
COMMENT ON COLUMN federated_topics.canonical_topic_key IS
  'SHA256(lower(normalized_name) || ''|'' || subtype). Same canonical key '
  'across nodes means same real-world topic.';

-- ─── Node-side: track what this node has published ─────────────────────────
-- (created on every server but only meaningful on nodes; the hub never
--  writes here because the hub itself doesn't publish — it RECEIVES.)
CREATE TABLE IF NOT EXISTS federation_publications (
  publication_id         BIGSERIAL PRIMARY KEY,
  tenant_id              VARCHAR(64) NOT NULL DEFAULT 'default',
  local_topic_id         BIGINT NOT NULL,
  canonical_topic_key    TEXT NOT NULL,
  canonical_name         TEXT NOT NULL,
  subtype                VARCHAR(32),
  base60_address         VARCHAR(16),
  member_count_at_publish INTEGER NOT NULL DEFAULT 0,
  hub_url                TEXT,
  status                 VARCHAR(32) NOT NULL DEFAULT 'pending',  -- pending | published | failed | skipped
  attempt_count          INTEGER NOT NULL DEFAULT 0,
  last_error             TEXT,
  first_published_at     TIMESTAMPTZ,
  last_published_at      TIMESTAMPTZ,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, local_topic_id, hub_url)
);

CREATE INDEX IF NOT EXISTS idx_fedpub_status
  ON federation_publications (status, updated_at);
CREATE INDEX IF NOT EXISTS idx_fedpub_topic
  ON federation_publications (canonical_topic_key);

COMMENT ON TABLE federation_publications IS
  'Per-node ledger of which local topics have been published to which hub '
  'URL. Lets the publisher worker know what to push, retry failures, and '
  'avoid duplicate publication.';

COMMIT;
