-- Migration 101: library_mirrors — subscriber-side tracking of outgoing
-- library subscriptions.
--
-- library_subscriptions (migration 089) tracks who's subscribed TO this
-- node's libraries (incoming subscriptions, on the publisher side). When
-- a subscriber wants to mirror an upstream library, it needs its own
-- local state — which upstream node + library_id it's tracking, where
-- to fetch from, what's been pulled so far, last seen manifest hash.
--
-- library_mirrors is that state. One row per outgoing subscription.
-- Joined to a LOCAL libraries row that the subscriber creates to host
-- the mirrored content.
--
-- Flow:
--   1. Operator browses /hub/libraries/discover, finds node-A's Acme lib
--   2. Operator clicks Subscribe → POST /libraries/mirror
--   3. Local node creates: a new libraries row + a library_mirrors row +
--      calls upstream's POST /fleet/libraries/{id}/subscribe to register
--      itself as a subscriber on the publisher side
--   4. library_subscriber worker walks library_mirrors WHERE status='active',
--      fetches manifest, pulls missing docs via /p2p/content/{sha},
--      inserts into local documents + binds to local library_members
--   5. Worker updates manifest_hash + last_synced_at after each pass

CREATE TABLE IF NOT EXISTS library_mirrors (
  mirror_id            BIGSERIAL PRIMARY KEY,

  -- The LOCAL library row this mirror populates. Local libraries.canonical_name
  -- typically matches the upstream's; library_subtype='mirror_library'.
  local_library_id     BIGINT NOT NULL REFERENCES libraries(library_id) ON DELETE CASCADE,

  -- Where the mirrored content originates.
  upstream_node_id     VARCHAR(128) NOT NULL,   -- node:abc123... publisher ID
  upstream_library_id  BIGINT NOT NULL,         -- library_id in publisher's namespace
  upstream_url         TEXT NOT NULL,           -- e.g. https://localhost

  -- Last successful sync state. Manifest hash is the publisher's signature
  -- digest; comparing it lets us skip syncs when nothing changed.
  manifest_hash        CHAR(64),
  last_synced_at       TIMESTAMPTZ,
  last_sync_error      TEXT,

  -- Bookkeeping for UI / observability.
  documents_pulled     INTEGER NOT NULL DEFAULT 0,
  documents_failed     INTEGER NOT NULL DEFAULT 0,
  bytes_pulled         BIGINT  NOT NULL DEFAULT 0,

  -- Lifecycle. revoked means we're done with this subscription; unsubscribe
  -- the publisher and stop syncing. paused holds without unsubscribing.
  status               VARCHAR(32) NOT NULL DEFAULT 'active',
  CHECK (status IN ('active', 'paused', 'revoked', 'failed')),

  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  notes                TEXT,

  -- A given upstream library can only be mirrored once per local node.
  UNIQUE (upstream_node_id, upstream_library_id)
);

-- Subscriber worker walks active mirrors. Index needs to support that.
CREATE INDEX IF NOT EXISTS idx_library_mirrors_active
    ON library_mirrors (status, last_synced_at NULLS FIRST)
    WHERE status = 'active';

-- UI on the local library page needs to know if it's a mirror.
CREATE INDEX IF NOT EXISTS idx_library_mirrors_local
    ON library_mirrors (local_library_id);

COMMENT ON TABLE library_mirrors IS
    'Subscriber-side state for outgoing library subscriptions. One row per '
    'mirrored upstream library. The library_subscriber worker walks these '
    'rows on a regular tick to fetch manifests and pull new content.';

COMMENT ON COLUMN library_mirrors.upstream_url IS
    'Base URL of the upstream node. Used to construct '
    '${upstream_url}/libraries/${upstream_library_id}/manifest.enki and '
    '${upstream_url}/p2p/content/sha256:${hash}';

COMMENT ON COLUMN library_mirrors.manifest_hash IS
    'SHA-256 of the last successfully-fetched manifest CBOR. If unchanged, '
    'the subscriber can skip the full manifest diff. Updated only after a '
    'successful pull pass completes.';
