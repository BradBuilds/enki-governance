-- 094_enki_content_addressing.sql
--
-- Phase 7 of the multi-hub / P2P plan.
--
-- Adds content-addressing + .enki on-disk paths to documents so every
-- ingested artifact has a verifiable SHA-256 identity and a portable
-- .enki file representation.
--
-- Also adds network-mode controls so each operator picks isolated /
-- networked / hybrid mode, and each library opts into network sharing
-- independently.

BEGIN;

-- ── documents content addressing ─────────────────────────────────────────

ALTER TABLE documents
  ADD COLUMN IF NOT EXISTS content_sha256 CHAR(64);

ALTER TABLE documents
  ADD COLUMN IF NOT EXISTS enki_path TEXT;

ALTER TABLE documents
  ADD COLUMN IF NOT EXISTS enki_signed_at TIMESTAMPTZ;

ALTER TABLE documents
  ADD COLUMN IF NOT EXISTS enki_signer_pubkey CHAR(64);

-- Fast lookup: "who has content X?" indexed on content_sha256.
CREATE INDEX IF NOT EXISTS idx_documents_content_sha
  ON documents (content_sha256) WHERE content_sha256 IS NOT NULL;


-- ── entity_documents content addressing (mirror columns) ─────────────────
--
-- entity_documents is a legacy CRM table (filed docs per contact/asset).
-- Some dev installs predate schema_v2.sql and don't have it. The signal
-- pipeline doesn't depend on these columns — wrap in a guard so the rest
-- of the migration can proceed.

DO $$
BEGIN
  IF to_regclass('public.entity_documents') IS NOT NULL THEN
    EXECUTE 'ALTER TABLE entity_documents ADD COLUMN IF NOT EXISTS content_sha256 CHAR(64)';
    EXECUTE 'ALTER TABLE entity_documents ADD COLUMN IF NOT EXISTS enki_path TEXT';
    EXECUTE 'ALTER TABLE entity_documents ADD COLUMN IF NOT EXISTS enki_signed_at TIMESTAMPTZ';
    EXECUTE 'ALTER TABLE entity_documents ADD COLUMN IF NOT EXISTS enki_signer_pubkey CHAR(64)';
    EXECUTE 'CREATE INDEX IF NOT EXISTS idx_entity_documents_content_sha
             ON entity_documents (content_sha256) WHERE content_sha256 IS NOT NULL';
  END IF;
END $$;


-- ── per-library opt-in to network publication ────────────────────────────
--
-- This is independent of is_public + subscription_policy. Even a public
-- library can be local-only on a specific node (the node holds it,
-- subscribes other nodes don't see it via that hub). Use case: an
-- operator who runs their own private hub but has the same library
-- catalog locally.

ALTER TABLE libraries
  ADD COLUMN IF NOT EXISTS is_published_to_network BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_libraries_published_network
  ON libraries (is_published_to_network) WHERE is_published_to_network = TRUE;


-- ── node-wide federation mode ────────────────────────────────────────────
--
-- isolated : never talks to hub or peers. Air-gap capable.
--            ingest from local files / USB / bundles only.
-- networked: heartbeat + content advertise + serve P2P + work assignment.
-- hybrid   : per-library decision via libraries.is_published_to_network.
--            DEFAULT — most operators want this.
--
-- Distinct from setup_config.network_mode which is the
-- setup-wizard online/offline/airgap choice. ENKI_NETWORK_MODE env
-- var can override.

ALTER TABLE setup_config
  ADD COLUMN IF NOT EXISTS federation_mode VARCHAR(32) NOT NULL DEFAULT 'hybrid'
    CHECK (federation_mode IN ('isolated', 'networked', 'hybrid'));


-- ── content_advertisements: who-has-what for the network ─────────────────
--
-- Each node periodically tells its hub what content_sha256 values it
-- holds. The hub aggregates this so any subscriber can look up "who
-- has hash X?" in one query. Hub-side table only.

CREATE TABLE IF NOT EXISTS content_advertisements (
  ad_id              BIGSERIAL PRIMARY KEY,
  node_id            VARCHAR(128) NOT NULL,
  content_sha256     CHAR(64) NOT NULL,
  library_id         BIGINT REFERENCES libraries(library_id) ON DELETE SET NULL,
  -- last time the advertising node confirmed it still has this content.
  last_advertised_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  -- size hint so receivers can budget bandwidth.
  size_bytes         BIGINT,
  -- if the node is willing to act as a full-mirror seed for the
  -- library (high bandwidth, high uptime), this flag indicates so.
  is_full_mirror     BOOLEAN NOT NULL DEFAULT FALSE,
  UNIQUE (node_id, content_sha256)
);

CREATE INDEX IF NOT EXISTS idx_content_ads_lookup
  ON content_advertisements (content_sha256, last_advertised_at DESC);

CREATE INDEX IF NOT EXISTS idx_content_ads_node
  ON content_advertisements (node_id, library_id);

CREATE INDEX IF NOT EXISTS idx_content_ads_mirrors
  ON content_advertisements (library_id)
  WHERE is_full_mirror = TRUE;


-- ── known_peers: trackerless fallback cache ──────────────────────────────
--
-- Each node maintains a cache of peers it has talked to. If the hub
-- becomes unreachable, the node uses this to gossip-discover content
-- (ask 3 known peers for X, each asks 3 more).

CREATE TABLE IF NOT EXISTS known_peers (
  peer_node_id      VARCHAR(128) PRIMARY KEY,
  peer_url          TEXT NOT NULL,
  peer_pubkey       CHAR(64),
  first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  -- how we learned about this peer
  discovered_via    VARCHAR(32) NOT NULL DEFAULT 'hub'
    CHECK (discovered_via IN ('hub', 'gossip', 'manual', 'bootstrap_list')),
  -- did the last reach attempt succeed
  is_reachable      BOOLEAN NOT NULL DEFAULT TRUE,
  failed_attempts   INT NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_known_peers_reachable
  ON known_peers (last_seen_at DESC) WHERE is_reachable = TRUE;


-- ── library_source_connectors: pluggable source feeds per library ────────
--
-- Each library can ingest from multiple sources. The 'connector_type'
-- selects the parser (rss / sec_edgar / usaspending / gdelt / etc.),
-- and 'config' holds the per-connector parameters (RSS URL, query
-- terms, API key reference, etc.).

CREATE TABLE IF NOT EXISTS library_source_connectors (
  connector_id           BIGSERIAL PRIMARY KEY,
  library_id             BIGINT NOT NULL REFERENCES libraries(library_id)
                           ON DELETE CASCADE,
  connector_type         VARCHAR(64) NOT NULL,
  display_name           TEXT NOT NULL,
  config                 JSONB NOT NULL DEFAULT '{}'::jsonb,
  enabled                BOOLEAN NOT NULL DEFAULT TRUE,
  -- politeness
  refresh_cadence        VARCHAR(32) NOT NULL DEFAULT 'daily',
  last_run_at            TIMESTAMPTZ,
  last_run_status        VARCHAR(32),
  last_run_message       TEXT,
  -- counters
  documents_ingested     BIGINT NOT NULL DEFAULT 0,
  errors_count           BIGINT NOT NULL DEFAULT 0,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (library_id, connector_type, display_name)
);

CREATE INDEX IF NOT EXISTS idx_lsc_enabled
  ON library_source_connectors (enabled, refresh_cadence, last_run_at);

COMMIT;
