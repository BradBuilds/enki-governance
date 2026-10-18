-- Migration 103: Peer identity foundation — federation membership, JWT auth,
-- invitation tokens, and per-request audit.
--
-- This is Phase 0 of the Library Federation plan
-- (the internal library federation plan). It must land before
-- any other federation work because every cross-node trust gate (G6 trust
-- score, G7 consensus, G4 hub-claim binding) requires the hub to know
-- cryptographically which peer is calling.
--
-- Today's auth is one HTTP header (X-Enki-Node-Id, env-var) — see
-- core/federation/phase4.py:35-44. Anyone can claim any node_id. This
-- migration replaces that with Ed25519 signed-challenge handshake +
-- hub-issued JWT.
--
-- Three tables:
--   peer_memberships   — the roster of who's in this federation
--   peer_invitations   — single-use tokens for onboarding new peers
--   peer_auth_audit    — every handshake / token / revocation logged
--
-- Locked decisions:
--   - Software Ed25519 keys for v1; hardware HWKEY backing in Phase 10
--   - Only federation_admin can issue invitation tokens
--   - Dual-mode rollout: ENKI_AUTH_MODE = dual (default) | required

-- ─── 1. peer_memberships ────────────────────────────────────────────────
-- One row per peer that has ever been admitted to this federation.
-- Lives on the HUB. Peers don't store this table — they store their own
-- keypair and their issued JWT, nothing else about other peers.

CREATE TABLE IF NOT EXISTS peer_memberships (
    peer_node_id        TEXT PRIMARY KEY,
    pubkey_ed25519      BYTEA NOT NULL,                -- 32 bytes
    pubkey_fingerprint  CHAR(64) NOT NULL,              -- sha256(pubkey) hex; used for lookup + display

    -- Chain of trust: who admitted this peer
    invited_by_node_id  TEXT,                           -- NULL only for the bootstrap admin
    invitation_token    TEXT,                           -- which token they consumed (audit trail)
    invited_at          TIMESTAMPTZ,
    activated_at        TIMESTAMPTZ,                    -- when handshake completed

    status              TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'active', 'suspended', 'revoked')),

    -- Trust score is independent of role; reputation accumulates from
    -- corroboration/contradiction outcomes (existing federation/phase4.py
    -- machinery). Range 0.0–1.0, default 0.5 (neutral).
    trust_score         REAL NOT NULL DEFAULT 0.5
                          CHECK (trust_score >= 0.0 AND trust_score <= 1.0),

    -- Roles control authority. Encoded as JSONB array of role strings:
    --   "peer"             — publish own work, pull subscribed libraries
    --   "library_owner"    — manage co-producers + visibility on owned libs
    --   "gdu_steward"      — publish pack versions, mark packs revoked
    --   "federation_admin" — invite/revoke peers, change roles, fork federation
    roles               JSONB NOT NULL DEFAULT '["peer"]'::jsonb,

    -- Self-reported metadata: hostname, profile (node/node-public/evidentiary),
    -- software version, capabilities. Free-form; informational only.
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Operational signal — when did we last see a JWT-authenticated call
    -- from this peer? Useful for health monitoring + stale-peer detection.
    last_seen_at        TIMESTAMPTZ,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (pubkey_fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_peer_memberships_status
    ON peer_memberships (status);
CREATE INDEX IF NOT EXISTS idx_peer_memberships_last_seen
    ON peer_memberships (last_seen_at DESC NULLS LAST)
    WHERE status = 'active';

COMMENT ON TABLE peer_memberships IS
    'Hub-side roster of federation members. One row per peer node, '
    'created at invitation, activated by signed-challenge handshake, '
    'and revocable by federation_admin. Every JWT issued by the hub '
    'is bound to a row here.';

COMMENT ON COLUMN peer_memberships.roles IS
    'JSONB array of role strings. "peer" is the baseline. '
    '"library_owner" allowed for libs they own. "gdu_steward" can publish '
    'pack versions. "federation_admin" can invite/revoke. A peer can hold '
    'multiple roles; some roles are scoped (library_owner is per-library).';

COMMENT ON COLUMN peer_memberships.trust_score IS
    'Reputation 0.0–1.0. Defaults to 0.5 at admission. Updated by hub '
    'from corroboration/contradiction outcomes (federation/phase4.py). '
    'Subscribers use this for G6 trust gate when picking which producer '
    'to pull extractions from.';


-- ─── 2. peer_invitations ────────────────────────────────────────────────
-- Single-use tokens issued by a federation_admin for onboarding new peers.
-- The new peer presents the token + their pubkey during handshake; hub
-- consumes the token, inserts a peer_memberships row, returns the first JWT.

CREATE TABLE IF NOT EXISTS peer_invitations (
    invitation_token    TEXT PRIMARY KEY,                -- URL-safe, ≥128 bits entropy
    issued_by_node_id   TEXT NOT NULL,                   -- must be federation_admin
    issued_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at          TIMESTAMPTZ NOT NULL,            -- typically 7 days

    -- Free-form label for who this token is intended for, so the admin
    -- can keep track of pending invitations. Not enforced; the token is
    -- redeemable by anyone who has it (so admins should treat it as a secret).
    intended_for_label  TEXT,

    -- Roles to assign on activation. Defaults to ["peer"] — admin can
    -- grant additional roles at invitation time.
    initial_roles       JSONB NOT NULL DEFAULT '["peer"]'::jsonb,

    consumed_at         TIMESTAMPTZ,
    consumed_by_node_id TEXT,                            -- NULL until handshake completes
    consumed_by_ip      INET,

    status              TEXT NOT NULL DEFAULT 'open'
                          CHECK (status IN ('open', 'consumed', 'expired', 'revoked'))
);

CREATE INDEX IF NOT EXISTS idx_peer_invitations_status
    ON peer_invitations (status, expires_at);

COMMENT ON TABLE peer_invitations IS
    'Single-use onboarding tokens. Issued by federation_admin via the '
    'hub admin panel. A new peer presents the token + their Ed25519 '
    'pubkey at first handshake; hub validates, inserts peer_memberships, '
    'returns initial JWT. Tokens expire (default 7 days).';


-- ─── 3. peer_auth_audit ─────────────────────────────────────────────────
-- Every auth-relevant event logged: handshake attempts (ok/fail), JWT
-- issuance, JWT rejection, peer revocation, invitation consumption.
-- Append-only; never updated. Used for post-mortem investigation if a
-- peer is suspected of compromise.

CREATE TABLE IF NOT EXISTS peer_auth_audit (
    audit_id            BIGSERIAL PRIMARY KEY,
    audited_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    peer_node_id        TEXT,                            -- NULL if pre-handshake (token validation, etc.)

    event               TEXT NOT NULL
                          CHECK (event IN (
                              'invitation_issued',
                              'invitation_consumed',
                              'invitation_rejected',
                              'handshake_ok',
                              'handshake_fail',
                              'jwt_issued',
                              'jwt_refreshed',
                              'jwt_rejected',
                              'roles_changed',
                              'suspended',
                              'revoked',
                              'reactivated'
                          )),

    reason              TEXT,                            -- short code for the cause
    request_ip          INET,
    metadata            JSONB                            -- event-specific details
);

CREATE INDEX IF NOT EXISTS idx_peer_auth_audit_peer
    ON peer_auth_audit (peer_node_id, audited_at DESC);
CREATE INDEX IF NOT EXISTS idx_peer_auth_audit_event
    ON peer_auth_audit (event, audited_at DESC);

COMMENT ON TABLE peer_auth_audit IS
    'Append-only audit log of every auth-relevant event. Used for '
    'post-mortem investigation of suspected key compromise, debugging '
    'failed handshakes, and demonstrating federation hygiene to auditors. '
    'No row in this table is ever updated or deleted.';


-- ─── 4. Bootstrap admin row ─────────────────────────────────────────────
-- The bootstrap problem: a fresh hub has no federation_admin, so no one
-- can issue the first invitation, so no peer can join. We seed a single
-- bootstrap admin row whose pubkey gets registered by the install script
-- (or the hub admin manually via the panel on first run).
--
-- The bootstrap admin is marked with peer_node_id = 'bootstrap_admin' and
-- status='pending' until the install script POSTs its pubkey to a
-- one-shot /admin/bootstrap endpoint. After that, status flips to active
-- and the bootstrap_admin can issue invitations to other peers (including
-- a real-named admin row for the operator's main node).
--
-- This row is the ONLY one ever created with invited_by_node_id IS NULL.

INSERT INTO peer_memberships (
    peer_node_id, pubkey_ed25519, pubkey_fingerprint,
    invited_by_node_id, status, roles, metadata
) VALUES (
    'bootstrap_admin',
    E'\\x00'::bytea,                                     -- placeholder; replaced on first /admin/bootstrap call
    'pending_bootstrap',
    NULL,
    'pending',
    '["federation_admin"]'::jsonb,
    '{"note": "Replace pubkey via /admin/bootstrap on first hub startup"}'::jsonb
)
ON CONFLICT (peer_node_id) DO NOTHING;
