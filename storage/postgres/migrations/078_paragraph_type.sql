-- 078_paragraph_type.sql
-- Paragraph-level content classification. The collapse pipeline used to
-- treat every paragraph as prose and run statement extraction on it. That
-- produced bibliographic noise on academic docs, record-fragment noise on
-- structured logs (CIA FOIA case logs), and wasted LLM calls on index/
-- header text. This column lets the router send each paragraph to the
-- right specialized extractor.
--
-- Values written by core/documents/paragraph_classifier.py:
--   prose         narrative text with verbs — runs statement extractor
--   record        structured row (date | id | subject) — record_extractor
--   bibliography  citation/footnote — citation_extractor (cites edges)
--   index         alphabetized name → page-number list — skip extraction
--   caption       figure/table caption — image_mention_extractor
--   header        chapter/page heading, TOC, page-number — skip
--   table         tabular data — row-by-row record extraction
--   transcript    Q&A interview — speaker-says-other parsing (future)
--   unknown       not yet classified

ALTER TABLE document_paragraphs
    ADD COLUMN IF NOT EXISTS paragraph_type TEXT NOT NULL DEFAULT 'unknown';

CREATE INDEX IF NOT EXISTS idx_paragraphs_type
    ON document_paragraphs (paragraph_type, document_id)
 WHERE paragraph_type <> 'unknown';

-- Track classifier provenance so we can re-run with a better model later
-- and see what changed.
ALTER TABLE document_paragraphs
    ADD COLUMN IF NOT EXISTS classified_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS classifier_version TEXT;
