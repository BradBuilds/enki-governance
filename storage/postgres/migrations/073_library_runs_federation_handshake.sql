-- 073_library_runs_federation_handshake.sql
--
-- Binds each library_run to the GDU active_set under which it was started.
-- The active_set hash + version is the federation handshake: two nodes can
-- only contribute compute to the same run if they're running compatible
-- GDU substrate (same packs, same edge constraints, same predicate dict).
--
-- When a peer joins a swarm, the coordinator checks:
--   peer.active_set_version == run.required_active_set_version
--   peer.pack_hash         == run.required_pack_hash
-- Mismatch → peer can subscribe (read consensus) but can't submit results.
--
-- This is the GDU-as-handshake property — same substrate, same canonical
-- universe, results merge cleanly.

BEGIN;

ALTER TABLE library_runs
    ADD COLUMN IF NOT EXISTS required_active_set_version TEXT,
    ADD COLUMN IF NOT EXISTS required_pack_hash          TEXT,
    ADD COLUMN IF NOT EXISTS required_pack_version       TEXT,
    -- Snapshot at run-start so we know exactly what substrate produced
    -- the derived layers under this run.
    ADD COLUMN IF NOT EXISTS active_set_at_start         JSONB;

COMMENT ON COLUMN library_runs.required_active_set_version IS
    'GDU active_set version a peer must be running to contribute compute. '
    'NULL = no constraint (local-only runs).';
COMMENT ON COLUMN library_runs.required_pack_hash IS
    'sha256 of the canonical pack bundle (entityvocab+edgevocab+signalvocab+…). '
    'Pinned at run start; peers must match to submit results.';
COMMENT ON COLUMN library_runs.active_set_at_start IS
    'Full snapshot of /governance/state at run-start. Replayable.';

CREATE INDEX IF NOT EXISTS idx_library_runs_active_set
    ON library_runs (required_active_set_version)
    WHERE required_active_set_version IS NOT NULL;

COMMIT;
