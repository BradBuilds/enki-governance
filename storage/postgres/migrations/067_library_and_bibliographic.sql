-- 067: Library + bibliographic reference layer
--
-- The OSINT discovery pattern: for a topic, query bibliographic indices
-- (Google Books, Scholar, Wikipedia bibliographies, archive.org) to
-- discover WHICH references SHOULD exist, then attempt to acquire each
-- as a PDF for ingestion. References become first-class nodes whether
-- or not they've been acquired — the library tracks "what we know
-- exists" vs "what we've actually read."
--
-- See the internal base60 cyclical language model notes
-- for the design rationale.
--
-- Schema:
--   libraries                — addressable topic-anchored subgraphs (13.X.Y)
--   bibliographic_references  — discovered refs, status-tracked
--   library_members           — which entities/docs/refs belong to a library
--   citation_emissions        — when doc D cites ref R (provenance trail)
--
-- Each library and each reference gets a base-60 address. Acquired
-- references also reuse their document address (5.X.Y) — the
-- bibliographic_references row + the documents row coexist, with the
-- documents row activating once acquisition succeeds.

-- ── Address sequences for new subtypes ───────────────────────────────

INSERT INTO gdu_address_sequences (category, subtype, next_seq) VALUES
    ('library', 'entity_library',       1),
    ('library', 'relationship_library', 1),
    ('library', 'event_library',        1),
    ('library', 'topic_library',        1),
    ('library', 'corpus_library',       1),
    ('document', 'bibliographic_reference', 1),
    ('source',   'bibliographic_index',     1)
ON CONFLICT (category, subtype) DO NOTHING;


-- ── libraries — topic-anchored addressable subgraphs ─────────────────

CREATE TABLE IF NOT EXISTS libraries (
    library_id              BIGSERIAL PRIMARY KEY,
    base60_address          TEXT NOT NULL UNIQUE,
    library_subtype         TEXT NOT NULL CHECK (library_subtype IN
                              ('entity_library','relationship_library',
                               'event_library','topic_library','corpus_library')),
    canonical_name          TEXT NOT NULL,
    focal_term              TEXT,                   -- the user-given seed term ("Roswell", "Khufu")
    focal_entity_id         BIGINT REFERENCES entities(entity_id) ON DELETE SET NULL,
    focal_address           TEXT,                    -- when scoped to a specific node
    description             TEXT,
    tenant_id               TEXT NOT NULL DEFAULT 'default',
    member_addresses        TEXT[] NOT NULL DEFAULT '{}',
    reference_count         INTEGER NOT NULL DEFAULT 0,
    acquired_count          INTEGER NOT NULL DEFAULT 0,
    pending_count           INTEGER NOT NULL DEFAULT 0,
    unavailable_count       INTEGER NOT NULL DEFAULT 0,
    document_count          INTEGER NOT NULL DEFAULT 0,
    entity_count            INTEGER NOT NULL DEFAULT 0,
    fact_count              INTEGER NOT NULL DEFAULT 0,
    discovery_status        TEXT NOT NULL DEFAULT 'active'
                              CHECK (discovery_status IN ('active','paused','saturated','archived')),
    refresh_interval_hours  INTEGER NOT NULL DEFAULT 168,    -- weekly default
    next_refresh_at         TIMESTAMPTZ,
    last_refreshed_at       TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes                   TEXT,
    UNIQUE (tenant_id, canonical_name)
);

CREATE INDEX IF NOT EXISTS idx_libraries_focal_entity ON libraries(focal_entity_id);
CREATE INDEX IF NOT EXISTS idx_libraries_status ON libraries(discovery_status);
CREATE INDEX IF NOT EXISTS idx_libraries_next_refresh ON libraries(next_refresh_at)
    WHERE discovery_status = 'active' AND next_refresh_at IS NOT NULL;


-- ── bibliographic_references — known refs, acquired or not ───────────

CREATE TABLE IF NOT EXISTS bibliographic_references (
    reference_id            BIGSERIAL PRIMARY KEY,
    base60_address          TEXT NOT NULL UNIQUE,
    library_id              BIGINT REFERENCES libraries(library_id) ON DELETE SET NULL,
    citation_text           TEXT NOT NULL,
    title                   TEXT,
    authors                 TEXT[],
    publication_year        INTEGER,
    publisher               TEXT,
    reference_type          TEXT,                          -- book / paper / article / foia_release / government_report / unknown
    identifiers             JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {isbn, doi, ol_id, archive_id, fbi_vault_id, url}
    discovered_via          TEXT NOT NULL,                 -- google_books / scholar / wikipedia / archive_org / citation_extracted / manual
    discovery_query         TEXT,
    discovered_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confidence_to_topic     REAL,                          -- relevance 0..1
    acquisition_status      TEXT NOT NULL DEFAULT 'pending'
                              CHECK (acquisition_status IN
                                ('pending','acquired','unavailable','restricted',
                                 'paywall','rejected','ingesting','ingested')),
    acquisition_attempts    INTEGER NOT NULL DEFAULT 0,
    last_attempted_at       TIMESTAMPTZ,
    next_retry_at           TIMESTAMPTZ,
    acquisition_method      TEXT,                          -- which method succeeded (when acquired)
    acquired_url            TEXT,                          -- where we got the PDF from
    file_path               TEXT,                          -- local path of acquired PDF
    file_sha256             TEXT,
    file_size_bytes         BIGINT,
    document_id             BIGINT REFERENCES documents(document_id) ON DELETE SET NULL,
    citing_document_ids     BIGINT[] NOT NULL DEFAULT '{}',
    issue_id                BIGINT,
    error_message           TEXT,
    notes                   TEXT
);

CREATE INDEX IF NOT EXISTS idx_refs_library ON bibliographic_references(library_id);
CREATE INDEX IF NOT EXISTS idx_refs_status ON bibliographic_references(acquisition_status);
CREATE INDEX IF NOT EXISTS idx_refs_pending_retry ON bibliographic_references(next_retry_at)
    WHERE acquisition_status = 'pending';
CREATE INDEX IF NOT EXISTS idx_refs_document ON bibliographic_references(document_id)
    WHERE document_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_refs_via ON bibliographic_references(discovered_via);
CREATE UNIQUE INDEX IF NOT EXISTS uq_refs_dedup
    ON bibliographic_references(library_id,
                                COALESCE(identifiers->>'doi',
                                         identifiers->>'isbn',
                                         identifiers->>'ol_id',
                                         identifiers->>'archive_id',
                                         title));


-- ── library_members — which entities + docs + refs belong here ───────

CREATE TABLE IF NOT EXISTS library_members (
    library_id        BIGINT NOT NULL REFERENCES libraries(library_id) ON DELETE CASCADE,
    member_kind       TEXT NOT NULL CHECK (member_kind IN
                        ('entity','document','reference','assessment','event')),
    member_id         BIGINT NOT NULL,
    member_address    TEXT,
    added_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    added_by          TEXT,
    PRIMARY KEY (library_id, member_kind, member_id)
);

CREATE INDEX IF NOT EXISTS idx_library_members_id ON library_members(member_kind, member_id);


-- ── citation_emissions — provenance: doc D mentions ref R ─────────────

CREATE TABLE IF NOT EXISTS citation_emissions (
    emission_id       BIGSERIAL PRIMARY KEY,
    source_document_id BIGINT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    reference_id       BIGINT NOT NULL REFERENCES bibliographic_references(reference_id) ON DELETE CASCADE,
    page_id            BIGINT,
    paragraph_id       BIGINT,
    citation_text      TEXT,
    citation_format    TEXT,                       -- footnote / inline / bibliography / endnote
    discovered_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_document_id, reference_id, citation_text)
);

CREATE INDEX IF NOT EXISTS idx_cite_emissions_source
    ON citation_emissions(source_document_id);
CREATE INDEX IF NOT EXISTS idx_cite_emissions_ref
    ON citation_emissions(reference_id);


-- ── Convenience views ────────────────────────────────────────────────

-- Library acquisition dashboard: per-library counts at a glance
CREATE OR REPLACE VIEW library_acquisition_status AS
SELECT l.library_id,
       l.canonical_name,
       l.base60_address,
       l.focal_term,
       COUNT(r.reference_id) AS total_refs,
       COUNT(*) FILTER (WHERE r.acquisition_status = 'pending')     AS pending,
       COUNT(*) FILTER (WHERE r.acquisition_status = 'acquired')    AS acquired,
       COUNT(*) FILTER (WHERE r.acquisition_status = 'ingested')    AS ingested,
       COUNT(*) FILTER (WHERE r.acquisition_status = 'unavailable') AS unavailable,
       COUNT(*) FILTER (WHERE r.acquisition_status = 'paywall')     AS paywall,
       COUNT(*) FILTER (WHERE r.acquisition_status = 'restricted')  AS restricted,
       l.discovery_status,
       l.last_refreshed_at,
       l.next_refresh_at
  FROM libraries l
  LEFT JOIN bibliographic_references r ON r.library_id = l.library_id
 GROUP BY l.library_id;


-- Library citation graph: which refs are cited by which acquired docs
CREATE OR REPLACE VIEW library_citation_graph AS
SELECT l.library_id, l.canonical_name AS library_name,
       d.document_id AS citing_doc_id, d.title AS citing_doc_title,
       r.reference_id, r.citation_text, r.acquisition_status,
       e.citation_format, e.page_id, e.paragraph_id
  FROM libraries l
  JOIN library_members lm ON lm.library_id = l.library_id AND lm.member_kind = 'document'
  JOIN documents d        ON d.document_id = lm.member_id
  JOIN citation_emissions e ON e.source_document_id = d.document_id
  JOIN bibliographic_references r ON r.reference_id = e.reference_id;
