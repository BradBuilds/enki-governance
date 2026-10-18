-- Migration 050: per-finding manual attribution + entity-split mechanism.
--
-- Phase 7e of the internal research validation plan.
--
-- Two parts to support the disambiguation workflow:
--
-- 1. Per-finding human-confirmed/rejected attribution (overrides the
--    LLM relevance score). Reviewer clicks ✓ → finding stays attached
--    to this entity. Clicks ✗ → finding marked rejected. The override
--    column lets the dossier respect human judgment over the model.
--
-- 2. Entity SPLIT records — when ≥ N rejected findings cluster into a
--    coherent "different entity" (e.g. the 4 wrong David Harlow cases
--    are about a bankruptcy filer, not the Acme Widgets CEO), the
--    user can spin them into a new entity. The original entity keeps
--    its base60 address and signal count; the new entity gets its own
--    address; the rejected findings get re-attributed via reassigning
--    investigations OR (cheaper) via the override table.
--
-- Both DELETE-safe — splits keep both entities, attributions keep the
-- raw finding row. Reversible.

BEGIN;

-- ── Per-finding human attribution ─────────────────────────────────
CREATE TABLE IF NOT EXISTS finding_attributions (
    attribution_id  BIGSERIAL PRIMARY KEY,
    tenant_id       VARCHAR(255) NOT NULL,
    finding_id      BIGINT NOT NULL REFERENCES investigation_findings(finding_id) ON DELETE CASCADE,
    -- Which entity does the user say this finding belongs to?
    -- Usually the finding's parent entity (confirming) or NULL (rejecting outright).
    entity_id       BIGINT REFERENCES entities(entity_id) ON DELETE SET NULL,
    decision        VARCHAR(20) NOT NULL,  -- 'confirmed' | 'rejected' | 'reassigned'
    reviewer        VARCHAR(255) NOT NULL,
    reviewer_note   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Lineage
    admitted_under_active_set     VARCHAR(32),
    admitted_under_pack_versions  JSONB,
    -- Each finding has at most one current decision
    CONSTRAINT finding_attributions_unique UNIQUE (tenant_id, finding_id)
);

CREATE INDEX IF NOT EXISTS idx_attributions_finding
    ON finding_attributions (finding_id);
CREATE INDEX IF NOT EXISTS idx_attributions_decision
    ON finding_attributions (tenant_id, decision, created_at DESC);

COMMENT ON TABLE finding_attributions IS
    'Human override layer over the relevance_score on investigation_findings. '
    'Decisions: confirmed (✓ this is about the entity) | rejected (✗ wrong) '
    '| reassigned (move to a different entity, see entity_id). The dossier '
    'endpoint respects this column over the LLM relevance score so user '
    'judgment wins.';

-- ── Entity splits ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS entity_splits (
    split_id            BIGSERIAL PRIMARY KEY,
    tenant_id           VARCHAR(255) NOT NULL,
    parent_entity_id    BIGINT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    new_entity_id       BIGINT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    split_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    split_by            VARCHAR(255) NOT NULL,
    reason              TEXT,
    -- finding_ids re-attributed to the new entity (audit trail; the
    -- actual reassignment lives in finding_attributions.entity_id)
    reassigned_findings BIGINT[],
    -- Lineage stamps
    admitted_under_active_set     VARCHAR(32),
    admitted_under_pack_versions  JSONB,
    -- Two splits CAN'T target the same (parent, new) pair
    CONSTRAINT entity_splits_unique UNIQUE (parent_entity_id, new_entity_id),
    -- An entity can't be split from itself
    CONSTRAINT entity_splits_no_self CHECK (parent_entity_id <> new_entity_id)
);

CREATE INDEX IF NOT EXISTS idx_splits_parent
    ON entity_splits (tenant_id, parent_entity_id);
CREATE INDEX IF NOT EXISTS idx_splits_new
    ON entity_splits (new_entity_id);

COMMENT ON TABLE entity_splits IS
    'When a user disambiguates a parent entity into multiple sibling '
    'entities (e.g. "David Harlow CEO" stays at parent; "David Harlow '
    'Florida bankruptcy" becomes a new entity), each split is recorded '
    'here for audit + potential rollback. The reassigned_findings array '
    'gives you the exact set of facts that moved. Inverse of '
    'entity_redirects (which is for merges); these tables together let '
    'us treat entity disambiguation as fully reversible.';

COMMIT;
