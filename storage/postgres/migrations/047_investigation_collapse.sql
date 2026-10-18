-- Migration 047: investigation findings + discovered collapse layer.
--
-- Same architectural pattern as signal_aliases for
-- news, entity_redirects for entities, and signal_enrichments dedup_link
-- for headlines. Raw investigation_findings + investigation_discovered
-- stay append-only (audit trail), and a collapse layer aggregates them
-- by their natural dedup key.
--
-- Why this matters: re-running an investigation (manually queued for
-- testing, cooldown gap, network retry, source double-fire) produces
-- identical findings — the worker writes them anyway because the table
-- is append-only. The dossier endpoint then sees N copies of "990 Filing
-- X" and "SMITH, JOHN MR. (treasurer_of)" cluttering every node card.
-- UI-side dedup is a band-aid; the actual collapse belongs in the data.
--
-- Architecture:
--
--   investigation_findings (raw, append-only) ──┐
--                                                ├─→ investigation_finding_collapsed (view)
--   investigation_discovered (raw, append-only) ─┘
--                                                ↓
--                                       investigation_discovered_collapsed (view)
--
-- The collapsed views give the dossier endpoint:
--   - one row per (entity_id, source, finding_type, normalized_title)
--   - occurrence_count = how many times the same finding was seen
--   - source_investigation_ids = which investigations produced it
--   - first_seen_at / last_seen_at = freshness window
--
-- Materialized for query speed (refreshed on a small cadence by the
-- investigation worker after each batch — see Phase 5b notes). Falls
-- back to a regular view in development.

BEGIN;

-- ── Findings collapse ────────────────────────────────────────────────
DROP MATERIALIZED VIEW IF EXISTS investigation_finding_collapsed;
CREATE MATERIALIZED VIEW investigation_finding_collapsed AS
SELECT
    fi.tenant_id,
    ei.entity_id,
    fi.source,
    fi.finding_type,
    -- Normalized title for dedup key (case-insensitive, whitespace-collapsed)
    LOWER(REGEXP_REPLACE(fi.title, '\s+', ' ', 'g')) AS normalized_title,
    -- Pick a representative title (most recent of the dedup-cluster)
    (ARRAY_AGG(fi.title ORDER BY fi.created_at DESC))[1]      AS title,
    -- Pick the highest-confidence data blob
    (ARRAY_AGG(fi.data  ORDER BY fi.confidence DESC NULLS LAST,
                                 fi.created_at DESC))[1]      AS data,
    MAX(fi.confidence)                                         AS confidence,
    COUNT(*)                                                   AS occurrence_count,
    ARRAY_AGG(DISTINCT fi.investigation_id)                    AS source_investigation_ids,
    MIN(fi.created_at)                                         AS first_seen_at,
    MAX(fi.created_at)                                         AS last_seen_at
  FROM investigation_findings fi
  JOIN entity_investigations  ei USING (investigation_id)
 WHERE ei.entity_id IS NOT NULL
 GROUP BY fi.tenant_id, ei.entity_id, fi.source, fi.finding_type,
          LOWER(REGEXP_REPLACE(fi.title, '\s+', ' ', 'g'));

-- Indexes for the dossier endpoint's typical query shape
CREATE UNIQUE INDEX idx_findings_collapsed_unique
    ON investigation_finding_collapsed
       (tenant_id, entity_id, source, finding_type, normalized_title);
CREATE INDEX idx_findings_collapsed_entity
    ON investigation_finding_collapsed (entity_id, confidence DESC);

COMMENT ON MATERIALIZED VIEW investigation_finding_collapsed IS
    'Per-entity collapsed view of investigation_findings. One row per '
    'unique (entity, source, finding_type, normalized_title). Tracks '
    'occurrence_count and source_investigation_ids for audit. Refresh '
    'with: REFRESH MATERIALIZED VIEW CONCURRENTLY investigation_finding_collapsed;';

-- ── Discovered collapse ───────────────────────────────────────────────
DROP MATERIALIZED VIEW IF EXISTS investigation_discovered_collapsed;
CREATE MATERIALIZED VIEW investigation_discovered_collapsed AS
SELECT
    di.tenant_id,
    ei.entity_id                                          AS parent_entity_id,
    LOWER(di.name)                                        AS normalized_name,
    di.relationship,
    -- Representative name (most recent casing)
    (ARRAY_AGG(di.name ORDER BY di.created_at DESC))[1]   AS name,
    -- Most-common entity_type (most-recent wins on ties)
    (ARRAY_AGG(di.entity_type ORDER BY di.created_at DESC))[1] AS entity_type,
    -- Prefer rows that have an admitted entity_id, then most-recent
    (ARRAY_AGG(di.entity_id
        ORDER BY (di.entity_id IS NOT NULL) DESC,
                 di.created_at DESC))[1]                  AS admitted_entity_id,
    -- Source list — which sources independently surfaced this entity
    ARRAY_AGG(DISTINCT di.source)                         AS sources,
    COUNT(*)                                              AS occurrence_count,
    ARRAY_AGG(DISTINCT di.investigation_id)               AS source_investigation_ids,
    MIN(di.created_at)                                    AS first_seen_at,
    MAX(di.created_at)                                    AS last_seen_at
  FROM investigation_discovered di
  JOIN entity_investigations    ei USING (investigation_id)
 WHERE ei.entity_id IS NOT NULL
   AND di.name IS NOT NULL
 GROUP BY di.tenant_id, ei.entity_id, LOWER(di.name), di.relationship;

CREATE UNIQUE INDEX idx_discovered_collapsed_unique
    ON investigation_discovered_collapsed
       (tenant_id, parent_entity_id, normalized_name, relationship);
CREATE INDEX idx_discovered_collapsed_parent
    ON investigation_discovered_collapsed (parent_entity_id, relationship);
CREATE INDEX idx_discovered_collapsed_admitted
    ON investigation_discovered_collapsed (admitted_entity_id)
    WHERE admitted_entity_id IS NOT NULL;

COMMENT ON MATERIALIZED VIEW investigation_discovered_collapsed IS
    'Per-parent-entity collapsed view of investigation_discovered. One '
    'row per unique (parent_entity, normalized_name, relationship). '
    'sources[] shows which sources independently named this entity '
    '(cross-source corroboration signal — e.g. SEC and FEC both '
    'naming "Vicki Roe" raises confidence). Refresh with: '
    'REFRESH MATERIALIZED VIEW CONCURRENTLY investigation_discovered_collapsed;';

-- Initial materialization
REFRESH MATERIALIZED VIEW investigation_finding_collapsed;
REFRESH MATERIALIZED VIEW investigation_discovered_collapsed;

COMMIT;
