-- 129_open_contributions.sql
--
-- Open contribution libraries: anyone — identified or anonymous — can submit
-- source documents to a library. Every contribution carries a permanent mark
-- (who added it) + source link (always visible). A relevance gate flags
-- off-topic docs and marks the contributor. Contributor trust is a WEIGHT:
--   - identified contributors   → accepted, normal weight (server does compute)
--   - anonymous w/o compute      → held (pending_self_validation), NOT accepted
--   - anonymous w/ compute-proof → accepted at LOW weight via a signed,
--                                   content-addressed envelope (verify, no re-extract)
--   - system acquirer            → now a gated contributor too (fixes contamination)
--
-- This is the generalization of the contamination fix: the acquirer is just one
-- contributor subject to the same relevance gate that flagged the 30 mislabeled
-- docs in the Acme library.
--
-- Reputation reuses the federation_node_trust model conceptually (0.5 prior,
-- corroboration ↑ / contradiction ↓) but contributors get their own table.

-- ── Contributors: identity + reputation ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS contributors (
    contributor_id     TEXT PRIMARY KEY,          -- user:<id> | anon:<pubkey_sha> | system:acquirer
    tenant_id          VARCHAR(255) NOT NULL DEFAULT 'default',
    kind               VARCHAR(16)  NOT NULL,     -- identified | anonymous | system
    display_name       TEXT,                       -- shown publicly for identified contributors
    pubkey             CHAR(64),                   -- Ed25519 pubkey (hex) for anon/signed submissions
    trust_score        REAL NOT NULL DEFAULT 0.5,  -- innocent prior, [0,1]
    total_submissions  INTEGER NOT NULL DEFAULT 0,
    accepted_count     INTEGER NOT NULL DEFAULT 0,
    flagged_count      INTEGER NOT NULL DEFAULT 0,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes              TEXT
);

-- The system acquirer is itself a contributor, so it is subject to the gate.
INSERT INTO contributors (contributor_id, kind, display_name, trust_score, notes)
VALUES ('system:acquirer', 'system', 'ENKI Acquirer', 0.6,
        'Automated source acquisition. Subject to the same relevance gate as humans.')
ON CONFLICT (contributor_id) DO NOTHING;

-- ── Contributions: the submission ledger ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS contributions (
    contribution_id    BIGSERIAL PRIMARY KEY,
    tenant_id          VARCHAR(255) NOT NULL DEFAULT 'default',
    library_id         BIGINT NOT NULL,
    contributor_id     TEXT NOT NULL REFERENCES contributors(contributor_id),
    document_id        BIGINT,                     -- set once the doc row exists
    kind               VARCHAR(16) NOT NULL,       -- public | anonymous | system
    input_kind         VARCHAR(16) NOT NULL,       -- url | upload | envelope
    source_url         TEXT,                        -- permanent provenance link (always visible)
    upload_path        TEXT,                        -- on-disk path for uploads
    content_sha256     CHAR(64),                    -- permanent content identity
    signature          TEXT,                        -- base64 Ed25519 sig (envelope/signed submits)
    envelope_sha256    CHAR(64),                    -- content-addressed envelope hash (compute-proof)
    status             VARCHAR(32) NOT NULL DEFAULT 'received',
        -- received | validating | relevant | not_relevant | rejected
        -- | accepted | pending_self_validation
    relevance_score    REAL,
    relevance_reason   TEXT,
    relevance_model    VARCHAR(64),
    relevance_scored_at TIMESTAMPTZ,
    weight             REAL NOT NULL DEFAULT 0.0,   -- contributor_weight applied to this contribution
    error              TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_contributions_status   ON contributions(status);
CREATE INDEX IF NOT EXISTS idx_contributions_library  ON contributions(library_id);
CREATE INDEX IF NOT EXISTS idx_contributions_contrib  ON contributions(contributor_id);
CREATE INDEX IF NOT EXISTS idx_contributions_validating ON contributions(status) WHERE status = 'validating';

-- ── documents: who contributed it + its relevance verdict ────────────────────
ALTER TABLE documents ADD COLUMN IF NOT EXISTS contributor_id   TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS contribution_id  BIGINT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS relevance_status VARCHAR(16); -- relevant | flagged | NULL

-- ── library_members: the contribution's mark + weight on the membership ──────
ALTER TABLE library_members ADD COLUMN IF NOT EXISTS contributor_id      TEXT;
ALTER TABLE library_members ADD COLUMN IF NOT EXISTS contributor_weight  REAL;
ALTER TABLE library_members ADD COLUMN IF NOT EXISTS submission_status   VARCHAR(32); -- accepted | pending | flagged
ALTER TABLE library_members ADD COLUMN IF NOT EXISTS relevance_score     REAL;
