-- 074_replay_and_federation_substrate.sql
--
-- Three things added — all schema, no behavior change yet. The columns
-- get populated by code in later sprints; existing rows get sensible
-- defaults so everything keeps working.
--
-- 1. extraction_profile_id on derived layers
--    Tags every emitted mention / statement / assessment with the exact
--    LLM+prompt+version combination that produced it. When Gemma 5 lands
--    and we replay a library, the new rows are tagged with the new
--    profile; the old rows remain queryable as a historical snapshot.
--    The "current view" for a library is just WHERE extraction_profile_id
--    = library.active_profile_id. Multi-profile coexistence for free.
--
-- 2. libraries.privacy_mode + publish provenance
--    By design: every library starts private_solo. Publishing
--    to public_share or sharing as private_invited is a deliberate
--    ceremony — HWKEY-signed manifest, audit-logged, hard to reverse.
--    The signature columns are the receipt of that ceremony.
--
-- 3. node_registry + extraction_profiles tables (federation substrate)
--    node_registry: every node that has ever talked to this one. Ed25519
--    pubkeys, capabilities, last-seen, current active_set hash.
--    extraction_profiles: the named LLM+prompt+model triplets nodes can
--    advertise as a capability. The federation work-claim queue uses
--    these to route compute (Sprint 5).

BEGIN;

-- ─── 1. extraction_profile_id tags ────────────────────────────────────────

ALTER TABLE entity_mentions
    ADD COLUMN IF NOT EXISTS extraction_profile_id TEXT;

ALTER TABLE statements
    ADD COLUMN IF NOT EXISTS extraction_profile_id TEXT;

ALTER TABLE assessments
    ADD COLUMN IF NOT EXISTS extraction_profile_id TEXT;

-- Pre-existing rows get a "legacy_v1" tag so we can distinguish them
-- from anything produced after profile-aware extraction lands.
UPDATE entity_mentions SET extraction_profile_id = 'legacy_v1'
 WHERE extraction_profile_id IS NULL;
UPDATE statements     SET extraction_profile_id = 'legacy_v1'
 WHERE extraction_profile_id IS NULL;
UPDATE assessments    SET extraction_profile_id = 'legacy_v1'
 WHERE extraction_profile_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_em_extraction_profile
    ON entity_mentions (extraction_profile_id)
    WHERE extraction_profile_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_stmt_extraction_profile
    ON statements (extraction_profile_id)
    WHERE extraction_profile_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_asmt_extraction_profile
    ON assessments (extraction_profile_id)
    WHERE extraction_profile_id IS NOT NULL;


-- ─── 2. libraries.privacy_mode + publish receipt ──────────────────────────

ALTER TABLE libraries
    ADD COLUMN IF NOT EXISTS privacy_mode         TEXT NOT NULL DEFAULT 'private_solo',
    ADD COLUMN IF NOT EXISTS active_profile_id    TEXT,
    -- Ceremony receipts (NULL until a publish/share ceremony occurs)
    ADD COLUMN IF NOT EXISTS published_at         TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS published_by_node_id TEXT,
    ADD COLUMN IF NOT EXISTS publish_signature    TEXT,
    ADD COLUMN IF NOT EXISTS publish_manifest     JSONB;

-- Constraint: privacy_mode is a known enum
ALTER TABLE libraries
    DROP CONSTRAINT IF EXISTS chk_library_privacy_mode;
ALTER TABLE libraries
    ADD CONSTRAINT chk_library_privacy_mode CHECK (privacy_mode IN (
        'private_solo',      -- owner only, data never leaves
        'private_invited',   -- owner + named peers (each invited via ceremony)
        'public_share',      -- open swarm, anyone can subscribe+contribute
        'public_anchor'      -- one node curates, others read-only subscribe
    ));

COMMENT ON COLUMN libraries.privacy_mode IS
    'private_solo (default, never leaves node) | private_invited (specific peers) '
    '| public_share (open swarm) | public_anchor (curated, read-only). '
    'Transition to anything other than private_solo requires a hardware-key '
    'signed publish ceremony.';
COMMENT ON COLUMN libraries.publish_manifest IS
    'Ceremony receipt — the signed manifest that authorized publication. '
    'Pinned at publish time. Includes prior_chain_hash + content_hash + '
    'contributor_list + publishing_node_id + ceremony timestamp.';


-- ─── 3. extraction_profiles registry ──────────────────────────────────────

CREATE TABLE IF NOT EXISTS extraction_profiles (
    profile_id            TEXT         PRIMARY KEY,
    -- The exact LLM ingredients that determine output
    model_name            TEXT         NOT NULL,    -- e.g. 'gemma3:12b'
    model_digest          TEXT,                     -- ollama blob sha256
    ollama_version        TEXT,
    prompt_template       TEXT         NOT NULL,    -- e.g. 'entity_v2_pack_aware'
    prompt_version        TEXT         NOT NULL,
    temperature           REAL         NOT NULL DEFAULT 0.0,
    -- Fingerprint = sha256 of (model_digest || prompt_template || prompt_version
    --                          || temperature || ollama_version). Federation
    -- uses this to route work — same fingerprint = identical output.
    fingerprint           TEXT         NOT NULL UNIQUE,
    capabilities          TEXT[]       NOT NULL DEFAULT '{}'::text[],
        -- e.g. {entity_extraction, statement_extraction, predicate_validation}
    notes                 TEXT,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    deprecated_at         TIMESTAMPTZ
);

-- Seed the current profile so existing rows reference a real record.
INSERT INTO extraction_profiles (
    profile_id, model_name, prompt_template, prompt_version, fingerprint,
    capabilities, notes
) VALUES (
    'legacy_v1',
    'gemma3:4b',                      -- approximation; will be re-fingerprinted
    'pre_profile_aware',
    'v1',
    'legacy_v1_pre_fingerprint',
    ARRAY['entity_extraction','statement_extraction','predicate_validation'],
    'Pre-Sprint-2 extractions. Tagged retroactively for replay-aware queries.'
)
ON CONFLICT (profile_id) DO NOTHING;


-- ─── 4. node_registry — peer identity ─────────────────────────────────────

CREATE TABLE IF NOT EXISTS node_registry (
    node_id              TEXT         PRIMARY KEY,
        -- 'node:<ed25519_pubkey_hex>[:8]' — short stable identifier
    display_name         TEXT,
    full_pubkey          TEXT         NOT NULL,   -- hex-encoded Ed25519 pubkey
    -- What this node can do
    capabilities         TEXT[]       NOT NULL DEFAULT '{}'::text[],
    supported_profiles   TEXT[]       NOT NULL DEFAULT '{}'::text[],
    -- Current substrate state (refreshed on every heartbeat)
    active_set_version   TEXT,
    pack_hash            TEXT,
    -- Federation metadata
    is_self              BOOLEAN      NOT NULL DEFAULT FALSE,
    first_seen           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_heartbeat       TIMESTAMPTZ,
    reputation           REAL         NOT NULL DEFAULT 1.0,
        -- 0.0 - 1.0. Starts at 1.0, drops on disagreement / late submission.
    revoked_at           TIMESTAMPTZ,
    revoked_reason       TEXT,
    metadata             JSONB        NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_node_registry_heartbeat
    ON node_registry (last_heartbeat DESC NULLS LAST)
    WHERE revoked_at IS NULL;

COMMENT ON TABLE node_registry IS
    'Every Enki node this node has talked to (including itself, marked is_self). '
    'Federation peers: pairing flow inserts; heartbeats keep last_heartbeat '
    'fresh; reputation decays on bad submissions; revoke marks the row dead '
    'without deleting (audit trail).';
COMMENT ON COLUMN node_registry.is_self IS
    'TRUE for exactly one row — this node''s own identity. Used by the '
    'install script to know which row is us.';

COMMIT;
