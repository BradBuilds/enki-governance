-- Migration 105: library_notes — append-only, content-addressed notes
-- layer for library federation bundles.
--
-- Phase 6 of the Library Federation plan
-- (the internal library federation plan).
--
-- DEPLOY TARGET: peer nodes. The hub does not store note bodies; it may
-- later carry a lightweight index (Phase 6.5).
--
-- A note is an analyst's annotation against something in a library: an
-- entity dossier, a document, a specific mention/statement/assessment, or
-- the library as a whole. Notes:
--
--   • Are append-only. An "edit" is a new note row whose parent_note_sha
--     points to the previous version. UI shows the chain; the data layer
--     never mutates.
--   • Are content-addressed by note_sha256 = sha256 over a canonical
--     JSON of (library_federation_id, subject_kind, subject_ref,
--     author_node_id, author_user_id, body, body_format, parent_note_sha,
--     created_at_iso). Federation sync dedupes on this sha so two peers
--     can pull the same note from different routes without collision.
--   • Travel through the library manifest v2 notes[] section (the
--     primary sync channel). A future Phase 6.5 will add hub-mediated
--     /fleet/notes/publish for push-side propagation.
--
-- Subject_kind enum-by-convention:
--   entity      | subject_ref = canonical_entity_key (sha256 hex)
--   document    | subject_ref = content_sha256
--   mention     | subject_ref = "doc_sha:char_start-char_end"
--   statement   | subject_ref = "doc_sha:predicate:raw_text_sha[:16]"
--   assessment  | subject_ref = base60_address
--   library     | subject_ref = NULL (note is about the library itself)

CREATE TABLE IF NOT EXISTS library_notes (
    note_id              BIGSERIAL PRIMARY KEY,
    note_sha256          CHAR(64) NOT NULL UNIQUE,    -- content address

    tenant_id            TEXT NOT NULL DEFAULT 'default',

    -- Federation-wide library identity. We key on federation_id (not the
    -- local library_id BIGINT) so notes follow the library across peers.
    -- NULL only when the library hasn't been federation-published yet.
    library_federation_id UUID,

    -- Local convenience: also keep the local library_id for fast joins
    -- before the library is federation-published. Either field can be
    -- NULL but at least one must be set (enforced in app code).
    library_id           BIGINT REFERENCES libraries(library_id) ON DELETE CASCADE,

    -- Append-only edit chain. Root notes have parent_note_sha = NULL.
    -- Edits create a new note whose parent points back. UI walks the
    -- chain to show "current version" and "edit history".
    parent_note_sha      CHAR(64),

    subject_kind         TEXT NOT NULL
                           CHECK (subject_kind IN ('entity','document','mention',
                                                    'statement','assessment','library')),
    subject_ref          TEXT,                          -- key into the subject (see header)

    author_node_id       TEXT NOT NULL,                 -- which peer authored this
    author_user_id       TEXT,                          -- which analyst (NULL = system/automation)
    author_display_name  TEXT,                          -- denormalized for portable display

    body                 TEXT NOT NULL,                 -- the note content
    body_format          TEXT NOT NULL DEFAULT 'markdown' CHECK (body_format IN ('markdown','plain')),

    visibility           TEXT NOT NULL DEFAULT 'library'
                           CHECK (visibility IN ('library','producers_only','private')),

    -- Producer-signed for cross-node integrity (Phase 10 makes this required).
    signature            TEXT,

    -- Federation pull provenance (NULL for locally-authored notes)
    pulled_from_node_id      TEXT,
    pulled_from_envelope_sha CHAR(64),

    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Soft-delete marker. A note "deletion" is actually a new note with
    -- is_deletion_marker=TRUE pointing parent_note_sha at the target.
    -- The original row stays for audit. The UI hides notes whose latest
    -- chain entry is a deletion marker.
    is_deletion_marker   BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_library_notes_library
    ON library_notes (library_id, created_at DESC)
    WHERE is_deletion_marker = FALSE;

CREATE INDEX IF NOT EXISTS idx_library_notes_federation
    ON library_notes (library_federation_id, created_at DESC)
    WHERE library_federation_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_library_notes_subject
    ON library_notes (subject_kind, subject_ref)
    WHERE subject_ref IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_library_notes_parent
    ON library_notes (parent_note_sha)
    WHERE parent_note_sha IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_library_notes_pulled_envelope
    ON library_notes (pulled_from_envelope_sha)
    WHERE pulled_from_envelope_sha IS NOT NULL;

COMMENT ON TABLE library_notes IS
    'Append-only, content-addressed library annotations. Travels through '
    'library manifest v2''s notes[] section as the federation sync channel. '
    'Edits chain via parent_note_sha; soft-deletes via is_deletion_marker. '
    'Two peers seeing the same note (same note_sha256) trivially dedup '
    'on the UNIQUE index.';

COMMENT ON COLUMN library_notes.note_sha256 IS
    'sha256 over canonical JSON of (library_federation_id, subject_kind, '
    'subject_ref, author_node_id, author_user_id, body, body_format, '
    'parent_note_sha, created_at_iso). Stable across peers. The content '
    'IS the identity.';
