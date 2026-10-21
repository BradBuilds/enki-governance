-- 136: statement extraction run accounting
--
-- The batched extractor already counted three of its drop paths
-- (dropped_copula, dropped_no_object, dropped_object_unresolved) into a
-- dict that was returned to the worker and thrown away, and two more paths
-- dropped with no counter at all. There is no log directory on the node, so
-- the counts left no trace anywhere.
--
-- The consequence is that the fact layer's headline number has an unmeasured
-- denominator: we know 79% of the statements that REACHED the database were
-- rejected, and we do not know what fraction of the extractor's output ever
-- reached it. This table is the denominator. One row per run per document.
--
-- Additive: no existing table or column is touched, and a failure to write a
-- row here never affects the statements the run already committed.

CREATE TABLE IF NOT EXISTS statement_extraction_runs (
    run_id                        BIGSERIAL PRIMARY KEY,
    tenant_id                     TEXT NOT NULL DEFAULT 'default',
    document_id                   BIGINT NOT NULL,
    -- Which regime produced this run. NULL means the model could not be
    -- identified: honest, and queryable as such.
    extraction_profile_id         TEXT,

    -- What was offered to the extractor
    paragraphs                    INTEGER,
    batches                       INTEGER,
    llm_calls                     INTEGER,
    skipped_garbage               INTEGER,

    -- What came back, and what became of it. The identity is
    --   llm_statements_seen = statements_persisted
    --                       + dropped_malformed + dropped_copula
    --                       + dropped_no_object + dropped_subject_unresolved
    --                       + dropped_object_unresolved + persist_errors
    llm_statements_seen           INTEGER,
    statements_persisted          INTEGER,
    dropped_malformed             INTEGER,
    dropped_copula                INTEGER,
    dropped_no_object             INTEGER,
    dropped_subject_unresolved    INTEGER,
    dropped_object_unresolved     INTEGER,
    persist_errors                INTEGER,

    -- Losses the identity cannot account for: a batch whose JSON never
    -- parsed produced an unknowable number of statements, but the
    -- paragraphs it covered are knowable.
    batch_failures                INTEGER,
    paragraphs_in_failed_batches  INTEGER,
    results_over_batch            INTEGER,
    results_malformed             INTEGER,

    elapsed_s                     REAL,
    created_at                    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_stmt_extraction_runs_doc
    ON statement_extraction_runs (document_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_stmt_extraction_runs_profile
    ON statement_extraction_runs (extraction_profile_id)
    WHERE extraction_profile_id IS NOT NULL;

COMMENT ON TABLE statement_extraction_runs IS
    'Per-document accounting for batched statement extraction: what the LLM produced, what was persisted, and every path by which the rest was lost.';
