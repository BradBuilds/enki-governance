-- Migration 111: structural classification of documents at ingest.
--
-- Different document types have wildly different signal-to-noise ratios
-- when fed to a generic LLM extractor:
--
--   gds_booking_record (Amadeus/Sabre PNRs) — 90% structural codes, 10%
--       intelligence (passenger names, routings, fares). Generic
--       extraction yields hundreds of garbage mentions per doc; needs
--       a domain-specific parser.
--   court_filing — case caption + parties + dates + claims. Has a
--       parser-friendly grammar (Bluebook citations, docket numbers).
--   news_article — paragraph text, good fit for generic LLM extraction.
--   government_record — variable; many carry FOIA exemption markers
--       like (b)(6) that pollute mention extraction.
--   research_paper — citations + abstract + body.
--   tabular — XLS/CSV; row-by-row extraction, not paragraph extraction.
--   email_thread — header + body + reply chains.
--   image_with_text — OCR'd image; lower confidence.
--   unknown — fallback.
--
-- The class drives which extraction recipe runs. Stored as TEXT (not
-- enum) so adding a new class is a migration-free config change in the
-- classifier code.
--
-- The classifier is heuristic for v1 (no LLM call required). Confidence
-- < 0.5 means "couldn't decide"; the extractor falls back to the
-- generic recipe and the doc gets re-classified later if better signal
-- arrives (e.g. after OCR completes).

ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS document_class            TEXT,
    ADD COLUMN IF NOT EXISTS classification_confidence REAL,
    ADD COLUMN IF NOT EXISTS classified_at             TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS classification_evidence   JSONB;

CREATE INDEX IF NOT EXISTS idx_documents_class
    ON documents (document_class)
    WHERE document_class IS NOT NULL;

-- A separate index on (class, tenant_id) helps the per-class re-extract
-- worker pull just the docs of one class quickly.
CREATE INDEX IF NOT EXISTS idx_documents_class_tenant
    ON documents (document_class, tenant_id)
    WHERE document_class IS NOT NULL;

COMMENT ON COLUMN documents.document_class IS
    'Structural class assigned at ingest by core.documents.classifier. '
    'Drives which extraction recipe runs (generic LLM, PNR parser, '
    'court-filing parser, etc.). NULL = not yet classified.';

COMMENT ON COLUMN documents.classification_confidence IS
    'Heuristic match strength in [0, 1]. < 0.5 means low confidence; '
    'extractor falls back to generic recipe and the doc is queued for '
    're-classification when better signal is available.';

COMMENT ON COLUMN documents.classification_evidence IS
    'JSON record of what the classifier matched on: '
    '{"signals": ["amadeus_header", "pnr_action_codes"], "rejected": ["news_byline"]}. '
    'Used for debugging mis-classifications.';
