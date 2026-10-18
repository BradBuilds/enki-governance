-- Migration 009 — story_research
--
-- Per-node research artifact: summary, claims, corroboration matrix,
-- sources consulted. One row per research run; keyed on the story's
-- knowledge_nodes.node_id (VARCHAR 255).
--
-- Claims and corroborations also get mirrored as knowledge_nodes +
-- knowledge_edges by the orchestrator so the graph view picks them
-- up — but the authoritative, queryable artifact lives here.

CREATE TABLE IF NOT EXISTS story_research (
    research_id     BIGSERIAL PRIMARY KEY,
    tenant_id       VARCHAR(255) NOT NULL DEFAULT 'default',
    node_id         VARCHAR(255) NOT NULL,

    -- Lifecycle
    status          VARCHAR(32)  NOT NULL DEFAULT 'pending',
                    -- pending | running | complete | error
    current_stage   VARCHAR(64),
    stages_done     INT          NOT NULL DEFAULT 0,
    stages_total    INT          NOT NULL DEFAULT 6,
    error           TEXT,

    -- Result artifacts (populated incrementally)
    summary         JSONB,       -- {bullets: [...], stance: "..."}
    claims          JSONB,       -- [{id, claim, type, entities, temporal, ...}]
    conflicts       JSONB,       -- [{claim_id, confirms:[...], contradicts:[...]}]
    sources_used    JSONB,       -- [{url, outlet, title, stance, evidence}]
    entities_found  JSONB,       -- [{name, type, role}]

    -- Accounting
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    gemma_calls     INT          NOT NULL DEFAULT 0,
    source_count    INT          NOT NULL DEFAULT 0,

    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- One research run per (tenant, node) — re-runs UPDATE in place.
CREATE UNIQUE INDEX IF NOT EXISTS idx_story_research_unique
    ON story_research (tenant_id, node_id);

CREATE INDEX IF NOT EXISTS idx_story_research_status
    ON story_research (tenant_id, status, updated_at DESC);
