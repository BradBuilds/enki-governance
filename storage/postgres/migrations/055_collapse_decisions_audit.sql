-- 055: Collapse decisions audit + co-occurrence index
--
-- Two new tables that make node collapse trustworthy:
--
--   collapse_decisions    every merge / create / split / keep_separate decision
--                         gets a row recording WHY it was made, what evidence
--                         supported it, what evidence contradicted it, who
--                         decided (auto or human), and whether it's reversible.
--                         Without this audit trail, no collapse is defensible —
--                         you can't tell why "ALEX" was matched to "Alex Young"
--                         or how to undo it.
--
--   entity_co_occurrences for each pair of entities that appeared together on
--                         the same page or paragraph, count + provenance. This
--                         is the key signal for disambiguating common-name
--                         surface forms ("Alex" co-occurring with {Doe,
--                         Acme} is a different Alex than "Alex" co-occurring
--                         with {Project Blue Book, Wright-Patterson}).
--
-- See:
--   the internal refdata hierarchical collapse plan

-- ─── Drift backfill: tables earlier migrations forgot to create ─────────
-- entity_external_lookups was historically created out-of-band on the
-- production DB; this migration first references it via FK from
-- collapse_decisions.external_lookup_id. Recreate it here so fresh-DB
-- bootstrap works. Migrations 057, 058 ALTER it further; both are
-- idempotent on this shape.
CREATE TABLE IF NOT EXISTS entity_external_lookups (
    lookup_id         BIGSERIAL PRIMARY KEY,
    corpus_id         TEXT NOT NULL,
    surface_form_norm TEXT NOT NULL,
    source            TEXT NOT NULL,
    external_id       TEXT,
    label             TEXT,
    summary           TEXT,
    url               TEXT,
    matched           BOOLEAN NOT NULL DEFAULT FALSE,
    confidence        REAL,
    raw               JSONB,
    looked_up_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    cluster_id        BIGINT
);

CREATE INDEX IF NOT EXISTS idx_external_corpus
    ON entity_external_lookups (corpus_id, matched);
CREATE INDEX IF NOT EXISTS idx_external_qid
    ON entity_external_lookups (external_id)
    WHERE external_id IS NOT NULL;

-- ─── Collapse decisions audit ───────────────────────────────────────────

CREATE TABLE IF NOT EXISTS collapse_decisions (
    decision_id        BIGSERIAL PRIMARY KEY,
    tenant_id          TEXT NOT NULL DEFAULT 'default',
    -- Target of the decision
    entity_id          BIGINT,                       -- canonical entity affected
    surface_form_norm  TEXT,                         -- the input form being resolved
    corpus_id          TEXT,                         -- which corpus the form came from
    -- What was decided
    action             TEXT NOT NULL,                -- 'created' | 'merged_into' |
                                                     -- 'kept_separate' | 'split_from' |
                                                     -- 'collapse_candidate' | 'rejected'
    merged_into_entity_id BIGINT,                    -- only set when action='merged_into'
    split_from_entity_id  BIGINT,                    -- only set when action='split_from'
    -- Why
    rule_fired         TEXT,                         -- 'identifier_match' |
                                                     -- 'name+type+cooccur' |
                                                     -- 'name_only_below_threshold' |
                                                     -- 'short_surface_form_blocked' | etc.
    evidence_for       JSONB,                        -- [{axis, value, weight, source_id}, ...]
    evidence_against   JSONB,                        -- contradicting evidence
    score              REAL,                         -- aggregated collapse score 0..1
    confidence_tier    TEXT,                         -- 'hard'/'soft'/'suggested'/'no'
    -- Where the evidence came from
    source_mentions    BIGINT[],                     -- doc_mention IDs that drove this
    external_lookup_id BIGINT REFERENCES entity_external_lookups(lookup_id) ON DELETE SET NULL,
    -- Who decided
    decided_by         TEXT NOT NULL,                -- 'auto:resolver_v3' | 'human:analyst' | etc.
    decided_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Lifecycle
    reversible         BOOLEAN NOT NULL DEFAULT TRUE,
    reverted_at        TIMESTAMPTZ,
    reverted_by        TEXT,
    notes              TEXT
);

CREATE INDEX IF NOT EXISTS idx_collapse_decisions_entity ON collapse_decisions(entity_id);
CREATE INDEX IF NOT EXISTS idx_collapse_decisions_form ON collapse_decisions(corpus_id, surface_form_norm);
CREATE INDEX IF NOT EXISTS idx_collapse_decisions_action ON collapse_decisions(action, decided_at DESC);
CREATE INDEX IF NOT EXISTS idx_collapse_decisions_unreviewed
    ON collapse_decisions(decided_at DESC)
    WHERE confidence_tier = 'suggested' AND reverted_at IS NULL;

-- ─── Entity co-occurrence index ─────────────────────────────────────────
-- For each pair of entities (entity_a, entity_b) that appeared on the same
-- paragraph, page, or document, count + provenance. Used by the multi-signal
-- resolver to disambiguate common surface forms via shared context.

CREATE TABLE IF NOT EXISTS entity_co_occurrences (
    entity_a_id        BIGINT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    entity_b_id        BIGINT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    -- Granularity of co-occurrence (stronger to weaker)
    paragraph_count    INT NOT NULL DEFAULT 0,       -- same paragraph
    page_count         INT NOT NULL DEFAULT 0,       -- same page (any paragraph)
    document_count     INT NOT NULL DEFAULT 0,       -- same document (any page)
    -- Aggregate score: paragraph >> page >> document
    cooccur_score      REAL NOT NULL DEFAULT 0,      -- 3*paragraph + page + 0.1*doc
    -- Sample provenance — pointer to one example doc/page/paragraph
    sample_document_id BIGINT,
    sample_page_id     BIGINT,
    sample_paragraph_id BIGINT,
    last_seen_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (entity_a_id, entity_b_id),
    CHECK (entity_a_id < entity_b_id)               -- canonical ordering (avoid duplicate (a,b)/(b,a))
);

CREATE INDEX IF NOT EXISTS idx_co_occur_a ON entity_co_occurrences(entity_a_id, cooccur_score DESC);
CREATE INDEX IF NOT EXISTS idx_co_occur_b ON entity_co_occurrences(entity_b_id, cooccur_score DESC);

-- ─── Wikidata type cache ────────────────────────────────────────────────
-- Per-QID, what is it an "instance of" (P31 in Wikidata)? Cached so we don't
-- re-fetch on every source-validation call. Used to filter out wrong-type
-- Wikipedia matches (e.g., reject "From (TV series)" when looking for a
-- person in a document).

CREATE TABLE IF NOT EXISTS wikidata_type_cache (
    wikidata_qid       TEXT PRIMARY KEY,
    instance_of_qids   TEXT[],                       -- e.g. ['Q5'] for human
    instance_of_labels TEXT[],                       -- e.g. ['human']
    -- Convenience flags for the most common filters
    is_human           BOOLEAN DEFAULT FALSE,        -- Q5
    is_organization    BOOLEAN DEFAULT FALSE,        -- Q43229, Q4830453, etc.
    is_place           BOOLEAN DEFAULT FALSE,        -- Q515 city, Q6256 country, etc.
    is_aircraft        BOOLEAN DEFAULT FALSE,        -- Q11436
    is_vessel          BOOLEAN DEFAULT FALSE,        -- Q11446
    is_creative_work   BOOLEAN DEFAULT FALSE,        -- Q17537576 (TV series, films, books, etc.)
    is_event           BOOLEAN DEFAULT FALSE,
    raw                JSONB,
    fetched_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_wd_cache_human ON wikidata_type_cache(is_human) WHERE is_human;
CREATE INDEX IF NOT EXISTS idx_wd_cache_org ON wikidata_type_cache(is_organization) WHERE is_organization;
CREATE INDEX IF NOT EXISTS idx_wd_cache_place ON wikidata_type_cache(is_place) WHERE is_place;
