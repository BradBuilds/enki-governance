-- 137: stop the two silent losses in statement extraction, and make place
--      resolution a comparison rather than a lookup.
--
-- LOSS 1 — the object text. The extractor computed the object string, used it
-- to look up a mention, and then dropped it. When the lookup failed the claim
-- was discarded outright; when it succeeded the surface form was still gone,
-- so nothing downstream could re-resolve it after the substrate improved.
--
-- LOSS 2 — the place name. _persist_statement passed location_text into the
-- surrounding_text column, so 3,256 statements carry a place name in a field
-- named for something else, event_location_entity_id is NULL on every row,
-- and the UI renders a bare place name as if it were sentence context.
--
-- WHY A COMPARISON TABLE AND NOT A RESOLVER. Matching a place name against
-- entities by name alone binds 208 rows to a non-place: 134 to organizations,
-- 68 to PEOPLE. The name is not enough, and no single source is either — the
-- governed gazetteerpack resolves 39% of these rows, the entity graph 79%,
-- and they disagree in both directions. So each observer records what it saw
-- and the binding is only made where they agree. A disagreement is kept as a
-- finding, not overwritten by whichever observer ran last.
--
-- Additive: no column is dropped and no existing value is rewritten by this
-- migration. The 3,256-row move out of surrounding_text is done by an audited
-- backfill script, not here.

ALTER TABLE statements ADD COLUMN IF NOT EXISTS object_text   TEXT;
ALTER TABLE statements ADD COLUMN IF NOT EXISTS location_text TEXT;

COMMENT ON COLUMN statements.object_text IS
    'The object as the extractor produced it. Kept even when it resolves to no mention, so the claim can be re-resolved when the substrate improves.';
COMMENT ON COLUMN statements.location_text IS
    'The place name as the extractor produced it. Was misfiled into surrounding_text until migration 137.';

CREATE INDEX IF NOT EXISTS idx_statements_location_text
    ON statements (lower(btrim(location_text)))
    WHERE location_text IS NOT NULL;


-- One row per (place name, observer). Observers are peers: none is the
-- arbiter, and an external one may only ever record a verdict — never
-- coordinates or any other content that would put licensed data in the
-- substrate.
CREATE TABLE IF NOT EXISTS place_name_observations (
    observation_id  BIGSERIAL PRIMARY KEY,
    name_norm       TEXT NOT NULL,          -- lower(btrim(name))
    observer        TEXT NOT NULL,          -- entity_graph | gazetteer_pack | external:<name>
    -- What this observer says the name is. 'place' / 'not_a_place' /
    -- 'unknown' — unknown is a real answer and is recorded as one.
    verdict         TEXT NOT NULL,
    entity_id       BIGINT,                 -- only observers that can bind
    canonical_type  TEXT,
    lat             DOUBLE PRECISION,       -- never populated by an external observer
    lng             DOUBLE PRECISION,
    detail          JSONB NOT NULL DEFAULT '{}',
    observer_version TEXT,                  -- pack version, graph snapshot, api version
    observed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (name_norm, observer)
);

CREATE INDEX IF NOT EXISTS idx_place_obs_name ON place_name_observations (name_norm);
CREATE INDEX IF NOT EXISTS idx_place_obs_disagreement
    ON place_name_observations (name_norm) WHERE verdict = 'not_a_place';

COMMENT ON TABLE place_name_observations IS
    'What each observer says about a place name. Peers, not a hierarchy: the binding is made where they agree, and disagreement is recorded as a finding rather than resolved.';
COMMENT ON COLUMN place_name_observations.lat IS
    'Governed observers only. An external reference returns a verdict; its coordinates must never enter the substrate (plan decision 5).';
