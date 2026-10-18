-- Migration 108: Node-onboarding registry.
--
-- Customer auto-onboarding pipeline (the next chunk of the library
-- federation plan). When a federation_admin issues an onboarding
-- token via /hub/admin/nodes/onboard, the hub:
--   1. Calls Cloudflare API to create a Tunnel (gives the customer a
--      tunnel_id + credentials they'll install on their box)
--   2. Calls Cloudflare API to create a DNS record at
--      {slug}.example.com → that tunnel
--   3. Mints a peer_invitations row so the customer's box can
--      handshake into the federation as a peer
--   4. Records the artifacts in this table so we can:
--      - revoke / clean up later (delete DNS + tunnel + invitation)
--      - audit what was provisioned and when
--      - dedupe slugs (UNIQUE on slug)
--
-- DEPLOY TARGET: hub (enki-hub VM).
--
-- Doesn't replace peer_memberships — that's still where the actual
-- federation membership lives. node_subdomains is the PROVISIONING
-- record (DNS + tunnel side); peer_memberships is the IDENTITY record
-- (Ed25519 key side). They link via the invitation_token + the
-- eventual consumed_by_node_id (set when the customer completes
-- handshake_initial and peer_memberships gets created).

CREATE TABLE IF NOT EXISTS node_subdomains (
    subdomain_id          BIGSERIAL PRIMARY KEY,

    -- The customer-facing identity. Slug becomes part of the URL:
    --   cairo  → localhost
    -- Constraints: lowercase, alphanumeric + dash, 3-40 chars,
    -- not in a reserved list (admin, api, hub, www, etc.). The app
    -- enforces these — the column just has the slug.
    slug                  TEXT NOT NULL UNIQUE,
    full_hostname         TEXT NOT NULL,

    -- Cloudflare artifacts. tunnel_id is the UUID from the
    -- POST /accounts/{acct}/cfd_tunnel API; dns_record_id is from
    -- POST /zones/{zone}/dns_records. We hold both so we can revoke
    -- cleanly (DELETE on both endpoints).
    cloudflare_tunnel_id  TEXT,
    cloudflare_dns_id     TEXT,

    -- Free-form label for the admin UI ("Cairo PD archive node",
    -- "node-B DIA cell", etc.). Not part of any URL.
    label                 TEXT,

    -- Who issued this onboarding token (federation_admin's node_id)
    issued_by_node_id     TEXT NOT NULL,

    -- The peer_invitations.invitation_token this is tied to. When
    -- the customer's box completes handshake_initial, peer_invitations
    -- gets consumed and peer_memberships gets a new row whose
    -- peer_node_id we then copy here as consumed_by_node_id.
    invitation_token      TEXT NOT NULL,
    consumed_by_node_id   TEXT,                 -- NULL until handshake completes
    consumed_at           TIMESTAMPTZ,

    -- Lifecycle. provisioned → activated → revoked.
    status                TEXT NOT NULL DEFAULT 'provisioned'
                            CHECK (status IN ('provisioned', 'activated', 'revoked')),

    -- Provisioning audit.
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at            TIMESTAMPTZ,
    revoke_reason         TEXT
);

CREATE INDEX IF NOT EXISTS idx_node_subdomains_status
    ON node_subdomains (status);
CREATE INDEX IF NOT EXISTS idx_node_subdomains_invitation
    ON node_subdomains (invitation_token);

COMMENT ON TABLE node_subdomains IS
    'Hub-side provisioning registry for customer-onboarded nodes. Holds '
    'the Cloudflare Tunnel + DNS record IDs so the hub can revoke them '
    'cleanly when a peer is decommissioned. Linked to peer_memberships '
    'via invitation_token → consumed_by_node_id.';
