-- 135: Per-role LLM provider assignment
-- ai_config held ONE row per tenant, so a single provider choice served two
-- jobs with opposite requirements: interactive chat (wants a strong cloud
-- reasoner, answers in seconds) and bulk extraction/OCR (wants a cheap local
-- model that can run 24/7 at zero marginal cost). Because they shared the row,
-- pointing extraction at local Ollama also pointed chat there, where it queued
-- behind the OCR backlog and timed out.
--
-- Splitting on role lets each be configured independently: a provider incident
-- on one can no longer take the other down. Lookups fall back to the
-- 'default' row, so a tenant with no role-specific row behaves exactly as
-- before this migration.

ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS role VARCHAR(32) NOT NULL DEFAULT 'default';

-- Repoint the primary key from (tenant_id) to (tenant_id, role).
ALTER TABLE ai_config DROP CONSTRAINT IF EXISTS ai_config_pkey;
ALTER TABLE ai_config ADD CONSTRAINT ai_config_pkey PRIMARY KEY (tenant_id, role);

COMMENT ON COLUMN ai_config.role IS
    'Which job this provider serves: default | chat | extraction. Lookups for a role with no row fall back to the default row.';
