-- Migration 107: Hub-side envelope bytes replication marker.
--
-- Phase 9 of the Library Federation plan
-- (the internal library federation plan).
--
-- DEPLOY TARGET: HUB. (extraction_publications was already added to the
-- hub by the Phase 0+ smoke-test work; this just extends it.)
--
-- The hub holds the extraction_publications registry (index of who-has-
-- what). Phase 9 lets producers optionally also upload envelope BYTES to
-- the hub so subscribers can pull from the hub when the producer is
-- behind a firewall (Cloudflare Access, NAT, etc.) and not directly
-- reachable. Producers stay authoritative for content addressing —
-- envelope_sha256 is computed over canonical content and verified
-- end-to-end regardless of which intermediary served the bytes.
--
-- The column is just a flag — actual bytes live on disk under
-- /var/lib/enki/hub-storage/extractions/{sha[:2]}/{sha}/{...}.enki
-- (or wherever ENKI_HUB_EXTRACTION_STORE points). The hub-bytes-store
-- module reads/writes this disk layout.

ALTER TABLE extraction_publications
    ADD COLUMN IF NOT EXISTS hub_has_bytes        BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS hub_bytes_size       INT,
    ADD COLUMN IF NOT EXISTS hub_bytes_stored_at  TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_extpub_hub_has_bytes
    ON extraction_publications (hub_has_bytes)
    WHERE hub_has_bytes = TRUE;

COMMENT ON COLUMN extraction_publications.hub_has_bytes IS
    'TRUE when the producer uploaded envelope bytes to the hub at publish '
    'time (Phase 9). Subscribers behind firewalls (or unable to reach the '
    'producer directly) can pull from the hub instead of /p2p/extraction. '
    'Content integrity is still verified end-to-end via envelope_sha256 — '
    'the hub is a relay, not a trusted intermediary.';
