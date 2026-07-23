-- Migration 002: Add report_schedules and reports tables
-- Required by: Reports feature (Phase 4)

CREATE TABLE IF NOT EXISTS report_schedules (
    schedule_id     VARCHAR(255) PRIMARY KEY,
    tenant_id       VARCHAR(255) NOT NULL,
    name            VARCHAR(500) NOT NULL,
    cron_expression VARCHAR(100) NOT NULL DEFAULT '0 7 * * *',
    report_type     VARCHAR(50) NOT NULL DEFAULT 'intelligence_brief',
    topics          TEXT[] DEFAULT '{}',
    delivery        VARCHAR(20) NOT NULL DEFAULT 'in_app',
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    last_run_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_report_sched_tenant ON report_schedules(tenant_id);
CREATE INDEX IF NOT EXISTS idx_report_sched_enabled ON report_schedules(tenant_id, enabled) WHERE enabled = TRUE;

CREATE TABLE IF NOT EXISTS reports (
    report_id       VARCHAR(255) PRIMARY KEY,
    tenant_id       VARCHAR(255) NOT NULL,
    schedule_id     VARCHAR(255),
    title           VARCHAR(1000) NOT NULL,
    report_type     VARCHAR(50) NOT NULL DEFAULT 'intelligence_brief',
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sections        JSONB DEFAULT '[]',
    summary         TEXT,
    section_count   INTEGER DEFAULT 0,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_reports_tenant ON reports(tenant_id);
CREATE INDEX IF NOT EXISTS idx_reports_generated ON reports(tenant_id, generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_reports_schedule ON reports(schedule_id) WHERE schedule_id IS NOT NULL;

-- Track this migration
INSERT INTO schema_migrations (version, description)
VALUES ('002_report_tables', 'Add report_schedules and reports tables for intelligence briefing')
ON CONFLICT (version) DO NOTHING;
