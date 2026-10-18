-- 085_federation_entities.sql
--
-- Federation Phase 2 — cross-node ENTITY resolution.
--
-- Mirror of 084 (topic federation) applied to entities. Same canonical-
-- key pattern: sha256(lower(normalized_name) + '|' + entity_type) is
-- deterministic across nodes, so two nodes that admit the same real-world
-- person/org/place produce the same canonical_entity_key.
--
-- This is the pillar that makes Phase 2 membership sharing compose. Once
-- two nodes share canonical entity identity, they can share topic
-- memberships meaningfully: Node A says "topic X includes
-- canonical_entity_key Y", Node B looks up Y in its own federated_entities,
-- finds its local entity_id, and renders the membership locally.

BEGIN;

-- ─── Hub-side: the global entity registry ──────────────────────────────────
CREATE TABLE IF NOT EXISTS federated_entities (
  federation_id          BIGSERIAL PRIMARY KEY,
  canonical_entity_key   TEXT NOT NULL,
  canonical_name         TEXT NOT NULL,
  canonical_type         VARCHAR(64),
  publishing_nodes       JSONB NOT NULL DEFAULT '[]'::jsonb,
  network_signal_count   BIGINT NOT NULL DEFAULT 0,
  pack_versions_seen     JSONB NOT NULL DEFAULT '{}'::jsonb,
  first_published_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (canonical_entity_key)
);

CREATE INDEX IF NOT EXISTS idx_fedentity_type
  ON federated_entities (canonical_type, network_signal_count DESC);
CREATE INDEX IF NOT EXISTS idx_fedentity_updated
  ON federated_entities (last_updated_at DESC);

COMMENT ON TABLE federated_entities IS
  'Hub-side entity registry. canonical_entity_key = sha256(lower(name) || ''|'' || type). '
  'Same key across nodes means same real-world entity. publishing_nodes JSONB '
  'tracks each contributor''s local entity_id, base60_address, signal_count.';

-- ─── Node-side: ledger of published entities ───────────────────────────────
CREATE TABLE IF NOT EXISTS federation_entity_publications (
  publication_id         BIGSERIAL PRIMARY KEY,
  tenant_id              VARCHAR(64) NOT NULL DEFAULT 'default',
  local_entity_id        BIGINT NOT NULL,
  canonical_entity_key   TEXT NOT NULL,
  canonical_name         TEXT NOT NULL,
  canonical_type         VARCHAR(64),
  base60_address         VARCHAR(16),
  signal_count_at_publish BIGINT NOT NULL DEFAULT 0,
  hub_url                TEXT,
  status                 VARCHAR(32) NOT NULL DEFAULT 'pending',
  attempt_count          INTEGER NOT NULL DEFAULT 0,
  last_error             TEXT,
  first_published_at     TIMESTAMPTZ,
  last_published_at      TIMESTAMPTZ,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, local_entity_id, hub_url)
);

CREATE INDEX IF NOT EXISTS idx_fedentpub_status
  ON federation_entity_publications (status, updated_at);
CREATE INDEX IF NOT EXISTS idx_fedentpub_canonical
  ON federation_entity_publications (canonical_entity_key);

COMMENT ON TABLE federation_entity_publications IS
  'Per-node ledger of which local entities have been published to which '
  'hub URL. Lets the publisher know what to push, retry failures, and '
  'avoid duplicate publication. Entities only get published once they '
  'have signal_count >= 5 to keep the federation free of long-tail noise.';

COMMIT;
