-- 054: Dual-extraction page readings + LLM-judge verdicts
--
-- Stores INDEPENDENT readings of the same page from multiple extraction engines
-- (pdfplumber native text, PaddleOCR, Ollama vision) so we can validate text
-- quality by comparison rather than trust a single engine. An LLM-as-judge
-- pass produces a verdict per page; the verdict drives which reading
-- populates document_pages.raw_text and feeds downstream node collapse.
--
-- Why this is its own table (not columns on document_pages):
--   • Each engine may run at a different time (native at ingest, OCR later)
--   • We need to preserve EVERY reading even after a judgment, so a future
--     better LLM can re-judge without re-running OCR
--   • Comparison itself produces a row (engine='judge_merged') with the
--     chosen text, structured differences, and confidence
--
-- See:
--   the internal refdata hierarchical collapse plan (Phase 2.5)

CREATE TABLE IF NOT EXISTS document_page_readings (
    reading_id       BIGSERIAL PRIMARY KEY,
    document_id      BIGINT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    page_id          BIGINT NOT NULL REFERENCES document_pages(page_id) ON DELETE CASCADE,
    engine           TEXT NOT NULL,           -- 'pdfplumber' / 'ocr_paddle' / 'llm_vision' / 'judge_merged'
    raw_text         TEXT,
    confidence       REAL,
    char_count       INT,
    word_count       INT,
    line_count       INT,
    -- Engine-specific data: paddle line boxes, vision JSON, judge differences.
    metadata         JSONB DEFAULT '{}'::jsonb,
    -- For the 'judge_merged' rows: which engine won, why, structured diff.
    judge_verdict    JSONB,
    is_chosen        BOOLEAN DEFAULT FALSE,   -- True for the reading we wrote back to document_pages.raw_text
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (page_id, engine)
);

CREATE INDEX IF NOT EXISTS idx_page_readings_doc ON document_page_readings(document_id);
CREATE INDEX IF NOT EXISTS idx_page_readings_page ON document_page_readings(page_id);
CREATE INDEX IF NOT EXISTS idx_page_readings_engine ON document_page_readings(engine);
CREATE INDEX IF NOT EXISTS idx_page_readings_chosen ON document_page_readings(page_id) WHERE is_chosen;

-- Per-corpus extraction policy: the calibration output that says
-- "for documents that look like X, prefer engine Y". Populated by a
-- calibration job after enough pages have been judged.
CREATE TABLE IF NOT EXISTS corpus_extraction_policy (
    corpus_id        TEXT NOT NULL,
    doc_class        TEXT NOT NULL,           -- 'born_digital_modern' / 'scanned_typewriter' / 'court_filing' / etc.
    preferred_engine TEXT NOT NULL,           -- 'pdfplumber' / 'ocr_paddle' / 'llm_vision' / 'dual_with_judge'
    sample_size      INT,                     -- judged-page count behind this policy
    accuracy_score   REAL,                    -- judge-mean agreement w/ preferred engine
    rationale        JSONB,                   -- top failure modes of losing engines
    last_calibrated  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (corpus_id, doc_class)
);
