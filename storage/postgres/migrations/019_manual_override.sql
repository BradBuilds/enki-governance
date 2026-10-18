-- Migration 019: manual override columns + audit log (Phase 5C)
--
-- Gives humans a first-class escape hatch from auto-classification:
--   * validation_locked — once set, the validator worker skips this row
--     forever (until explicitly unlocked). Every user override flips it.
--   * manual_override_log — append-only record of every classify /
--     reject / confirm / unlock / merge action with before+after state,
--     user identity, and free-text reason.
--
-- This pairs with the kb_user_contributions table (migration 018) which
-- captures the CORRECTION AS A RULE for future auto-validation. The
-- override log captures the ACTION; the user-contributions table
-- captures the KNOWLEDGE. Same event → two records → two useful queries.

BEGIN;

-- ── lock column on the master tables ────────────────────────────────
ALTER TABLE entities
    ADD COLUMN IF NOT EXISTS validation_locked BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE places
    ADD COLUMN IF NOT EXISTS validation_locked BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE topics
    ADD COLUMN IF NOT EXISTS validation_locked BOOLEAN NOT NULL DEFAULT FALSE;

-- Partial index on the common filter — "rows eligible for validator work"
CREATE INDEX IF NOT EXISTS idx_entities_validator_eligible
    ON entities (validation_attempts, created_at)
    WHERE validation_status = 'pending' AND validation_locked = FALSE;

CREATE INDEX IF NOT EXISTS idx_places_validator_eligible
    ON places (validation_attempts, created_at)
    WHERE validation_status = 'pending' AND validation_locked = FALSE;

CREATE INDEX IF NOT EXISTS idx_topics_validator_eligible
    ON topics (validation_attempts, created_at)
    WHERE validation_status = 'pending' AND validation_locked = FALSE;


-- ── audit log ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS manual_override_log (
    log_id                BIGSERIAL PRIMARY KEY,
    tenant_id             VARCHAR(255) NOT NULL DEFAULT 'default',
    user_id               VARCHAR(255) NOT NULL,

    -- what got touched
    target_kind           VARCHAR(16) NOT NULL,   -- entity | place | topic
    target_id             BIGINT NOT NULL,        -- entity_id / place_id / topic_id
    target_node_id        VARCHAR(255),           -- knowledge_nodes.node_id at time of action

    -- what action
    action                VARCHAR(16) NOT NULL,   -- classify | reject | confirm | unlock | merge | unmerge | migrate

    -- before state (whole-row snapshot of the validation-relevant fields)
    before_canonical_name TEXT,
    before_canonical_type VARCHAR(64),
    before_status         VARCHAR(16),
    before_source         VARCHAR(32),
    before_source_id      TEXT,

    -- after state (what the action landed)
    after_canonical_name  TEXT,
    after_canonical_type  VARCHAR(64),
    after_status          VARCHAR(16),
    after_target_kind     VARCHAR(16),            -- for migrate: entity→place, etc.
    after_target_node_id  VARCHAR(255),           -- for migrate: new node_id after kind change

    note                  TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- governance lineage
    admitted_under_active_set     VARCHAR(32),
    admitted_under_pack_versions  JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_override_log_by_target
    ON manual_override_log (target_kind, target_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_override_log_by_user
    ON manual_override_log (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_override_log_recent
    ON manual_override_log (tenant_id, created_at DESC);

COMMIT;
