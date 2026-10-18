-- Migration 110: prep documents table to serve original source bytes to the UI.
--
-- The /api/documents/{id}/source endpoint streams the raw file (PDF, DOCX,
-- XLSX, TXT, HTML, image, etc.) so the in-browser viewer can render it.
-- For that we need to set Content-Type correctly and offer the file a real
-- name on download. Two new columns:
--
--   mime_type       — canonical IANA mime, e.g. 'application/pdf'.
--                     Backfilled from on_disk_path extension + python-magic
--                     where the extension lies.
--   source_filename — what to call the file when the user clicks Download.
--                     Falls back to "{document_id}.{ext}" when missing.
--
-- Both nullable. Old documents that never carried this metadata can still
-- be served (the endpoint sniffs the extension as a fallback) but having
-- the columns explicit lets new ingest paths populate them once and use
-- the indexes cheaply.

ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS mime_type       TEXT,
    ADD COLUMN IF NOT EXISTS source_filename TEXT;

CREATE INDEX IF NOT EXISTS idx_documents_mime_type
    ON documents (mime_type) WHERE mime_type IS NOT NULL;

COMMENT ON COLUMN documents.mime_type IS
    'IANA media type the UI viewer dispatches on, e.g. application/pdf. '
    'Populated at ingest from python-magic or the source HTTP Content-Type. '
    'NULL means "sniff from on_disk_path extension at serve time".';

COMMENT ON COLUMN documents.source_filename IS
    'Original filename as supplied by the source (HTTP Content-Disposition, '
    'or the on-disk basename). Used as the inline+download filename in the '
    '/source endpoint. NULL falls back to "{document_id}.{ext}".';
