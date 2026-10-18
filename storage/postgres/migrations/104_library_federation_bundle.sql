-- Migration 104: Library federation bundle schema — peer-side additions
--
-- This is Phase 1 of the Library Federation plan
-- (the internal library federation plan).
--
-- DEPLOY TARGET: peer nodes (node-A, node-B, etc.), NOT the cloud hub.
-- The cloud hub's schema lives in hub_schema.sql / migration 103.
--
-- This migration is the substrate that Phase 2 (envelope v2 publisher) and
-- Phase 3 (extraction_subscriber with 10-gate verification + entity resolver)
-- write against. Without it:
--   • the subscriber cannot record which envelope a row was pulled from
--     (no provenance → no audit trail for cross-node poisoning attempts)
--   • the entity resolver cannot dedupe pulled entities against local ones
--     (no canonical_entity_key index → O(N) name matching, FK skew lurks)
--   • libraries cannot identify themselves across the federation
--     (no federation_id → no way to know "your Egypt library == my Egypt
--      library" when both have local BIGINT library_id = 1)
--
-- ADDITIVE ONLY. Every change is a new column or new table; nothing
-- existing is modified or dropped. Safe to re-run.

-- ─── 1. documents.extraction_state ────────────────────────────────────────
-- Tracks where this doc is in the extract-it pipeline so the local LLM and
-- the federation subscriber don't both fire on the same doc.
--
-- Enum by convention (not CHECK constraint — keeps the column cheap to
-- update from multiple code paths):
--   NULL                    | never seen by extraction pipeline
--   'pending_local'         | queued for local LLM extraction
--   'claimed_by_peer'       | another federation peer is extracting (Phase 4)
--   'pulled_from_network'   | applied a peer's envelope; skip local LLM
--   'local_running'         | local LLM extraction in progress
--   'local_complete'        | local LLM extraction done; envelope publishable
--   'quarantine'            | pulled envelope failed verification gates

ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS extraction_state TEXT;

CREATE INDEX IF NOT EXISTS idx_documents_extraction_state
    ON documents (extraction_state)
    WHERE extraction_state IS NOT NULL;


-- ─── 2. entities.canonical_entity_key + pull provenance ───────────────────
-- canonical_entity_key = sha256(lower(canonical_name) | lower(canonical_type))
-- with whitespace normalized (single space, leading/trailing trimmed).
--
-- Matches core/federation/entity_publisher.py:canonical_key() byte-for-byte
-- so two peers compute the same key from the same (name, type) tuple.
-- This is the cross-node identity primitive that makes pulled-entity
-- resolution deterministic instead of fuzzy-matching surface forms.

ALTER TABLE entities
    ADD COLUMN IF NOT EXISTS canonical_entity_key  CHAR(64),
    ADD COLUMN IF NOT EXISTS pulled_from_node_id   TEXT;

-- Backfill canonical_entity_key for existing rows. Uses Postgres' built-in
-- sha256() (no pgcrypto extension needed — sha256(bytea) ships in core
-- from Postgres 11+).
UPDATE entities
   SET canonical_entity_key = encode(
       sha256((
           trim(regexp_replace(lower(coalesce(canonical_name,'')), '\s+', ' ', 'g'))
           || '|'
           || trim(lower(coalesce(canonical_type,'')))
       )::bytea),
       'hex'
   )
 WHERE canonical_entity_key IS NULL
   AND canonical_name IS NOT NULL
   AND canonical_name <> '';

-- The canonical key is the dedupe handle for cross-node pulls. Ideally
-- this would be a UNIQUE index — but node-A's existing entities table has
-- pre-existing duplicates (e.g. 5x "United States / country" entities at
-- entity_id 1, 1429, 1508, 1614, 2103) that pre-date Phase 0 federation
-- work. Those are real GDU dedupe misses that need merging in a
-- dedicated cleanup pass (see TODO below) before UNIQUE can apply.
--
-- TODO(federation-dedupe): write a merge worker that, per canonical_entity_key
-- group, picks the highest-signal_count row as winner, repoints every FK
-- (entity_mentions, statements speaker/object, assessments subj/obj,
-- entity_relationships source/target, entity_surface_forms, etc.) to the
-- winner, deletes losers, then tightens this index to UNIQUE. Until that
-- lands, Phase 3 subscriber's resolver picks the highest-signal_count
-- matching row when multiple rows share a canonical_entity_key.
--
-- Partial index because entities without a populated name (e.g. unresolved
-- mention placeholders) legitimately have NULL canonical_entity_key.
CREATE INDEX IF NOT EXISTS idx_entities_canonical_key
    ON entities (tenant_id, canonical_entity_key)
    WHERE canonical_entity_key IS NOT NULL;

COMMENT ON COLUMN entities.canonical_entity_key IS
    'Cross-node identity hash: sha256(lower(canonical_name) | lower(canonical_type)). '
    'Same key on two peers = same real-world entity. Used by Phase 3 '
    'subscriber to resolve pulled entity references to local entity_id '
    'without trusting the producer''s local PK space.';

COMMENT ON COLUMN entities.pulled_from_node_id IS
    'NULL for locally-created entities. Set to the producer peer''s node_id '
    'when this entity row was first created from a pulled extraction envelope. '
    'Combined with [[entity_relationships]] pulled_from_* this lets analysts '
    'see which slice of the graph came from which federation peer.';


-- ─── 3. Pull provenance on every extraction layer ─────────────────────────
-- Every row that subscribers insert from a pulled envelope is stamped with
-- the producer node_id and the envelope sha256 it came from. Lets us:
--   • audit what got pulled when (analyst question: "where did this fact
--     come from?")
--   • forensically trace a poisoning attempt (DB query: "show me every
--     mention that came from envelope X")
--   • bulk-undo a bad envelope (DELETE WHERE pulled_from_envelope_sha = X)

ALTER TABLE document_mentions
    ADD COLUMN IF NOT EXISTS pulled_from_node_id      TEXT,
    ADD COLUMN IF NOT EXISTS pulled_from_envelope_sha CHAR(64);
CREATE INDEX IF NOT EXISTS idx_document_mentions_pulled_envelope
    ON document_mentions (pulled_from_envelope_sha)
    WHERE pulled_from_envelope_sha IS NOT NULL;

ALTER TABLE entity_mentions
    ADD COLUMN IF NOT EXISTS pulled_from_node_id      TEXT,
    ADD COLUMN IF NOT EXISTS pulled_from_envelope_sha CHAR(64);
CREATE INDEX IF NOT EXISTS idx_entity_mentions_pulled_envelope
    ON entity_mentions (pulled_from_envelope_sha)
    WHERE pulled_from_envelope_sha IS NOT NULL;

ALTER TABLE statements
    ADD COLUMN IF NOT EXISTS pulled_from_node_id      TEXT,
    ADD COLUMN IF NOT EXISTS pulled_from_envelope_sha CHAR(64);
CREATE INDEX IF NOT EXISTS idx_statements_pulled_envelope
    ON statements (pulled_from_envelope_sha)
    WHERE pulled_from_envelope_sha IS NOT NULL;

ALTER TABLE assessments
    ADD COLUMN IF NOT EXISTS pulled_from_node_id      TEXT,
    ADD COLUMN IF NOT EXISTS pulled_from_envelope_sha CHAR(64);
CREATE INDEX IF NOT EXISTS idx_assessments_pulled_envelope
    ON assessments (pulled_from_envelope_sha)
    WHERE pulled_from_envelope_sha IS NOT NULL;

ALTER TABLE entity_relationships
    ADD COLUMN IF NOT EXISTS pulled_from_node_id      TEXT,
    ADD COLUMN IF NOT EXISTS pulled_from_envelope_sha CHAR(64);
CREATE INDEX IF NOT EXISTS idx_entity_relationships_pulled_envelope
    ON entity_relationships (pulled_from_envelope_sha)
    WHERE pulled_from_envelope_sha IS NOT NULL;


-- ─── 4. libraries.federation_id ───────────────────────────────────────────
-- A library's federation identity. Set ONCE at first publish to the hub;
-- immutable thereafter. Cairo's local Egypt library and your master's
-- Egypt library both reference the same federation_id even though their
-- local BIGINT library_id values differ.
--
-- Generation: UUIDv7 minted at first publish (app code chooses; gen_random_uuid()
-- would also work but UUIDv7 is time-ordered which is nicer for ops).
-- Existing libraries stay NULL until their next publish; their federation_id
-- gets assigned then.

ALTER TABLE libraries
    ADD COLUMN IF NOT EXISTS federation_id              UUID,
    ADD COLUMN IF NOT EXISTS original_producer_node_id  TEXT,
    ADD COLUMN IF NOT EXISTS first_published_at         TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS idx_libraries_federation_id
    ON libraries (federation_id)
    WHERE federation_id IS NOT NULL;

COMMENT ON COLUMN libraries.federation_id IS
    'Federation-wide library identity (UUIDv7). NULL until the library is '
    'first published to the hub; then set immutably. Two peers holding the '
    'same federation_id are mirroring the same logical library, even though '
    'their local library_id BIGINTs differ.';


-- ─── 5. extraction_pull_audit ─────────────────────────────────────────────
-- Every cross-node extraction-envelope pull attempt logged here: which
-- producer, which doc, which envelope sha (claimed vs verified), what the
-- gate decision was, what reason. Append-only.
--
-- This is the table you query when something looks wrong: "show me every
-- rejected pull in the last hour" or "did peer X try to push us bad data?".
-- Without this, federation poisoning is invisible after the fact.

CREATE TABLE IF NOT EXISTS extraction_pull_audit (
    pull_id              BIGSERIAL PRIMARY KEY,
    pulled_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- What was being pulled
    content_sha256       CHAR(64) NOT NULL,
    profile_fingerprint  TEXT NOT NULL,
    producer_node_id     TEXT NOT NULL,

    -- The two shas we compare for the bait-and-switch gate (G4):
    --   envelope_sha     = sha256(canonical_json(envelope.content)) (computed)
    --   hub_claimed_sha  = what /fleet/extractions/find said the envelope sha was
    -- If they disagree, the producer registered one sha with the hub and
    -- served different bytes.
    envelope_sha         CHAR(64),
    hub_claimed_sha      CHAR(64),

    -- decision = 'accepted'   → rows applied to local tables (transaction committed)
    --          = 'rejected'   → failed a verification gate; no rows applied
    --          = 'quarantined'→ rows partially applied or schema mismatch;
    --                            doc.extraction_state flipped to 'quarantine'
    decision             TEXT NOT NULL,

    -- Short stable codes for the gate that fired:
    --   g1_format_unknown        — envelope.format string wasn't recognized
    --   g2_content_mismatch      — envelope.content_sha256 != requested sha
    --   g3_sha_mismatch          — envelope_sha != hash of canonical content
    --   g4_hub_mismatch          — envelope_sha != hub_claimed_sha
    --   g5_oversize              — envelope > MAX_BYTES or row count > cap
    --   g6_low_trust             — producer trust_score below threshold
    --   g7_minority_envelope     — multiple producers, this one disagreed
    --   g8_fk_skew / g8_pack_incompatible / g8_value_invalid — per-row sanitation
    --   g9_tx_rolled_back        — DB error during apply; rolled back
    --   ok                       — accepted, no gate fired
    reason_code          TEXT,
    reason_detail        TEXT,

    -- For accepted pulls: {mentions: N, statements: M, assessments: K,
    --                      relationships: R, entities: E, new_entities: NE}
    row_counts_applied   JSONB,
    duration_ms          INT
);

CREATE INDEX IF NOT EXISTS idx_extraction_pull_audit_content
    ON extraction_pull_audit (content_sha256, pulled_at DESC);
CREATE INDEX IF NOT EXISTS idx_extraction_pull_audit_decision
    ON extraction_pull_audit (decision, pulled_at DESC);
CREATE INDEX IF NOT EXISTS idx_extraction_pull_audit_producer
    ON extraction_pull_audit (producer_node_id, pulled_at DESC);
CREATE INDEX IF NOT EXISTS idx_extraction_pull_audit_envelope
    ON extraction_pull_audit (envelope_sha)
    WHERE envelope_sha IS NOT NULL;

COMMENT ON TABLE extraction_pull_audit IS
    'Append-only ledger of every extraction-envelope pull attempt. Each row '
    'records which verification gate (G1..G9) accepted or rejected the '
    'envelope plus the diagnostic codes. The 10th gate (G10) IS this table — '
    'auditing means the federation has post-mortem visibility into every '
    'pull, whether accepted or rejected.';

COMMENT ON COLUMN extraction_pull_audit.decision IS
    'accepted | rejected | quarantined. accepted = rows applied. rejected = '
    'failed a gate before touching local tables. quarantined = applied '
    'partially then aborted, doc.extraction_state flipped to quarantine.';
