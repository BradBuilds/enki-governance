-- Migration 013: event clustering infrastructure
--
-- Phase 1.3b + 1.4 of the node-collapse plan. The event clusterer walks
-- recently-extracted events and tries to match them against older events
-- that describe the same real-world happening but got different
-- deterministic node_ids (because the LLM phrased the title differently).
--
-- Tables:
--   events.clustered_at                — when this event was last considered
--                                        for clustering. NULL = never processed.
--                                        Clusterer picks NULL or stale rows.
--
--   event_merge_review                 — pending merge proposals that the
--                                        clusterer deemed plausible but not
--                                        strong enough to auto-apply. A
--                                        background worker (1.4) auto-applies
--                                        them once they've aged past a
--                                        confidence-adjusted threshold, or a
--                                        human can resolve via the API.

BEGIN;

ALTER TABLE events
    ADD COLUMN IF NOT EXISTS clustered_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_events_unclustered
    ON events (created_at DESC)
    WHERE clustered_at IS NULL;

CREATE TABLE IF NOT EXISTS event_merge_review (
    review_id           BIGSERIAL PRIMARY KEY,
    tenant_id           VARCHAR(255) NOT NULL DEFAULT 'default',

    -- Candidate pair (older row is the proposed survivor)
    survivor_node_id    VARCHAR(255) NOT NULL,
    loser_node_id       VARCHAR(255) NOT NULL,

    -- Match evidence — what the clusterer saw
    match_score         NUMERIC(3,2) NOT NULL,      -- 0.00-1.00
    match_criteria      JSONB NOT NULL DEFAULT '{}'::jsonb,
        -- {type_match: bool, time_delta_seconds: int, place_match: "node"|"coord"|"text"|null,
        --  actor_overlap: int, target_overlap: int, title_similarity: float}

    -- Lifecycle
    status              VARCHAR(16) NOT NULL DEFAULT 'pending',
        -- pending|auto_applied|human_approved|rejected|stale
    proposed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at         TIMESTAMPTZ,
    resolved_by         VARCHAR(64),                 -- 'auto_resolver_v1' | 'user:<id>'
    resolution_note     TEXT,

    -- LDU lineage
    admitted_under_active_set     VARCHAR(32),
    gdu_canonical                 BOOLEAN NOT NULL DEFAULT TRUE,

    UNIQUE (tenant_id, survivor_node_id, loser_node_id)
);

CREATE INDEX IF NOT EXISTS idx_event_merge_review_pending
    ON event_merge_review (proposed_at DESC)
    WHERE status = 'pending';

COMMIT;
