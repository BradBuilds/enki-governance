-- Migration 115: hub catalog tables.
--
-- DEPLOYS TO: hub server only (enki_hub database, ENKI_PROFILE=hub).
-- Do NOT apply on node databases — the catalog is hub-side.
--
-- The hub catalog is a CONVENIENCE AGGREGATOR. It lists every known
-- federation_id (corpus) along with the nodes that announce they host
-- it. The hub itself never stores corpus content — only metadata +
-- which nodes have it.
--
-- Resilience model:
--   - Any peer node can fetch from any other peer's
--     /libraries/{N}/manifest.enki directly. They don't NEED the hub.
--   - The hub is fast discovery. If it dies, customers can still
--     reach known peers via cached lists or DNS seeds.
--   - Multiple nodes hosting the same federation_id is the norm, not
--     an exception. The hub tracks all of them — content_sha256
--     dedup means hosts are equivalent on the wire.

CREATE TABLE IF NOT EXISTS hub_catalog_entries (
    federation_id     UUID PRIMARY KEY,
    -- Topic library? Source corpus? Etc. Mirrors libraries.library_subtype.
    library_subtype   TEXT NOT NULL,
    canonical_name    TEXT NOT NULL,
    focal_term        TEXT NOT NULL,
    description       TEXT,
    -- The Ed25519 pubkey of the node that first registered this
    -- federation_id. Subsequent announcers must sign with the same
    -- key (preventing hijack of an existing federation_id by an
    -- unrelated node claiming the same corpus).
    primary_signer_pubkey BYTEA NOT NULL,
    -- Total bytes across the manifest if announced.
    declared_size_bytes BIGINT,
    declared_doc_count  INTEGER,
    -- When a node first told the hub about this federation_id.
    first_announced_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_announced_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Hub admin can mark a corpus as 'flagged' (suspected bad content)
    -- or 'deprecated' (replaced by another federation_id). Not a
    -- censorship tool — peers see the flag, decide for themselves.
    status              TEXT NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active', 'flagged', 'deprecated')),
    notes               TEXT
);

CREATE INDEX IF NOT EXISTS idx_hub_catalog_subtype
    ON hub_catalog_entries (library_subtype);
CREATE INDEX IF NOT EXISTS idx_hub_catalog_last_announced
    ON hub_catalog_entries (last_announced_at DESC);


CREATE TABLE IF NOT EXISTS hub_node_hosting (
    -- A row per (node × federation_id). One node can host many
    -- corpora; one corpus can be hosted by many nodes.
    federation_id     UUID NOT NULL REFERENCES hub_catalog_entries(federation_id)
                                ON DELETE CASCADE,
    node_id           TEXT NOT NULL,
    -- The fully-qualified manifest URL on that node where the corpus
    -- can be fetched. Customers hit this directly — the hub never
    -- proxies content, only points at it.
    manifest_url      TEXT NOT NULL,
    -- Manifest content hash as of the last announce. Lets customers
    -- pick the freshest mirror.
    manifest_sha256   CHAR(64),
    last_announced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Hub can mark a hosting endpoint as 'stale' (no announce in
    -- N days) for UI. Cleanup worker prunes stale rows after a
    -- longer threshold.
    health_status     TEXT NOT NULL DEFAULT 'fresh'
                        CHECK (health_status IN ('fresh', 'stale', 'unreachable')),
    PRIMARY KEY (federation_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_hub_hosting_node
    ON hub_node_hosting (node_id);
CREATE INDEX IF NOT EXISTS idx_hub_hosting_last_announced
    ON hub_node_hosting (last_announced_at DESC);


-- A simple table for tracking hub-level peer-pubkey registrations.
-- A peer must register their Ed25519 pubkey ONCE before they can
-- announce corpora. Hub admin can approve / reject / revoke.
CREATE TABLE IF NOT EXISTS hub_peer_registrations (
    node_id           TEXT PRIMARY KEY,
    pubkey            BYTEA NOT NULL,
    contact_email     TEXT,
    operator_name     TEXT,
    registration_status TEXT NOT NULL DEFAULT 'pending'
                          CHECK (registration_status IN
                                 ('pending', 'approved', 'revoked')),
    registered_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    approved_at       TIMESTAMPTZ,
    approved_by       TEXT
);

CREATE INDEX IF NOT EXISTS idx_hub_peer_status
    ON hub_peer_registrations (registration_status);


COMMENT ON TABLE hub_catalog_entries IS
    'Hub catalog: registry of every known federation_id (corpus). '
    'Hub stores metadata only — never corpus content. Customer nodes '
    'query this catalog for fast discovery, then fetch the actual '
    'corpus directly from any hosting node.';

COMMENT ON TABLE hub_node_hosting IS
    'Which nodes host which corpora. Many-to-many; multiple nodes can '
    'mirror the same federation_id. Hub is the directory, not the '
    'authority — bytes flow peer-to-peer.';

COMMENT ON TABLE hub_peer_registrations IS
    'Peer pubkey registrations. A node must register its Ed25519 pubkey '
    'once (subject to admin approval) before it can announce corpora. '
    'Prevents anonymous federation_id hijacking.';
