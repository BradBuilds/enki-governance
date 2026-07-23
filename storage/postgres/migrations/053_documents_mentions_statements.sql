-- 053: Hierarchical Documents + Mentions + Statements + Issues
--
-- The substrate for the new collapse-aware research model. Adds:
--
--   1. Document hierarchy: documents → sections → pages → paragraphs
--      Lets a 300-page PDF live in the graph as a single document with
--      navigable internal structure. Boilerplate detection runs on
--      paragraphs via SimHash.
--
--   2. Entity mentions: a per-occurrence record of "an entity appears here".
--      Distinct from the canonical entity. A mention can be bound to an
--      entity, unresolved, or disputed. Unresolved mentions are first-class
--      nodes in the graph (not a hidden queue) so analysts can navigate to
--      and resolve them deliberately.
--
--   3. Statements: claims/assertions extracted from documents. Subject,
--      predicate, object — plus who-said-it and where. Backs "who told on
--      who" workflows: a testimony is a statement(speaker, testified_against, target).
--      Reuses the existing assertions infrastructure where applicable but
--      adds the source-document trail.
--
--   4. Issues (collapse_candidate, contradiction, type_conflict,
--      redaction_mark): real graph nodes for ambiguity that needs human
--      decision. Backs the review-queue workflow.
--
-- See:
--   the internal refdata hierarchical collapse plan
--   the internal refdata and research plan

-- ─── Documents ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS documents (
    document_id      BIGSERIAL PRIMARY KEY,
    tenant_id        TEXT NOT NULL DEFAULT 'default',
    dataset_id       TEXT,                            -- e.g. 'example_corpus_2024'
    source_type      TEXT,                            -- 'pdf' / 'html' / 'docx' / 'json' / 'image'
    hash             TEXT UNIQUE,                     -- sha256 of raw content
    title            TEXT,
    docket_number    TEXT,                            -- court-record style identifier
    bates_range      TEXT,                            -- '[0001-0442]'
    jurisdiction     TEXT,
    publisher        TEXT,
    author           TEXT,
    release_date     DATE,
    document_date    DATE,                            -- date OF the document content (not release)
    page_count       INT,
    word_count       INT,
    ocr_quality      REAL,                            -- aggregate OCR confidence 0..1
    extraction_method TEXT,                           -- 'native' / 'ocr_paddle' / 'ocr_tesseract' / 'llm_vision' / 'dual'
    source_url       TEXT,
    on_disk_path     TEXT,
    metadata         JSONB DEFAULT '{}'::jsonb,
    trust_class      TEXT DEFAULT 'mentioned',        -- per dataset_policy
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_refreshed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_documents_tenant_dataset
    ON documents(tenant_id, dataset_id);
CREATE INDEX IF NOT EXISTS idx_documents_release_date
    ON documents(release_date DESC);
CREATE INDEX IF NOT EXISTS idx_documents_docket
    ON documents(docket_number) WHERE docket_number IS NOT NULL;

CREATE TABLE IF NOT EXISTS document_sections (
    section_id       BIGSERIAL PRIMARY KEY,
    document_id      BIGINT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    parent_section_id BIGINT REFERENCES document_sections(section_id),
    kind             TEXT,                            -- 'chapter' / 'section' / 'appendix' / 'exhibit'
    heading          TEXT,
    order_index      INT NOT NULL,
    start_page       INT,
    end_page         INT,
    metadata         JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_sections_doc ON document_sections(document_id, order_index);
CREATE INDEX IF NOT EXISTS idx_sections_parent ON document_sections(parent_section_id);

CREATE TABLE IF NOT EXISTS document_pages (
    page_id          BIGSERIAL PRIMARY KEY,
    document_id      BIGINT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    section_id       BIGINT REFERENCES document_sections(section_id),
    page_number      INT NOT NULL,
    raw_text         TEXT,
    raw_text_llm     TEXT,                            -- second-path extraction for comparator
    ocr_confidence   REAL,
    extraction_method TEXT,                           -- per-page in case dual fires differently
    has_redactions   BOOLEAN DEFAULT FALSE,
    on_disk_image    TEXT,                            -- path to rendered page image if applicable
    metadata         JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_pages_doc ON document_pages(document_id, page_number);

CREATE TABLE IF NOT EXISTS document_paragraphs (
    paragraph_id     BIGSERIAL PRIMARY KEY,
    page_id          BIGINT NOT NULL REFERENCES document_pages(page_id) ON DELETE CASCADE,
    document_id      BIGINT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    order_index      INT NOT NULL,
    raw_text         TEXT,
    simhash          BIGINT,                          -- for boilerplate cluster detection
    template_phrase_id BIGINT,                        -- non-null if this paragraph is boilerplate
    is_boilerplate   BOOLEAN DEFAULT FALSE,
    is_redacted      BOOLEAN DEFAULT FALSE,
    word_count       INT,
    metadata         JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_paragraphs_page ON document_paragraphs(page_id, order_index);
CREATE INDEX IF NOT EXISTS idx_paragraphs_doc ON document_paragraphs(document_id);
CREATE INDEX IF NOT EXISTS idx_paragraphs_simhash ON document_paragraphs(simhash) WHERE NOT is_boilerplate;
CREATE INDEX IF NOT EXISTS idx_paragraphs_template ON document_paragraphs(template_phrase_id) WHERE template_phrase_id IS NOT NULL;

-- Template phrase clusters — boilerplate detection output
CREATE TABLE IF NOT EXISTS template_phrases (
    template_id      BIGSERIAL PRIMARY KEY,
    corpus_id        TEXT NOT NULL,                   -- 'example_corpus_2024' / 'aaro_reports' / etc.
    representative_text TEXT,
    simhash          BIGINT,
    occurrence_count INT,
    document_count   INT,
    classification   TEXT,                            -- 'boilerplate' / 'recurring' / 'signal'
    suppressed       BOOLEAN DEFAULT TRUE,            -- entity extraction skips this when true
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_templates_corpus ON template_phrases(corpus_id);
CREATE INDEX IF NOT EXISTS idx_templates_simhash ON template_phrases(simhash);

-- ─── Entity mentions ────────────────────────────────────────────────────
-- Per-occurrence record. A mention is a thing-in-a-document; an entity is
-- the canonical identity behind one or more mentions. Mentions stay even
-- after resolution so we never lose the span-level provenance.

CREATE TABLE IF NOT EXISTS document_mentions (
    mention_id       BIGSERIAL PRIMARY KEY,
    tenant_id        TEXT NOT NULL DEFAULT 'default',
    document_id      BIGINT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    page_id          BIGINT REFERENCES document_pages(page_id) ON DELETE CASCADE,
    paragraph_id     BIGINT REFERENCES document_paragraphs(paragraph_id) ON DELETE SET NULL,
    char_start       INT,
    char_end         INT,
    surface_form     TEXT NOT NULL,                   -- as-written in source
    surface_form_norm TEXT,                           -- normalized for indexing
    suggested_type   TEXT,                            -- entity-type guess from extractor
    candidate_entity_id BIGINT REFERENCES entities(entity_id) ON DELETE SET NULL,
    confidence       REAL,
    resolution_status TEXT NOT NULL DEFAULT 'unresolved',  -- bound / unresolved / disputed / rejected
    candidates       JSONB,                           -- [{entity_id, score, reason}, ...] when disputed
    coref_to_mention_id BIGINT REFERENCES document_mentions(mention_id) ON DELETE SET NULL,
    extraction_method TEXT,                           -- 'ocr_only' / 'llm_only' / 'both_agree' / 'both_partial' / 'contradicted'
    extraction_engines JSONB,                         -- {ocr: 'paddle_2.6', llm: 'gemma-4'}
    review_status    TEXT,                            -- null / 'pending' / 'reviewed' / 'auto_resolved'
    reviewed_by      TEXT,
    reviewed_at      TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mentions_entity ON document_mentions(candidate_entity_id);
CREATE INDEX IF NOT EXISTS idx_mentions_doc ON document_mentions(document_id);
CREATE INDEX IF NOT EXISTS idx_mentions_page ON document_mentions(page_id);
CREATE INDEX IF NOT EXISTS idx_mentions_surface_norm ON document_mentions(surface_form_norm);
CREATE INDEX IF NOT EXISTS idx_mentions_status ON document_mentions(resolution_status) WHERE resolution_status != 'bound';

-- ─── Statements ─────────────────────────────────────────────────────────
-- A claim/assertion made WITHIN a document. Used for "who said what about
-- whom". Distinct from the existing assertions table because statements
-- carry document-trail provenance (page, paragraph, mention spans).
--
-- Statement structure: <speaker_entity> said <predicate> about <object_entity>
-- e.g.
--   speaker=Witness_A, predicate=testified_against, object=Party_B
--   speaker=Doe, predicate=denied, object=allegation
--   speaker=Witness_A, predicate=named_as_associate, object=Doe_3

CREATE TABLE IF NOT EXISTS statements (
    statement_id     BIGSERIAL PRIMARY KEY,
    tenant_id        TEXT NOT NULL DEFAULT 'default',
    document_id      BIGINT REFERENCES documents(document_id) ON DELETE CASCADE,
    page_id          BIGINT REFERENCES document_pages(page_id) ON DELETE SET NULL,
    paragraph_id     BIGINT REFERENCES document_paragraphs(paragraph_id) ON DELETE SET NULL,

    speaker_entity_id BIGINT REFERENCES entities(entity_id),   -- who said it (null if unknown)
    speaker_mention_id BIGINT REFERENCES document_mentions(mention_id),
    predicate        TEXT NOT NULL,                   -- testified_against / accused / alleged / corroborated / denied / claimed_to_know / named_associate / etc.
    object_entity_id BIGINT REFERENCES entities(entity_id),   -- about whom/what (null if statement is general)
    object_mention_id BIGINT REFERENCES document_mentions(mention_id),
    raw_text         TEXT,                            -- the verbatim claim
    summarized_text  TEXT,                            -- normalized form

    -- Modality / certainty
    modality         TEXT,                            -- 'asserted' / 'alleged' / 'denied' / 'hypothesized' / 'questioned'
    confidence       REAL,                            -- extractor's confidence
    polarity         TEXT,                            -- 'affirmative' / 'negative'
    speaker_role     TEXT,                            -- 'witness' / 'defendant' / 'plaintiff' / 'official' / 'journalist'

    -- Cross-linking with the existing assessment node graph (assessment.claim)
    assessment_node_id BIGINT,                        -- if promoted to a canonical claim node
    -- Corroboration / contradiction tracking
    corroborated_by_count INT DEFAULT 0,
    contradicted_by_count INT DEFAULT 0,

    extraction_method TEXT,                           -- 'llm' / 'rule_based' / 'manual'
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_statements_speaker ON statements(speaker_entity_id);
CREATE INDEX IF NOT EXISTS idx_statements_object ON statements(object_entity_id);
CREATE INDEX IF NOT EXISTS idx_statements_doc ON statements(document_id);
CREATE INDEX IF NOT EXISTS idx_statements_predicate ON statements(predicate);

-- ─── Issue nodes ────────────────────────────────────────────────────────
-- First-class nodes for things the system is uncertain about. The graph
-- shows them; the review queue lists them; the analyst resolves them.

-- Collapse candidate: "should entity_a and entity_b merge?"
CREATE TABLE IF NOT EXISTS collapse_candidates (
    candidate_id     BIGSERIAL PRIMARY KEY,
    tenant_id        TEXT NOT NULL DEFAULT 'default',
    entity_a_id      BIGINT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    entity_b_id      BIGINT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    suggested_action TEXT NOT NULL DEFAULT 'review',  -- 'merge' / 'keep_separate' / 'review'
    evidence_for     JSONB,                           -- [{type, weight, detail}, ...]
    evidence_against JSONB,
    score            REAL,                            -- aggregated merge-confidence
    dataset_id       TEXT,                            -- corpus that surfaced this
    status           TEXT NOT NULL DEFAULT 'pending', -- 'pending' / 'merged' / 'rejected' / 'kept_separate'
    decided_by       TEXT,
    decided_at       TIMESTAMPTZ,
    decision_note    TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT no_self_merge CHECK (entity_a_id <> entity_b_id),
    UNIQUE (entity_a_id, entity_b_id, dataset_id)
);

CREATE INDEX IF NOT EXISTS idx_collapse_status ON collapse_candidates(status, score DESC);
CREATE INDEX IF NOT EXISTS idx_collapse_dataset ON collapse_candidates(dataset_id);

-- Contradictions and type-conflicts surfaced by the dual-extraction comparator
-- or by cross-document fact disagreement.
CREATE TABLE IF NOT EXISTS contradictions (
    contradiction_id BIGSERIAL PRIMARY KEY,
    tenant_id        TEXT NOT NULL DEFAULT 'default',
    kind             TEXT NOT NULL,                   -- 'extraction' (OCR vs LLM) / 'fact' (doc A vs doc B) / 'type' / 'statement'
    -- Either-or: reference to the document spans, or to two statements
    document_id      BIGINT REFERENCES documents(document_id) ON DELETE CASCADE,
    page_id          BIGINT REFERENCES document_pages(page_id) ON DELETE SET NULL,
    char_start       INT,
    char_end         INT,
    reading_a        TEXT,                            -- OCR reading or doc-A text
    reading_b        TEXT,                            -- LLM reading or doc-B text
    statement_a_id   BIGINT REFERENCES statements(statement_id) ON DELETE SET NULL,
    statement_b_id   BIGINT REFERENCES statements(statement_id) ON DELETE SET NULL,
    entity_id        BIGINT REFERENCES entities(entity_id) ON DELETE SET NULL,  -- the entity the contradiction is ABOUT
    severity         REAL,                            -- 0..1
    status           TEXT NOT NULL DEFAULT 'open',    -- 'open' / 'resolved' / 'accepted_both' / 'rejected'
    resolved_by      TEXT,
    resolved_at      TIMESTAMPTZ,
    resolution_note  TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_contradictions_status ON contradictions(status, severity DESC);
CREATE INDEX IF NOT EXISTS idx_contradictions_doc ON contradictions(document_id);
CREATE INDEX IF NOT EXISTS idx_contradictions_entity ON contradictions(entity_id);

-- ─── Dataset policies (used by the resolver via collapse_policy) ───────
-- Each ingested dataset has a policy row. The GDU gateway consults it
-- before promoting an entity from LDU to GDU.
CREATE TABLE IF NOT EXISTS dataset_policies (
    dataset_id       TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    trust_class      TEXT NOT NULL,                   -- 'canonical' / 'corroborating' / 'mentioned' / 'review_required'
    merge_threshold  TEXT NOT NULL,                   -- 'identifier_only' / 'name_plus_attr' / 'name_plus_context' / 'review_required'
    required_evidence JSONB,
    promotion_threshold TEXT,                         -- 'auto' / 'two_independent_sources' / 'manual_only'
    sensitivity      JSONB,                           -- {view_role_required, audit_on_view, redaction_aware}
    extraction       JSONB,                           -- {boilerplate_detection, ocr_confidence_min, ...}
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Sensible defaults so anything ingested without an explicit policy lands in
-- a safe class.
INSERT INTO dataset_policies (dataset_id, name, trust_class, merge_threshold, promotion_threshold)
VALUES
    ('_default',  'System default',  'mentioned',        'name_plus_context',  'auto'),
    ('wikidata',  'Wikidata',        'canonical',        'identifier_only',    'auto'),
    ('ofac_sdn',  'OFAC SDN',        'corroborating',    'name_plus_attr',     'auto'),
    ('un_consolidated', 'UN Sanctions', 'corroborating', 'name_plus_attr',     'auto'),
    ('opensanctions_default', 'OpenSanctions Default', 'corroborating', 'name_plus_attr', 'auto'),
    ('gleif',     'GLEIF',           'canonical',        'identifier_only',    'auto'),
    ('sec_edgar', 'SEC EDGAR',       'corroborating',    'identifier_only',    'auto')
ON CONFLICT (dataset_id) DO NOTHING;

-- ─── Surface form tracking (per-entity alias frequency across corpora) ──
CREATE TABLE IF NOT EXISTS entity_surface_forms (
    entity_id        BIGINT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    surface_form     TEXT NOT NULL,
    surface_form_norm TEXT NOT NULL,
    occurrences      INT NOT NULL DEFAULT 0,
    first_seen_dataset TEXT,
    first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    tagged_as_alias  BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (entity_id, surface_form_norm)
);

CREATE INDEX IF NOT EXISTS idx_surfaceforms_norm ON entity_surface_forms(surface_form_norm);

-- ─── Corpus term frequency (for boilerplate / signal classification) ───
CREATE TABLE IF NOT EXISTS corpus_term_frequency (
    corpus_id        TEXT NOT NULL,
    term             TEXT NOT NULL,
    n                INT,                             -- n-gram size (1..5)
    document_count   INT,
    total_occurrences INT,
    document_freq    REAL,                            -- fraction of corpus docs containing
    classification   TEXT,                            -- 'boilerplate' / 'recurring' / 'signal' / 'rare'
    last_updated     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (corpus_id, term, n)
);

CREATE INDEX IF NOT EXISTS idx_corpus_terms_class
    ON corpus_term_frequency(corpus_id, classification, document_freq DESC);
