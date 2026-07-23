-- 087_federation_trust_redirects_conflicts.sql
--
-- Federation Phase 4 — three orthogonal capabilities the network needs:
--   (a) Trust scoring — per-node reputation that weights aggregation
--   (b) Redirect propagation — fission/merge decisions sync across nodes
--   (c) Conflict broadcast — alternatives propagate so contested claims
--       are visible network-wide
--
-- All additive. Empty on first deploy; populates as the network operates.

BEGIN;

-- ─── (a) Trust scoring ────────────────────────────────────────────────────
-- Hub maintains per-node reputation. Each fact a node publishes gets one
-- of three lifecycle outcomes when later evidence arrives:
--   corroborated  — another node confirms the same canonical assertion
--   contradicted  — another node asserts the alternative
--   unchanged     — no new evidence either way (default)
-- Trust score = (corroborations - contradictions) / total, bounded [0, 1],
-- defaulting to 0.5 for new nodes (innocent prior).

CREATE TABLE IF NOT EXISTS federation_node_trust (
  node_id                VARCHAR(128) PRIMARY KEY,
  trust_score            REAL NOT NULL DEFAULT 0.5,
  total_publications     INTEGER NOT NULL DEFAULT 0,
  corroboration_count    INTEGER NOT NULL DEFAULT 0,
  contradiction_count    INTEGER NOT NULL DEFAULT 0,
  first_seen_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  notes                  TEXT
);

CREATE INDEX IF NOT EXISTS idx_node_trust_score
  ON federation_node_trust (trust_score DESC);

COMMENT ON TABLE federation_node_trust IS
  'Hub-maintained per-node reputation. Trust_score weights aggregation '
  'so high-reputation nodes carry more signal than new or low-trust ones. '
  'Trust starts at 0.5 (innocent prior) and floats with corroborations.';

-- ─── (b) Cross-node redirect propagation ──────────────────────────────────
-- When Node A fissions/rebinds an entity (e.g. Acme 4428 → 2460), the
-- decision propagates to the hub. Other nodes whose locally-different
-- entity_id maps to the SAME canonical_entity_key can subscribe to the
-- redirect feed and auto-rebind their own substrate.

CREATE TABLE IF NOT EXISTS federation_entity_redirects (
  redirect_id            BIGSERIAL PRIMARY KEY,
  origin_canonical_key   TEXT NOT NULL,          -- key of the now-deprecated entity
  target_canonical_key   TEXT NOT NULL,          -- key of the canonical survivor
  reason                 TEXT NOT NULL,          -- 'fission_rebind' | 'manual_merge' | …
  originating_node_id    VARCHAR(128) NOT NULL,  -- which node decided this
  applied_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  audit_payload          JSONB NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (origin_canonical_key, target_canonical_key)
);

CREATE INDEX IF NOT EXISTS idx_fed_redirect_origin
  ON federation_entity_redirects (origin_canonical_key);
CREATE INDEX IF NOT EXISTS idx_fed_redirect_target
  ON federation_entity_redirects (target_canonical_key);
CREATE INDEX IF NOT EXISTS idx_fed_redirect_applied
  ON federation_entity_redirects (applied_at DESC);

COMMENT ON TABLE federation_entity_redirects IS
  'Hub-side log of cross-node entity redirects. When Node A fissions or '
  'rebinds an entity, this row is created. Other nodes poll this table '
  'and apply the same redirect to their local substrate.';

-- ─── (c) Conflict broadcast ────────────────────────────────────────────────
-- When a node''s collapse operator fires ACTION_ALTERNATIVES (different
-- values in the same lattice), it can publish the conflict so the network
-- knows both sides. Federation aggregates: same (node, property,
-- value_a, value_b) from multiple nodes accumulates contributing_nodes.

CREATE TABLE IF NOT EXISTS federation_conflicts (
  conflict_id            BIGSERIAL PRIMARY KEY,
  canonical_entity_key   TEXT NOT NULL,
  property_key           VARCHAR(64) NOT NULL,
  value_a                TEXT NOT NULL,
  value_b                TEXT NOT NULL,
  contributing_nodes     JSONB NOT NULL DEFAULT '[]'::jsonb,  -- [{node_id, side, last_seen}]
  first_seen_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (canonical_entity_key, property_key, value_a, value_b)
);

CREATE INDEX IF NOT EXISTS idx_fed_conflict_entity
  ON federation_conflicts (canonical_entity_key);
CREATE INDEX IF NOT EXISTS idx_fed_conflict_property
  ON federation_conflicts (property_key, last_updated_at DESC);

COMMENT ON TABLE federation_conflicts IS
  'Network-visible contradictions. When a node sees "X has color=red" '
  'and "X has color=blue", it can broadcast the conflict here. Other '
  'nodes pull the conflicts feed to learn what the network considers '
  'contested. UI shows contested badge based on this.';

COMMIT;
