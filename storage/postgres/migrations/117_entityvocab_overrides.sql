-- entityvocab_overrides — operator decisions made via /validation/ldup-triage.
--
-- The base entityvocab JSON file is the source of truth for the closed
-- vocabulary. But operators can't edit that file at runtime (read-only
-- in the prod container) and shouldn't need to — decisions about novel
-- types that show up in LDU-P should be data, not commits. This table
-- captures those runtime decisions; subtype_validator merges them on
-- top of the JSON on every reload.
--
-- One row per decision. Most recent row for a given (suspected_type)
-- wins. Decisions never deleted — overwrite by inserting a new row.
CREATE TABLE IF NOT EXISTS entityvocab_overrides (
  override_id      BIGSERIAL PRIMARY KEY,
  suspected_type   TEXT NOT NULL,
  decision         TEXT NOT NULL CHECK (decision IN ('accept','route','drop','alias')),
  -- For route: target category + subtype
  target_category  TEXT,
  target_subtype   TEXT,
  -- For accept/alias: the canonical subtype it maps to
  canonical_subtype TEXT,
  -- Bookkeeping
  decided_by       TEXT NOT NULL DEFAULT 'ldup_triage_ui',
  decided_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (
    (decision = 'route' AND target_category IS NOT NULL AND target_subtype IS NOT NULL) OR
    (decision = 'drop') OR
    (decision IN ('accept','alias') AND canonical_subtype IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS idx_entityvocab_overrides_suspected
  ON entityvocab_overrides (LOWER(suspected_type), decided_at DESC);
