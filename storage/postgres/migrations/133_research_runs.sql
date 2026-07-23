-- 133: Deep-research runs
-- An LLM-directed iterative research loop. A strong external "director" model
-- plans topics → web-searches for source URLs → Enki grabs those URLs and
-- collapses them into an auto-created topic LIBRARY → assesses gaps → iterates
-- → produces a cited report. Slow + async → pollable.
--
-- Mirrors entity_investigations (029). The per-step audit log
-- (research_run_steps) is the one addition investigations don't need — it
-- powers the live progress timeline and the report's provenance.

CREATE TABLE IF NOT EXISTS research_runs (
    run_id            BIGSERIAL PRIMARY KEY,
    tenant_id         VARCHAR(64) NOT NULL DEFAULT 'default',
    identity_id       VARCHAR(255),                 -- who started it (nullable)
    goal              TEXT NOT NULL,                -- the research question / topic
    status            VARCHAR(32) NOT NULL DEFAULT 'queued',
        -- queued, planning, gathering, collapsing, assessing,
        -- synthesizing, done, failed, cancelled
    library_id        BIGINT REFERENCES libraries(library_id),  -- auto-created corpus
    project_id        BIGINT,                       -- optional analysis graph (Phase 2)
    director_model    VARCHAR(128),                 -- e.g. 'gpt-4o' — drives the External badge
    iteration         INT DEFAULT 0,                -- current loop iteration
    max_iterations    INT DEFAULT 4,                -- loop guard
    sources_grabbed   INT DEFAULT 0,                -- URLs fetched + collapsed
    entities_count    INT DEFAULT 0,                -- library entities so far
    facts_count       INT DEFAULT 0,                -- library facts so far
    tokens_used       INT DEFAULT 0,                -- director-model token spend
    report_markdown   TEXT,                         -- final cited report
    report_citations  JSONB DEFAULT '[]',           -- [{marker, document_id, url, title}]
    phase_detail      TEXT,                         -- human status line
    error             TEXT,
    started_at        TIMESTAMPTZ,
    completed_at      TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_research_runs_tenant  ON research_runs(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_research_runs_library ON research_runs(library_id);

-- Append-only progress log powering the pollable timeline + report provenance.
CREATE TABLE IF NOT EXISTS research_run_steps (
    step_id      BIGSERIAL PRIMARY KEY,
    run_id       BIGINT NOT NULL REFERENCES research_runs(run_id),
    tenant_id    VARCHAR(64) NOT NULL DEFAULT 'default',
    iteration    INT DEFAULT 0,
    phase        VARCHAR(32) NOT NULL,     -- planning|gathering|collapsing|assessing|synthesizing
    title        TEXT NOT NULL,            -- "Searched: 'Khufu pyramid construction'"
    detail       JSONB DEFAULT '{}',       -- {urls:[...], query:"...", gaps:[...]}
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_research_steps_run ON research_run_steps(run_id, step_id);
