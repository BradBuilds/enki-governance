-- Migration 004: learned_mappings — auto-learn substrate for extraction workers.
--
-- Every time the extraction runner encounters a signal with no registered
-- worker, the shape profiler records the payload structure here. Over
-- repeated samples the mapping_learner proposes a payload→envelope field
-- map, which is evaluated, graded, and (when stable) promoted to a live
-- declarative worker.
--
-- This table is the DATA side of the auto-learn loop. The engine that
-- executes mappings (core.extraction.workers.declarative) reads nothing
-- from this table at runtime — it only reads promoted YAML files under
-- storage/mappings/learned/ (exported from here by the promoter).
--
-- LDU lineage columns record which active_set was in effect when each
-- sample/candidate/promotion happened, so learned mappings can be audited
-- and re-derived across active_set rolls.
--
-- This migration does NOT modify any GDU pack files and does not depend
-- on any libraries/ content. It is additive and idempotent.
--
-- Authority: Phase 2.2 — auto-learn substrate.

CREATE TABLE IF NOT EXISTS learned_mappings (
    mapping_id        BIGSERIAL PRIMARY KEY,
    tenant_id         VARCHAR(255) NOT NULL DEFAULT 'default',

    -- Identity
    signal_type       VARCHAR(128) NOT NULL,       -- signalvocab type id the samples arrived under
    mapping_version   INT NOT NULL DEFAULT 1,      -- bumped on each re-proposal
    status            VARCHAR(32) NOT NULL DEFAULT 'observing',
                       -- observing | candidate | promoted | rejected | superseded

    -- Accumulated observations
    shape_profile     JSONB NOT NULL DEFAULT '{}'::jsonb,
                       -- {field_path: {types: {str: n, int: n, ...}, samples: [...], count: N}}
    sample_count      INT NOT NULL DEFAULT 0,
    distinct_payload_shapes INT NOT NULL DEFAULT 0,

    -- Proposed mapping (null until the learner has enough samples)
    proposed_map      JSONB,                        -- full mapping dict (field_map, extractors, templates)
    last_score        REAL,                         -- 0.0-1.0 completeness score from evaluation
    score_history     JSONB DEFAULT '[]'::jsonb,    -- recent scores for stability detection

    -- Promotion state
    promoted_at       TIMESTAMPTZ,
    promoted_path     TEXT,                         -- file path in storage/mappings/learned/
    reviewed_by       VARCHAR(255),                 -- human reviewer who approved, or 'auto'

    -- LDU lineage — which active_set was in effect when observations began
    admitted_under_active_set     VARCHAR(32),
    admitted_under_pack_versions  JSONB,
    gdu_canonical                 BOOLEAN NOT NULL DEFAULT TRUE,

    first_observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_observed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT learned_mappings_status_valid CHECK (
        status IN ('observing', 'candidate', 'promoted', 'rejected', 'superseded')
    ),
    CONSTRAINT learned_mappings_unique_live UNIQUE (tenant_id, signal_type, mapping_version)
);

CREATE INDEX IF NOT EXISTS idx_learned_mappings_type
    ON learned_mappings (signal_type, status);
CREATE INDEX IF NOT EXISTS idx_learned_mappings_status
    ON learned_mappings (status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_learned_mappings_promoted
    ON learned_mappings (promoted_at DESC) WHERE status = 'promoted';
