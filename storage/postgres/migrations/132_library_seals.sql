-- 132_library_seals.sql
--
-- Phase 0 of the Open Archive Federation plan: the library SEAL primitive.
--
-- A library's identity is a LAYERED, content-addressed root — not a frozen copy
-- (git-tag semantics, not a zip). Three roots:
--
--   evidence_root       deterministic, model-INDEPENDENT. Hash of the library's
--                       documents (by content_sha256) [+ grounded verbatim spans
--                       once Phase 8 lands]. This is the federation IDENTITY — what
--                       peers MUST agree on. Stable across re-collapse and across
--                       which model interpreted the text.
--   interpretation_root the current collapse state (entities + cooccurrence graph)
--                       derived from those documents. EVOLVES as the library grows
--                       and as models contribute readings. Confluent (order-
--                       independent) — see EVIDENCE.md §7c.
--   combined_root       hash(evidence_root | interpretation_root | collapsepolicy
--                       hash | pack_versions). The seal binds all of it at a point
--                       in time.
--
-- Seals are APPEND-ONLY: a library keeps growing past a seal; each seal is an
-- immutable, citable release. A newer seal supersedes (superseded_by) but old
-- seals remain provable. Every seal is anchored to a hash-chained row in
-- gdu_admission_log (admission_rid) so the seal itself is tamper-evident.

BEGIN;

CREATE TABLE IF NOT EXISTS library_seals (
    seal_id              BIGSERIAL    PRIMARY KEY,
    library_id           BIGINT       NOT NULL,
    federation_id        UUID,
    tenant_id            VARCHAR(255) NOT NULL DEFAULT 'default',

    evidence_root        CHAR(64)     NOT NULL,
    interpretation_root  CHAR(64)     NOT NULL,
    combined_root        CHAR(64)     NOT NULL,

    collapsepolicy_hash  VARCHAR(64),
    pack_versions        JSONB        NOT NULL DEFAULT '{}'::jsonb,

    doc_count            INTEGER      NOT NULL DEFAULT 0,
    edge_count           INTEGER      NOT NULL DEFAULT 0,
    entity_count         INTEGER      NOT NULL DEFAULT 0,

    sealed_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    sealed_by            VARCHAR(255),

    -- git-tag semantics: a later seal supersedes this one; old seals stay provable.
    superseded_by        BIGINT       REFERENCES library_seals(seal_id),

    -- hash-chained anchor: the gdu_admission_log row that recorded this seal.
    admission_rid        BIGINT
);

CREATE INDEX IF NOT EXISTS idx_library_seals_library   ON library_seals (library_id, sealed_at DESC);
CREATE INDEX IF NOT EXISTS idx_library_seals_combined  ON library_seals (combined_root);
CREATE INDEX IF NOT EXISTS idx_library_seals_federation ON library_seals (federation_id) WHERE federation_id IS NOT NULL;

-- The current (active) seal per library = the one not yet superseded.
CREATE INDEX IF NOT EXISTS idx_library_seals_active
    ON library_seals (library_id) WHERE superseded_by IS NULL;

COMMIT;
