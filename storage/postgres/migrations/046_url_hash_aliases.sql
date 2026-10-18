-- Migration 046: url_hash on signals + signal_aliases table for cross-source dedup.
--
-- Phase 2 of the internal news collapse plan.
--
-- Pre-state: each ingested signal has a unique signal_id (random UUID),
-- so two outlets publishing the same article produce two unrelated rows.
-- The connector watermark catches WITHIN-feed dupes only. Reuters + BBC
-- + AP republishing the same wire all become separate signals.
--
-- This migration adds the dedup substrate WITHOUT changing the existing
-- ingest contract: every signal still gets its own row (we don't reject
-- on collision — that would break source attribution and audit trail).
-- Instead, we stamp a canonical-URL hash on insert and write a
-- signal_aliases row pointing the new signal at the earlier "canonical"
-- one when the hashes match.
--
-- Phase 4 (cluster-collapsed feed) will JOIN signal_aliases to render
-- one card with N source-attribution badges.

BEGIN;

-- 1. url_hash column on signals
ALTER TABLE signals
    ADD COLUMN IF NOT EXISTS url_hash VARCHAR(16);

-- Partial index — most non-content signal types (telemetry, position
-- reports) have no URL, so we index only the rows that participate in
-- dedup. Tenant-scoped because dedup is per-tenant.
CREATE INDEX IF NOT EXISTS idx_signals_url_hash
    ON signals (tenant_id, url_hash, rid)
    WHERE url_hash IS NOT NULL;

COMMENT ON COLUMN signals.url_hash IS
    'Truncated SHA-256 (16 chars) of canonicalize_url(payload->>link). '
    'Populated by storage/signals/store.py:insert_signal via '
    'core/extraction/url_canonical.py. NULL for signals without an '
    'article URL. Used to detect cross-source duplicates and emit '
    'signal_aliases rows.';

-- 2. signal_aliases table
CREATE TABLE IF NOT EXISTS signal_aliases (
    alias_id              BIGSERIAL PRIMARY KEY,
    tenant_id             VARCHAR(255) NOT NULL,
    -- The arriving (later) signal that we're declaring an alias of an earlier one
    signal_rid            BIGINT NOT NULL REFERENCES signals(rid) ON DELETE CASCADE,
    -- The earlier signal it points at (lowest-rid match for the url_hash)
    canonical_signal_rid  BIGINT NOT NULL REFERENCES signals(rid) ON DELETE CASCADE,
    url_hash              VARCHAR(16) NOT NULL,
    matched_url           TEXT,
    detected_by           VARCHAR(64) NOT NULL DEFAULT 'url_canonical_v1',
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Lineage stamps (mandatory)
    admitted_under_active_set     VARCHAR(32),
    admitted_under_pack_versions  JSONB,
    gdu_canonical                 BOOLEAN NOT NULL DEFAULT TRUE,
    ldu_t_id                      BIGINT,
    -- Each later signal points to AT MOST ONE canonical (no chains)
    CONSTRAINT signal_aliases_unique_signal UNIQUE (tenant_id, signal_rid),
    -- A signal cannot be its own alias
    CONSTRAINT signal_aliases_no_self CHECK (signal_rid <> canonical_signal_rid)
);

CREATE INDEX IF NOT EXISTS idx_signal_aliases_canonical
    ON signal_aliases (canonical_signal_rid);

CREATE INDEX IF NOT EXISTS idx_signal_aliases_url_hash
    ON signal_aliases (tenant_id, url_hash);

COMMENT ON TABLE signal_aliases IS
    'Cross-source duplicate detection. When a new signal arrives with '
    'a url_hash matching an earlier signal in the same tenant, an alias '
    'row is written pointing the new signal at the earlier (canonical) '
    'one. Phase 4 collapses these in the feed UI; Phase 6 lets the '
    'conflict scanner detect cross-source disagreements within a cluster.';

COMMIT;
