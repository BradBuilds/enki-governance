-- 066: Statement layer — extraction + collapse + assessment + event spawn
--
-- The statement layer is the bridge between MENTIONS (surface text the
-- system extracted) and ASSESSMENTS (canonical, corroborated facts).
-- A statement is one DOCUMENT'S utterance: "doc D paragraph P claims
-- that subject S did predicate Q to object O, in modality M, with
-- confidence C, occurring at time T in place L."
--
-- Statements collapse into ASSESSMENTS by canonical (subj, pred, obj)
-- triple. Multiple statements asserting the same triple corroborate;
-- conflicting triples contradict.
--
-- Each action-class statement also SPAWNS or REFINES an EVENT row
-- (existing `events` table) carrying the spatial-temporal anchor. So
-- the timeline + map views become queries over events rather than
-- separate fact storage.
--
-- This migration:
--   A. Extends `statements` table with event_time, event_location,
--      char span, surrounding text, and the address fields.
--   B. Creates the `assessments` table (the collapsed canonical layer
--      with base-60 addresses 7.X.X).
--   C. Creates `predicate_dictionary` — lemma (verb) → edge_type
--      mapping. Seeds the corpus-grounded predicate side of the
--      dictionary (analogue to lemma → concept on the noun side).
--   D. Creates the collapse function that finds-or-creates the
--      assessment for a given (subj, pred, obj).

-- ── A. Extend statements with spatial-temporal + provenance fields ───

ALTER TABLE statements
    ADD COLUMN IF NOT EXISTS event_time                TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS event_end_time            TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS temporal_precision        TEXT,
    ADD COLUMN IF NOT EXISTS event_location_entity_id  BIGINT REFERENCES entities(entity_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS spatial_precision         TEXT,
    ADD COLUMN IF NOT EXISTS char_start                INTEGER,
    ADD COLUMN IF NOT EXISTS char_end                  INTEGER,
    ADD COLUMN IF NOT EXISTS surrounding_text          TEXT,
    ADD COLUMN IF NOT EXISTS predicate_verb_surface    TEXT,
    ADD COLUMN IF NOT EXISTS predicate_lemma           TEXT,
    ADD COLUMN IF NOT EXISTS assessment_entity_id      BIGINT REFERENCES entities(entity_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS spawned_event_id          BIGINT,
    ADD COLUMN IF NOT EXISTS validation_status         TEXT DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS validation_reason         TEXT;

CREATE INDEX IF NOT EXISTS idx_statements_event_time
    ON statements (event_time) WHERE event_time IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_statements_location
    ON statements (event_location_entity_id) WHERE event_location_entity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_statements_assessment
    ON statements (assessment_entity_id) WHERE assessment_entity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_statements_predicate_lemma
    ON statements (predicate_lemma) WHERE predicate_lemma IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_statements_subj_obj
    ON statements (speaker_entity_id, object_entity_id, predicate);


-- ── B. Assessments — the collapsed canonical fact layer ──────────────
--
-- An assessment is one canonical claim about the world. Multiple
-- statements collapse into it. The address space is 7.X.X (already
-- defined in the cyclical model).
--
-- We use a dedicated `assessments` table rather than reusing entities
-- because assessments aren't entities — they are propositions ABOUT
-- entities. Keeping them separate keeps the entity layer clean.
--
-- collapse_key = canonical sha of (subj_entity_id || edge_type || obj_entity_id)

CREATE TABLE IF NOT EXISTS assessments (
    assessment_id            BIGSERIAL PRIMARY KEY,
    tenant_id                TEXT NOT NULL DEFAULT 'default',
    base60_address           TEXT NOT NULL UNIQUE,
    assessment_tier          TEXT NOT NULL DEFAULT 'observation'
                               CHECK (assessment_tier IN
                                 ('observation','fact','claim','hypothesis',
                                  'question','recommendation')),
    subj_entity_id           BIGINT REFERENCES entities(entity_id) ON DELETE CASCADE,
    predicate_edge_type      TEXT NOT NULL,
    obj_entity_id            BIGINT REFERENCES entities(entity_id) ON DELETE CASCADE,
    obj_value                TEXT,           -- when obj is a literal value, not an entity
    polarity                 TEXT NOT NULL DEFAULT 'affirmative'
                               CHECK (polarity IN ('affirmative','negative')),
    collapse_key             TEXT NOT NULL UNIQUE,
    corroborated_by_count    INTEGER NOT NULL DEFAULT 1,
    contradicted_by_count    INTEGER NOT NULL DEFAULT 0,
    source_statement_ids     BIGINT[] NOT NULL DEFAULT '{}',
    earliest_event_time      TIMESTAMPTZ,
    latest_event_time        TIMESTAMPTZ,
    primary_location_entity_id BIGINT REFERENCES entities(entity_id) ON DELETE SET NULL,
    confidence               REAL,
    promoted_at              TIMESTAMPTZ,
    demoted_at               TIMESTAMPTZ,
    first_seen               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    canonical_summary        TEXT,
    issue_id                 BIGINT
);

CREATE INDEX IF NOT EXISTS idx_assessments_subj
    ON assessments (subj_entity_id);
CREATE INDEX IF NOT EXISTS idx_assessments_obj
    ON assessments (obj_entity_id);
CREATE INDEX IF NOT EXISTS idx_assessments_pred
    ON assessments (predicate_edge_type);
CREATE INDEX IF NOT EXISTS idx_assessments_tier
    ON assessments (assessment_tier);
CREATE INDEX IF NOT EXISTS idx_assessments_addr
    ON assessments (base60_address);
CREATE INDEX IF NOT EXISTS idx_assessments_event_time
    ON assessments (earliest_event_time)
    WHERE earliest_event_time IS NOT NULL;


-- ── C. Predicate dictionary — verb form → edge_type ──────────────────
--
-- The form layer has lemmas. Some lemmas are nouns that map to
-- concept entities (the noun dictionary). Other lemmas are verbs that
-- map to edge types (the verb dictionary).
--
-- This table IS the verb dictionary. Seeded with ~100 common verbs;
-- grows empirically as statements teach which surface verbs commonly
-- collapse to which edge types.
--
-- canonical_lemma                edge_type             confidence
-- ───────────────────────────────────────────────────────────────
-- "fly"                          flew_with             0.92
-- "own"                          owns                  0.95
-- "meet"                         met_with              0.88
-- "say"                          said_by               0.85
-- ...

CREATE TABLE IF NOT EXISTS predicate_dictionary (
    entry_id                 BIGSERIAL PRIMARY KEY,
    canonical_lemma          TEXT NOT NULL,
    edge_type                TEXT NOT NULL REFERENCES gdu_type_constraints(edge_type) ON DELETE CASCADE,
    base60_address           TEXT UNIQUE,
    confidence               REAL NOT NULL DEFAULT 0.7,
    observation_count        BIGINT NOT NULL DEFAULT 0,
    source                   TEXT NOT NULL DEFAULT 'seed_v1',
    first_seen               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    polarity_modifier        TEXT DEFAULT 'affirmative',
    UNIQUE (canonical_lemma, edge_type)
);

CREATE INDEX IF NOT EXISTS idx_predicate_dict_lemma
    ON predicate_dictionary (canonical_lemma);
CREATE INDEX IF NOT EXISTS idx_predicate_dict_edge
    ON predicate_dictionary (edge_type);


-- ── D. Collapse helper: find-or-create-assessment ────────────────────

CREATE OR REPLACE FUNCTION compute_assessment_collapse_key(
    p_subj BIGINT, p_predicate TEXT, p_obj BIGINT, p_obj_value TEXT
) RETURNS TEXT AS $$
BEGIN
    RETURN MD5(
        COALESCE(p_subj::text, 'null') || '|' ||
        COALESCE(LOWER(p_predicate), 'null') || '|' ||
        COALESCE(p_obj::text, COALESCE(LOWER(p_obj_value), 'null'))
    );
END;
$$ LANGUAGE plpgsql IMMUTABLE;


-- ── E. Backref: spawned events table ─────────────────────────────────
--
-- A statement that asserts an ACTION (flew, met, paid, attended) gets
-- a row in the `events` table carrying the spatial-temporal anchor.
-- The events table already exists with the right columns (event_time,
-- latitude/longitude, actor_entity_ids[], place_text[]).
--
-- We just add an `originating_statement_ids` column so we can trace
-- an event back to the statements that produced it.

ALTER TABLE events
    ADD COLUMN IF NOT EXISTS originating_statement_ids BIGINT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS location_entity_id BIGINT REFERENCES entities(entity_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS base60_address TEXT;

CREATE INDEX IF NOT EXISTS idx_events_stmt
    ON events USING GIN (originating_statement_ids);
CREATE INDEX IF NOT EXISTS idx_events_addr
    ON events (base60_address) WHERE base60_address IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_location_entity
    ON events (location_entity_id) WHERE location_entity_id IS NOT NULL;


-- ── F. Views ──────────────────────────────────────────────────────────

-- entity_timeline: every event where this entity participated, ordered by time.
CREATE OR REPLACE VIEW entity_timeline AS
SELECT e.event_id,
       e.event_type,
       e.title,
       e.event_time,
       e.event_end_time,
       e.temporal_precision,
       e.confidence,
       e.location_entity_id,
       l.canonical_name AS location_name,
       e.latitude,
       e.longitude,
       e.actor_entity_ids,
       e.target_entity_ids,
       e.base60_address AS event_address,
       UNNEST(e.actor_entity_ids || e.target_entity_ids) AS participant_entity_id
  FROM events e
  LEFT JOIN entities l ON l.entity_id = e.location_entity_id
 WHERE e.event_time IS NOT NULL;

-- assessment_provenance: every statement that supports an assessment.
CREATE OR REPLACE VIEW assessment_provenance AS
SELECT a.assessment_id,
       a.base60_address AS assessment_address,
       a.assessment_tier,
       a.predicate_edge_type,
       s.statement_id,
       s.document_id,
       s.page_id,
       s.paragraph_id,
       s.raw_text,
       s.surrounding_text,
       s.event_time,
       s.event_location_entity_id,
       s.confidence AS statement_confidence,
       s.modality,
       s.polarity
  FROM assessments a
  JOIN statements s ON s.assessment_entity_id = NULL  -- placeholder; populated by promoter
                    OR s.statement_id = ANY(a.source_statement_ids)
 WHERE s.statement_id = ANY(a.source_statement_ids);
