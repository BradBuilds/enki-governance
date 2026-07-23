-- Migration 018: persistent knowledge-base cache + user contribution layer
--
-- Phase 5B — Layer B of the knowledge node base.
--
-- The validation chain today makes a Wikidata round-trip for every candidate.
-- This migration adds the cache that turns repeat lookups into DB hits:
--
--   entity_kb_cache       — one row per (normalized_name, kind), stamped with
--                           which upstream source confirmed it and when. Next
--                           time "Boeing" appears in any article, the
--                           resolver hits this cache FIRST and skips the
--                           network call entirely.
--
--   wikidata_qid_cache    — Q-ID → mapped canonical_type. Caches the result
--                           of the P279 subclass-walk so future candidates
--                           mapping to the same Q-ID resolve in constant time.
--
--   kb_user_contributions — when a human reclassifies, rejects, or accepts
--                           an entity, the decision lands here. These take
--                           AUTHORITY over upstream — if a user said
--                           "Strait of Hormuz is a strait", Wikidata can't
--                           silently overwrite that later.
--
-- Over weeks, entity_kb_cache grows into Layer B of the "knowledge node base
-- that grows from research and user touches" — federated Enki instances can
-- export/import it as a GDU pack.

BEGIN;

-- ── Layer B.1: per-normalized-name cache ─────────────────────────────
CREATE TABLE IF NOT EXISTS entity_kb_cache (
    cache_id              BIGSERIAL PRIMARY KEY,
    tenant_id             VARCHAR(255) NOT NULL DEFAULT 'default',

    normalized_name       TEXT NOT NULL,
    kind                  VARCHAR(16) NOT NULL,            -- entity | place | topic

    canonical_name        TEXT NOT NULL,
    canonical_type        VARCHAR(64) NOT NULL,
    aliases               TEXT[] DEFAULT ARRAY[]::text[],

    source                VARCHAR(32) NOT NULL,             -- wikidata | wikipedia | geonames | gleif | opencorporates | user | llm
    source_id             TEXT,                             -- Q66, GeoNames ID, LEI, …
    source_url            TEXT,
    identifiers           JSONB DEFAULT '{}'::jsonb,        -- {wikidata_qid, lei, geonames_id, isin, …}
    description           TEXT,
    thumbnail_url         TEXT,
    confidence            NUMERIC(3,2) DEFAULT 0.0,

    hit_count             INTEGER NOT NULL DEFAULT 0,
    first_seen_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_hit_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_verified_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    admitted_under_active_set VARCHAR(32),
    gdu_canonical         BOOLEAN NOT NULL DEFAULT TRUE,

    UNIQUE (tenant_id, normalized_name, kind)
);

CREATE INDEX IF NOT EXISTS idx_kb_cache_by_source_id
    ON entity_kb_cache (source, source_id)
    WHERE source_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_kb_cache_hot
    ON entity_kb_cache (tenant_id, hit_count DESC, last_hit_at DESC);


-- ── Layer B.2: Wikidata Q-ID → mapped type cache ─────────────────────
-- One-time cost per unknown Q-ID: walk P279 once, remember forever.
CREATE TABLE IF NOT EXISTS wikidata_qid_cache (
    qid                   VARCHAR(32) PRIMARY KEY,
    mapped_type           VARCHAR(64),                      -- NULL if no ancestor matched
    resolved_via          VARCHAR(32) NOT NULL,             -- 'direct' | 'description' | 'p279_walk' | 'llm' | 'user'
    depth                 SMALLINT NOT NULL DEFAULT 0,      -- # of P279 hops
    resolved_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    hit_count             INTEGER NOT NULL DEFAULT 0,
    last_hit_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ── Layer C: user contributions (authoritative over Layer A/B) ───────
CREATE TABLE IF NOT EXISTS kb_user_contributions (
    contribution_id       BIGSERIAL PRIMARY KEY,
    tenant_id             VARCHAR(255) NOT NULL DEFAULT 'default',
    user_id               VARCHAR(255) NOT NULL,

    kind                  VARCHAR(16) NOT NULL,             -- entity | place | topic
    raw_text              TEXT NOT NULL,                    -- what was typed/seen
    normalized_name       TEXT NOT NULL,                    -- lookup key

    action                VARCHAR(16) NOT NULL,             -- classify | reject | confirm | merge | unmerge
    canonical_name        TEXT,
    canonical_type        VARCHAR(64),
    target_node_id        VARCHAR(255),                     -- when action=merge, this is the survivor
    identifiers           JSONB DEFAULT '{}'::jsonb,        -- user-supplied identifiers (wiki QID, LEI, …)

    note                  TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Stamp which active_set + pack versions were in force at decision time
    admitted_under_active_set     VARCHAR(32),
    admitted_under_pack_versions  JSONB DEFAULT '{}'::jsonb,

    -- UNIQUE so two users re-contributing the same rule doesn't double-count
    UNIQUE (tenant_id, normalized_name, kind, user_id, action)
);

CREATE INDEX IF NOT EXISTS idx_user_contribs_by_name
    ON kb_user_contributions (tenant_id, normalized_name, kind);

CREATE INDEX IF NOT EXISTS idx_user_contribs_by_user
    ON kb_user_contributions (user_id, created_at DESC);

COMMIT;
