-- Migration 112: append-only history of data-integrity validation runs.
--
-- Every time the scheduled validator (or an operator hitting
-- /api/validation/data-integrity) runs the suite, every Finding is
-- captured here. The history lets us:
--
--   - Detect regressions (count of a check goes up between runs)
--   - Build a "data health" trend chart
--   - Audit when a fix actually moved the needle
--
-- We deliberately store one row per (run_id, check_name) so a row's
-- size stays bounded — sample rows are JSONB, capped to ~10 examples
-- in the worker so the table doesn't bloat.

CREATE TABLE IF NOT EXISTS validation_runs (
    run_id          BIGSERIAL PRIMARY KEY,
    run_started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_finished_at TIMESTAMPTZ,
    triggered_by    TEXT NOT NULL,    -- 'scheduled' | 'api' | 'manual'
    total_checks    INTEGER,
    with_issues     INTEGER,
    by_severity     JSONB
);

CREATE TABLE IF NOT EXISTS validation_findings (
    finding_id      BIGSERIAL PRIMARY KEY,
    run_id          BIGINT NOT NULL REFERENCES validation_runs(run_id) ON DELETE CASCADE,
    check_name      TEXT NOT NULL,
    severity        TEXT NOT NULL,    -- info | warn | error | critical
    count           INTEGER NOT NULL,
    sample          JSONB,            -- up to 5 example rows
    description     TEXT,
    fix_hint        TEXT,
    query_ms        INTEGER,
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_val_findings_run     ON validation_findings (run_id);
CREATE INDEX IF NOT EXISTS idx_val_findings_check   ON validation_findings (check_name, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_val_runs_started     ON validation_runs (run_started_at DESC);

COMMENT ON TABLE validation_runs IS
    'One row per data-integrity validation sweep. Append-only — never updated, never deleted.';
COMMENT ON TABLE validation_findings IS
    'Per-check finding captured during a validation run. Lets the operator '
    'chart how each check trends over time and catches regressions.';
