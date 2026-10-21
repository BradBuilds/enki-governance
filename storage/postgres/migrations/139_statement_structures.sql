-- 139: record the sentence structure; project the connection from it.
--
-- The earlier design recorded only the connection triple. That triple is a
-- PROJECTION of a sentence, not the sentence, and discarding what produced it
-- costs three things that cannot be recovered later:
--
--   1. You cannot re-resolve. When the substrate improves — and it is about to
--      change entirely, since 72% of the concept layer is licensed and must be
--      replaced — a stored triple cannot be re-derived. The evidence is gone.
--   2. You cannot tell a denial from an assertion. 635 negative-polarity and
--      126 hedged statements would merge into the affirmative count at the same
--      address, destroying exactly the contrast the comparison exists to find.
--   3. You cannot subdivide. Dividing an address when the evidence shows it is
--      doing two jobs is the point of base-60, and the discarded context is the
--      only raw material for seeing that.
--
-- So the structure is the record and the connection is a view of it. In the
-- expansion/contraction frame: the structure is what an address EXPANDS INTO.
-- An address that cannot be expanded back to its evidence cannot refute a
-- hallucination, which is the whole reason the substrate exists.

CREATE TABLE IF NOT EXISTS statement_structures (
    structure_id            BIGSERIAL PRIMARY KEY,
    statement_id            BIGINT NOT NULL UNIQUE,
    tenant_id               TEXT NOT NULL DEFAULT 'default',

    -- who
    agent_entity_id         BIGINT,
    agent_type              TEXT,

    -- the predicate, resolved through the collapse equation with the candidate
    -- set constrained to verbs. park is a real outcome and is recorded as one.
    predicate_surface       TEXT,
    predicate_lemma         TEXT,
    predicate_sense_address TEXT,
    predicate_action        TEXT,          -- collapse | park
    predicate_gloss         TEXT,

    -- how the claim is made. A denial is a DIFFERENT connection, not a
    -- negative count of the same one, so both travel into the projection.
    polarity                TEXT,
    modality                TEXT,

    -- what it points at
    object_entity_id        BIGINT,
    object_type             TEXT,
    object_text             TEXT,

    -- the whole statement mapped, not just the verb: every content term with
    -- its address, or marked parked. This is the raw material for subdivision.
    roles                   JSONB NOT NULL DEFAULT '{}',
    terms                   JSONB NOT NULL DEFAULT '[]',
    terms_resolved          INTEGER,
    terms_total             INTEGER,

    -- provenance and the path back to evidence
    proposition             TEXT,
    document_id             BIGINT,
    library_id              BIGINT,
    source_time             TIMESTAMPTZ,
    event_time              TIMESTAMPTZ,

    -- Anchored-sense structures may federate; parked or provisional ones are
    -- node-local. Flagged at write time rather than inferred later.
    federatable             BOOLEAN NOT NULL DEFAULT FALSE,

    resolver_version        TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_stmt_struct_statement ON statement_structures (statement_id);
CREATE INDEX IF NOT EXISTS idx_stmt_struct_predicate
    ON statement_structures (predicate_sense_address)
    WHERE predicate_sense_address IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_stmt_struct_doc ON statement_structures (document_id);


-- The projection. One row per OBSERVATION, so a count is a query rather than a
-- number someone maintains.
--
-- Deliberately NOT written into concept_relations, which the plan suggested:
-- that table is 96% WordNet ontology and is slated for purge, and mixing
-- corpus-derived connections into it would mean the purge either destroys our
-- own evidence or has to distinguish provenance it does not record.
--
-- The key is content-derived — sha16 over the addressed parts — so two nodes
-- that share no documents mint the SAME key for the same connection and merge
-- by sum. Polarity and modality are inside the key: a denial is a different
-- connection, not a negative count of the same one.
--
-- Endpoints are TYPES, not entity instances. Measured: only 32 collapsible
-- statements have a base60 address at both ends (person entities are 1.7%
-- addressed), so an instance-level projection would cover a rounding error.
-- The type level is also the level the corroboration evidence lives at —
-- person -"come together"-> person across 42 documents and 3 libraries.
-- Instance-level projection waits on entity addressing, as a named
-- prerequisite rather than a silent assumption.
CREATE TABLE IF NOT EXISTS connection_observations (
    observation_id          BIGSERIAL PRIMARY KEY,
    connection_key          CHAR(16) NOT NULL,
    tenant_id               TEXT NOT NULL DEFAULT 'default',

    subject_type            TEXT NOT NULL,
    predicate_sense_address TEXT NOT NULL,
    object_type             TEXT NOT NULL,
    polarity                TEXT NOT NULL,
    modality                TEXT NOT NULL,

    -- every observation resolves back to a structure, which resolves back to
    -- a span. A projection without its structure is never written.
    structure_id            BIGINT NOT NULL REFERENCES statement_structures(structure_id) ON DELETE CASCADE,
    statement_id            BIGINT NOT NULL,
    document_id             BIGINT,
    library_id              BIGINT,

    federatable             BOOLEAN NOT NULL DEFAULT FALSE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (connection_key, statement_id)
);

CREATE INDEX IF NOT EXISTS idx_conn_obs_key ON connection_observations (connection_key);
CREATE INDEX IF NOT EXISTS idx_conn_obs_corroboration
    ON connection_observations (connection_key, document_id);
CREATE INDEX IF NOT EXISTS idx_conn_obs_federatable
    ON connection_observations (connection_key) WHERE federatable;

COMMENT ON TABLE connection_observations IS
    'One row per observed connection. Corroboration is a COUNT OF DISTINCT DOCUMENTS over connection_key, never a stored total — the record carries no authority and frequency is evidence, not permission.';
COMMENT ON COLUMN connection_observations.connection_key IS
    'sha16(subject_type:predicate_sense_address:object_type:polarity:modality). Content-derived, so two nodes sharing no documents mint the same key and merge by sum.';
