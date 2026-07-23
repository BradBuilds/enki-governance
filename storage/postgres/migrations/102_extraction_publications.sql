-- Migration 102: extraction_publications — cross-node extraction-result
-- sharing substrate.
--
-- The "extractions" layer (mentions/statements/assessments) is the work
-- product of running an LLM extraction pipeline over a Layer-1 file
-- (identified by content_sha256). Today every node redoes that work even
-- when 99 other nodes have the same content and the same extraction
-- profile — the single largest unrealized scaling win in the federation
-- (see the internal federation scaling notes).
--
-- This table is the registry. When node A finishes extracting a file
-- with a specific profile, it serializes the rows into a canonical CBOR
-- envelope, signs it, stores it at
--   /var/lib/enki/storage/extractions/{content_sha}/{profile_fp}/{producer_node_id}.enki
-- and POSTs a row here announcing what it has.
--
-- Subscribers (Batch C, node-side `extraction_subscriber.py`) query
--   GET /fleet/extractions/find?content=X&profile=Y
-- before launching their own LLM run. If a trusted producer has already
-- published an envelope, the subscriber pulls it from
--   GET /p2p/extraction/{content_sha}/{profile_fp}
-- verifies the envelope_sha matches what was published, and applies the
-- rows into local mentions/statements/assessments.
--
-- Cross-node consensus falls out automatically: two producers publishing
-- different envelope_sha for the same (content, profile) → existing
-- governance_conflicts machinery picks majority. Two producers with
-- matching envelope_sha → free corroboration signal.

CREATE TABLE IF NOT EXISTS extraction_publications (
  publication_id      BIGSERIAL PRIMARY KEY,

  -- The Layer-1 file being extracted. References documents.content_sha256
  -- but stored as a column for the cross-node lookup pattern (publishers
  -- and subscribers don't necessarily share a documents table).
  content_sha256      CHAR(64) NOT NULL,

  -- The extraction profile identity. extraction_profiles.fingerprint is
  -- sha256(model_id, prompt_hash, schema_version, temperature, ...) so
  -- two nodes running the SAME profile produce comparable output. Two
  -- nodes running DIFFERENT profiles (e.g. gemma3 vs gemma4) produce
  -- legitimately different envelopes and both are valid; subscribers
  -- can pick which profile they trust.
  profile_fingerprint TEXT NOT NULL,

  -- Who produced this envelope. Federation trust score on this node_id
  -- determines whether subscribers accept the envelope without local
  -- re-extraction.
  producer_node_id    VARCHAR(128) NOT NULL,

  -- sha256 of the canonical envelope CBOR. Same value across producers
  -- ⇒ they extracted the same rows ⇒ consensus. Different values from
  -- two trusted producers ⇒ disagreement, surface to governance_conflicts.
  result_envelope_sha CHAR(64) NOT NULL,

  -- {mentions: N, statements: M, assessments: K, total_bytes: B}
  -- Quick "is this envelope worth pulling?" check for subscribers without
  -- having to download it first.
  result_row_counts   JSONB NOT NULL DEFAULT '{}'::jsonb,

  -- Provenance + replay. produced_at is when the producer finished the
  -- extraction; published_at is when this row was inserted on the hub.
  produced_at         TIMESTAMPTZ NOT NULL,
  published_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  -- Ed25519 signature over canonical_string(content_sha256 ||
  -- profile_fingerprint || result_envelope_sha || producer_node_id ||
  -- produced_at). Verifies the producer actually authored this claim.
  -- Optional in dev environments where producer doesn't have signing
  -- infra; subscribers can still pull envelope-verified content.
  signature           TEXT,

  -- A given producer publishes only ONE envelope per (content, profile)
  -- tuple. Re-running extraction with the same profile produces the
  -- same envelope (deterministic) — should be an UPSERT not a duplicate.
  UNIQUE (content_sha256, profile_fingerprint, producer_node_id)
);

-- The hot query is "find me producers for sha256:X profile:Y" — used by
-- the subscriber before every LLM run.
CREATE INDEX IF NOT EXISTS idx_extpub_lookup
    ON extraction_publications (content_sha256, profile_fingerprint);

-- For the "which producers have high trust" sub-query when there are
-- multiple producers for the same content+profile.
CREATE INDEX IF NOT EXISTS idx_extpub_producer
    ON extraction_publications (producer_node_id, published_at DESC);

-- For the consensus check — when N producers publish the same
-- envelope_sha that's free corroboration.
CREATE INDEX IF NOT EXISTS idx_extpub_envelope
    ON extraction_publications (result_envelope_sha);

COMMENT ON TABLE extraction_publications IS
    'Registry of cross-node extraction-result publications. Producers '
    'register one row per (content_sha256, profile_fingerprint) tuple '
    'they have extracted. Subscribers query this table via '
    'GET /fleet/extractions/find to find existing extractions before '
    'launching their own LLM run. The actual envelope bytes (CBOR-wrapped '
    'mentions/statements/assessments) are served from '
    '/p2p/extraction/{content_sha}/{profile_fp} by the producer node.';

COMMENT ON COLUMN extraction_publications.profile_fingerprint IS
    'sha256 of the (model_id, prompt_hash, schema_version, temperature, '
    'other_inputs) tuple. Two nodes with the same fingerprint will '
    'produce comparable extractions. References extraction_profiles.fingerprint.';

COMMENT ON COLUMN extraction_publications.result_envelope_sha IS
    'sha256 of the canonical CBOR envelope. Equal across producers ⇒ '
    'matching extraction ⇒ free corroboration. Different ⇒ disagreement, '
    'surface to governance_conflicts for review or majority-wins logic.';

COMMENT ON COLUMN extraction_publications.signature IS
    'Optional Ed25519 signature by producer_node_id over the publication '
    'metadata. Required in production (--profile evidentiary); optional '
    'in dev environments. Envelope itself is always verified via '
    'result_envelope_sha at pull time regardless.';
