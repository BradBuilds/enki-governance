-- 095_doc_extraction_triage.sql
-- Adds extraction_quality_class to documents so the triage worker can
-- route each PDF to the right extractor (pdfplumber for born-digital,
-- PaddleOCR for scans, skip for FBI redaction-only cover sheets).
--
-- Classes:
--   born_digital         clean text layer, pdfplumber works
--   scanned_pure         no text layer, needs OCR
--   scanned_with_overlay scan + printed cover sheets (FBI FOIA responses)
--   redaction_index      "DELETED PAGE INFORMATION SHEET" docs — no body content
--   mixed                some pages each
--   pending              not yet triaged

ALTER TABLE documents
  ADD COLUMN IF NOT EXISTS extraction_quality_class TEXT
    NOT NULL DEFAULT 'pending';

ALTER TABLE documents
  ADD COLUMN IF NOT EXISTS triage_decided_at TIMESTAMPTZ;

ALTER TABLE documents
  ADD COLUMN IF NOT EXISTS triage_evidence JSONB
    NOT NULL DEFAULT '{}'::jsonb;

-- Index so the re-extraction worker can quickly find docs needing a
-- specific treatment path.
CREATE INDEX IF NOT EXISTS idx_documents_triage_class
  ON documents(extraction_quality_class)
  WHERE extraction_quality_class != 'pending';
