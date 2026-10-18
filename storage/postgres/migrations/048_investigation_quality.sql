-- Migration 048: investigation quality scoring + entity review queue.
--
-- Phase 7c of the internal research validation plan.
--
-- After Phase 7b made per-type routing real, the quality of investigation
-- output varies wildly: a real entity with mainstream presence (Vale, Lockheed)
-- yields 50+ findings across 10+ sources; a position-fragment that slipped
-- past gateway validation yields 0 findings + 0 discovered. Phase 7c gives
-- us a programmatic way to identify the latter so they get routed for review
-- and (if confirmed garbage) cleanup, instead of accumulating as silent
-- waste in the database.
--
-- Quality bands (computed by core/workers/investigation_quality_validator.py):
--
--   HEALTHY  (score=2): findings_count >= 4 AND entities_discovered >= 1
--                       — investigation produced both raw facts AND a graph
--                       expansion. Nothing to do; the entity is real.
--   MARGINAL (score=1): findings_count between 1 and 3, OR healthy on one
--                       axis but not the other. Could be a real-but-obscure
--                       entity, or a half-garbage one. Re-investigate every
--                       30 days; demote after 3 consecutive MARGINAL runs.
--   LOW      (score=0): 0 findings AND 0 discovered. The investigation
--                       went out, hit every source, came back empty —
--                       either the entity isn't a real public figure /
--                       organization, or it's a fragment / noise.

BEGIN;

-- 1. Quality columns on entity_investigations
ALTER TABLE entity_investigations
    ADD COLUMN IF NOT EXISTS quality_score    INTEGER,
    ADD COLUMN IF NOT EXISTS quality_band     VARCHAR(16),
    ADD COLUMN IF NOT EXISTS quality_reason   TEXT,
    ADD COLUMN IF NOT EXISTS quality_scored_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_inv_quality_band
    ON entity_investigations (tenant_id, quality_band, quality_scored_at DESC)
    WHERE quality_band IS NOT NULL;

COMMENT ON COLUMN entity_investigations.quality_score IS
    '0 = LOW (no findings, no discovered), 1 = MARGINAL (some findings OR some discovered), 2 = HEALTHY (both). Computed by Phase 7c quality validator worker.';
COMMENT ON COLUMN entity_investigations.quality_band IS
    'Human-readable label: LOW | MARGINAL | HEALTHY. Same info as quality_score but easier to filter in UI.';
COMMENT ON COLUMN entity_investigations.quality_reason IS
    'Free-text explanation of the score: which counts triggered which band, plus any signal_count context.';

-- 2. entity_review_queue — entities flagged by the validator for human review
CREATE TABLE IF NOT EXISTS entity_review_queue (
    review_id           BIGSERIAL PRIMARY KEY,
    tenant_id           VARCHAR(255) NOT NULL,
    entity_id           BIGINT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    flagged_reason      VARCHAR(64) NOT NULL,
    flagged_by          VARCHAR(64) NOT NULL DEFAULT 'investigation_quality_validator',
    flagged_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Snapshot of why we flagged it so the reviewer doesn't have to dig
    investigation_id    BIGINT,                    -- the investigation that prompted the flag
    findings_count      INTEGER,
    entities_discovered INTEGER,
    signal_count        INTEGER,                   -- entity's signal_count at flag time
    -- Resolution
    status              VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending|confirmed_garbage|kept|merged|deleted
    reviewed_at         TIMESTAMPTZ,
    reviewed_by         VARCHAR(255),
    resolution_note     TEXT,
    -- Lineage stamps (mandatory per governance principles)
    admitted_under_active_set     VARCHAR(32),
    admitted_under_pack_versions  JSONB,
    gdu_canonical                 BOOLEAN NOT NULL DEFAULT TRUE,
    -- One open review per entity at a time
    CONSTRAINT entity_review_queue_unique_open
        UNIQUE (tenant_id, entity_id, status) DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX IF NOT EXISTS idx_review_queue_pending
    ON entity_review_queue (tenant_id, flagged_at DESC)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_review_queue_reason
    ON entity_review_queue (flagged_reason, status);

COMMENT ON TABLE entity_review_queue IS
    'Human-review queue for entities flagged by automated validators. Phase 7c populates from investigation quality scoring (LOW + signal_count<3 = strong garbage candidate). Future phases (7d relevance scorer) will also populate here. Review UI lives at /review (Phase 7e).';

COMMIT;
