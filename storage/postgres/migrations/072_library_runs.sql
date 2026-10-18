-- 072_library_runs.sql
--
-- Library-as-project state. The library_manager daemon reads this to know
-- which library is the current focus, what phase it's in, and what each
-- worker should be scoped to.
--
-- Phases (in order):
--   discover    catalog bibliographic references
--   acquire     download PDFs for cataloged refs
--   ingest      extract text + document_mentions from PDFs
--   collapse    paragraph → statement → assessment
--   enrich      Wikipedia/Wikidata KB lookups
--   investigate SEC / FEC / courts / contracts on top-N entities
--   done        all phases complete, ready to pick next library
--
-- Phase-advance triggers (hybrid):
--   - done_criteria met (e.g. all paragraphs extracted)
--   - no progress for stall_minutes (default 5)
--   - manual override via /libraries/{id}/run/advance
--
-- run_status:
--   queued    waiting in priority order
--   running   manager actively driving this library
--   paused    operator paused
--   complete  all phases finished
--   failed    unrecoverable error

BEGIN;

CREATE TABLE IF NOT EXISTS library_runs (
    run_id            BIGSERIAL    PRIMARY KEY,
    library_id        BIGINT       NOT NULL REFERENCES libraries(library_id) ON DELETE CASCADE,
    tenant_id         TEXT         NOT NULL DEFAULT 'default',
    run_status        TEXT         NOT NULL DEFAULT 'queued',
        CONSTRAINT chk_run_status CHECK (run_status IN
            ('queued', 'running', 'paused', 'complete', 'failed')),
    current_phase     TEXT,
        CONSTRAINT chk_run_phase CHECK (current_phase IS NULL OR current_phase IN
            ('discover', 'acquire', 'ingest', 'collapse', 'enrich', 'investigate', 'done')),
    priority          INT          NOT NULL DEFAULT 100,
    queued_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    started_at        TIMESTAMPTZ,
    phase_started_at  TIMESTAMPTZ,
    completed_at      TIMESTAMPTZ,
    last_progress_at  TIMESTAMPTZ,
    -- per-phase counters captured at phase entry/exit so we can compute deltas
    metrics           JSONB        NOT NULL DEFAULT '{}'::jsonb,
    -- log of phase transitions: [{phase, entered_at, exited_at, snapshot}]
    phase_history     JSONB        NOT NULL DEFAULT '[]'::jsonb,
    started_by        TEXT,
    note              TEXT,
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- One open run per library at a time
CREATE UNIQUE INDEX IF NOT EXISTS uq_library_runs_open
    ON library_runs (library_id)
    WHERE run_status IN ('queued', 'running', 'paused');

CREATE INDEX IF NOT EXISTS idx_library_runs_status_pri
    ON library_runs (run_status, priority DESC, queued_at ASC);

-- One library at a time has "running" status
CREATE UNIQUE INDEX IF NOT EXISTS uq_library_runs_one_running
    ON library_runs ((1))
    WHERE run_status = 'running';

COMMENT ON TABLE library_runs IS
    'Library-as-project run state. library_manager daemon orchestrates these.';
COMMENT ON COLUMN library_runs.metrics IS
    '{ refs_total, refs_acquired, refs_ingested, paragraphs_total, '
    'statements_total, assessments_total, entities_total, entities_enriched, '
    'entities_investigated, started_baseline:{...} }';
COMMENT ON COLUMN library_runs.phase_history IS
    'Array of {phase, entered_at, exited_at, advance_reason, snapshot} rows.';

COMMIT;
