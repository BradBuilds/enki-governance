-- Migration 120: Finding-level idempotency
--
-- A re-run of an investigation (after the 24h cooldown elapses, or when
-- an operator forces a sync run) should not re-store findings that are
-- byte-identical to ones already stored. Before this change, 10
-- runs for one entity each stored the same 8 Wikipedia/DDG/etc rows, so the
-- dossier reported "8 findings" but the underlying table held 80 copies.
--
-- Fix: compute a stable content hash at INSERT time and gate via partial
-- UNIQUE index. New rows are admitted; identical re-stores become
-- no-ops via ON CONFLICT DO NOTHING.
--
-- Scope: deduplication is per-tenant + per-source — the SAME Wikipedia
-- summary stored against TWO different investigations is treated as
-- the same fact. Investigations link to it via investigation_id, so
-- "findings for investigation X" stays well-defined as long as the
-- query uses the existing FK relationship.
--
-- We dedup across investigations rather than within one because the
-- per-investigation case is already prevented by the worker not
-- re-running sources within a single _run_investigation call; the
-- cross-investigation case is what we observed in the wild.

ALTER TABLE investigation_findings
    ADD COLUMN IF NOT EXISTS content_hash CHAR(40);

COMMENT ON COLUMN investigation_findings.content_hash IS
    'SHA1 of (source, title, sorted-key JSON of data). Used by '
    'uniq_finding_content to dedupe re-runs.';

CREATE UNIQUE INDEX IF NOT EXISTS uniq_finding_content
    ON investigation_findings (tenant_id, source, content_hash)
 WHERE content_hash IS NOT NULL;
