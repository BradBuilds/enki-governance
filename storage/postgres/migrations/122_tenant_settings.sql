-- 122_tenant_settings.sql
--
-- Per-tenant runtime configuration. A small JSONB k/v store so we can
-- add settings without per-feature migrations. Initial use case: the
-- news LLM throughput cap — the operator caps how much GPU we burn on
-- news enrichment so research (investigations, dossier work) gets the
-- rest of the LLM budget.
--
-- Convention: one row per tenant. Settings live in the `settings` jsonb.
-- Reads should always provide defaults for missing keys; writes are
-- partial-update via jsonb_set, not full replace.

CREATE TABLE IF NOT EXISTS tenant_settings (
    tenant_id     VARCHAR(255) PRIMARY KEY,
    settings      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed the 'default' tenant with the news budget. 30/min at BATCH_SIZE=1
-- and CHECK_INTERVAL=15s gives the worker headroom to drain bursts
-- without monopolizing the LLM. Operator can tune via Settings → Hub.
INSERT INTO tenant_settings (tenant_id, settings)
VALUES (
    'default',
    '{"news_llm_max_per_min": 30}'::jsonb
)
ON CONFLICT (tenant_id) DO UPDATE SET
    settings = tenant_settings.settings ||
               jsonb_build_object('news_llm_max_per_min',
                                  COALESCE(tenant_settings.settings->>'news_llm_max_per_min',
                                           '30')::int);
