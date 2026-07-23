-- 070_signal_to_library_and_assessment.sql
--
-- Wires the RSS-signal substrate into the library + assessment substrate so
-- news flows into the cyclical model the same way documents do.
--
-- Two pieces:
--
-- 1. library_signals: which RSS signals corroborate which library. Populated
--    by the library_router worker via keyword + entity-overlap matching
--    against each library's focal_term, canonical_name, and anchor entities.
--    Lets /feed cards carry server-side library chips (not client-side keyword
--    guesses) and lets /library/[id] surface a "Live news" tab.
--
-- 2. statements.signal_rid: lets the existing statement_extractor + assessment_
--    promoter pipeline run on RSS title + first_paragraph the same way it runs
--    on PDF paragraphs. A statement with document_id NULL and signal_rid SET
--    is RSS-derived; the assessment_promoter doesn't care which.

BEGIN;

-- ─── library_signals ──────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS library_signals (
    library_id    BIGINT       NOT NULL REFERENCES libraries(library_id) ON DELETE CASCADE,
    signal_rid    BIGINT       NOT NULL REFERENCES signals(rid)          ON DELETE CASCADE,
    tenant_id     TEXT         NOT NULL DEFAULT 'default',
    matched_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    match_kind    TEXT         NOT NULL,  -- 'keyword' | 'entity' | 'both'
    match_score   REAL,                   -- 0.0-1.0 confidence
    keyword_hit   TEXT,                   -- which keyword fired, if any
    entity_id     BIGINT,                 -- which entity bridged, if any
    PRIMARY KEY (library_id, signal_rid)
);

CREATE INDEX IF NOT EXISTS idx_library_signals_lib_time
    ON library_signals (library_id, matched_at DESC);
CREATE INDEX IF NOT EXISTS idx_library_signals_signal
    ON library_signals (signal_rid);

COMMENT ON TABLE library_signals IS
    'Which RSS signals corroborate which library — populated by library_router worker.';
COMMENT ON COLUMN library_signals.match_kind IS
    'keyword | entity | both — how the match was made';
COMMENT ON COLUMN library_signals.match_score IS
    '0..1 confidence, weighted by keyword specificity + entity prominence';

-- ─── statements.signal_rid ────────────────────────────────────────────────

ALTER TABLE statements
    ADD COLUMN IF NOT EXISTS signal_rid BIGINT
    REFERENCES signals(rid) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_statements_signal_rid
    ON statements (signal_rid) WHERE signal_rid IS NOT NULL;

COMMENT ON COLUMN statements.signal_rid IS
    'When set, statement was extracted from an RSS signal title/first paragraph. '
    'Mutually exclusive with document_id in normal use. assessment_promoter '
    'consumes both equally.';

-- Watermark row for library_router so the worker can resume after restarts
-- without re-scanning the whole signals table.
CREATE TABLE IF NOT EXISTS library_router_watermark (
    tenant_id     TEXT         NOT NULL,
    library_id    BIGINT       NOT NULL,
    last_signal_rid BIGINT     NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, library_id)
);

COMMIT;
