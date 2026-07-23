-- Paragraph text-quality validation: the OCR/garbage gate's verdict, persisted so
-- the meaning pipeline routes deterministically and a human reviews only the
-- uncertain middle.
--   validation_score : 0..1 sentence-likeness (core.documents.text_quality.score)
--   review_status    : pass | record | drop | flag  (NULL = not yet scored)
--                      pass   → into the meaning graph (auto)
--                      record → tabular, to the record extractor (auto)
--                      drop   → garbage, raw evidence only (auto)
--                      flag   → human review queue (non-deterministic ~few %)
ALTER TABLE document_paragraphs
  ADD COLUMN IF NOT EXISTS validation_score real,
  ADD COLUMN IF NOT EXISTS review_status    text;

CREATE INDEX IF NOT EXISTS idx_paragraphs_review_status
  ON document_paragraphs(review_status) WHERE review_status IS NOT NULL;

-- the human queue: flagged paragraphs, lowest score first
CREATE INDEX IF NOT EXISTS idx_paragraphs_flag_queue
  ON document_paragraphs(validation_score) WHERE review_status = 'flag';
