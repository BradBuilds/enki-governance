-- Migration 130: atomic statements + proven contradictions
--
-- The offline two-pass extraction layer (scripts/atomic_extract.py) produces
-- ATOMIC STATEMENTS: one proposition each — who said what, with a verbatim quote
-- that passed the deterministic faithfulness wall. These are NOT the collapse
-- S-P-O `statements` table (migration 053); those are subject-predicate-object
-- relations between resolved entities. Atomic statements are the "who-said-what"
-- evidence units the comparison pass (scripts/compare_statements.py) reasons over.
--
-- A CONTRADICTION is proven by juxtaposition: two atomic statements about the same
-- proposition (embedding-clustered) with opposite deterministic polarity. No model
-- renders the verdict — the row just points at the two opposing statements.
--
-- Dedup: UNIQUE (tenant_id, document_id, quote_sha256) so re-running extraction
-- upserts instead of duplicating — the same "do we already have this?" gate the
-- documents table uses on `hash`.

CREATE TABLE IF NOT EXISTS atomic_statements (
    statement_id     BIGSERIAL PRIMARY KEY,
    tenant_id        TEXT NOT NULL DEFAULT 'default',
    document_id      BIGINT REFERENCES documents(document_id) ON DELETE CASCADE,
    library_id       BIGINT,                          -- pool/library context (nullable)
    who              TEXT NOT NULL,                   -- named speaker/body
    claim            TEXT NOT NULL,                   -- one neutral proposition (no spin)
    stype            TEXT,                            -- admission|contradiction|evasion|obstruction|testimony|assessment
    quote            TEXT NOT NULL,                   -- verbatim; passed faithfulness wall
    statement_date   TEXT,                            -- date the statement refers to (raw, nullable)
    polarity         TEXT NOT NULL DEFAULT 'pos',     -- pos|neg (deterministic, for contradiction comparison)
    source_url       TEXT,
    window_idx       INTEGER,                         -- which extraction window it came from
    extractor_model  TEXT,                            -- e.g. 'gemma4:e2b' — provenance of the transcription
    quote_sha256     CHAR(64),                        -- sha256(normalized quote) — dedup + content address
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, document_id, quote_sha256)
);

CREATE INDEX IF NOT EXISTS idx_atomic_statements_doc
    ON atomic_statements (document_id);
CREATE INDEX IF NOT EXISTS idx_atomic_statements_polarity
    ON atomic_statements (tenant_id, polarity);
CREATE INDEX IF NOT EXISTS idx_atomic_statements_who
    ON atomic_statements (tenant_id, who);

CREATE TABLE IF NOT EXISTS statement_contradictions (
    contradiction_id     BIGSERIAL PRIMARY KEY,
    tenant_id            TEXT NOT NULL DEFAULT 'default',
    proposition          TEXT NOT NULL,               -- the shared proposition both sides address
    affirms_statement_id BIGINT REFERENCES atomic_statements(statement_id) ON DELETE CASCADE,
    denies_statement_id  BIGINT REFERENCES atomic_statements(statement_id) ON DELETE CASCADE,
    cross_document       BOOLEAN NOT NULL DEFAULT FALSE,  -- the two sides come from different documents
    cluster_size         INTEGER,
    comparator_version   TEXT,                        -- which compare logic produced it
    detected_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, affirms_statement_id, denies_statement_id)
);

CREATE INDEX IF NOT EXISTS idx_contradictions_cross
    ON statement_contradictions (tenant_id, cross_document);
