-- Migration 014: media library (Phase 4A)
--
-- Two tables implementing the "same image across N articles collapses to
-- one media node" model:
--
-- media_nodes     one row per unique image (deduped by perceptual + byte hash).
--                 Deterministic node_id is "media:<first-16-chars-of-phash>".
--                 bytes_path points at the library file on disk.
--
-- media_sources   append-only attribution log. Every URL/outlet/signal that
--                 has used this image writes a row here. This is how you
--                 answer "who originated this photo?" and "every article
--                 that used this image."
--
-- Perceptual hash (phash) is 64-bit stored as lowercase 16-char hex.
-- Near-duplicate matching uses Hamming distance computed in-app (Phase 4A.4);
-- we index the first 4 hex chars so the candidate set stays small.

BEGIN;

CREATE TABLE IF NOT EXISTS media_nodes (
    node_id              VARCHAR(64)  PRIMARY KEY,            -- "media:<phash[:16]>"
    tenant_id            VARCHAR(255) NOT NULL DEFAULT 'default',
    subtype              VARCHAR(16)  NOT NULL DEFAULT 'image', -- image|video|audio

    -- Hashes
    phash                VARCHAR(16)  NOT NULL,               -- 64-bit perceptual, lowercase hex
    dhash                VARCHAR(16),                         -- difference hash (faster exact-ish match)
    byte_hash            VARCHAR(64)  NOT NULL,               -- SHA-256 of raw bytes (exact-bytes dedup)

    -- Pixel metadata
    width                INTEGER,
    height               INTEGER,
    bytes                INTEGER,                             -- file size
    mime_type            VARCHAR(64),

    -- Library storage
    bytes_path           TEXT,                                -- storage/cache/media_library/<...> relative
    thumbnail_path       TEXT,                                -- optional derived thumbnail

    -- Attribution shortcut (denormalized — media_sources is the full log)
    first_seen_url       TEXT,
    first_seen_outlet    VARCHAR(255),
    first_seen_signal_rid BIGINT,
    first_seen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_count         INTEGER NOT NULL DEFAULT 1,
    outlet_count         INTEGER NOT NULL DEFAULT 1,

    -- EXIF / analysis (future-proofing)
    exif_coordinates     GEOGRAPHY(POINT, 4326),
    exif_taken_at        TIMESTAMPTZ,
    analysis             JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- LDU lineage
    admitted_under_active_set     VARCHAR(32),
    admitted_under_pack_versions  JSONB DEFAULT '{}'::jsonb,
    gdu_canonical                 BOOLEAN NOT NULL DEFAULT TRUE,
    ldu_t_id                      BIGINT,

    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT media_nodes_tenant_byte_unique UNIQUE (tenant_id, byte_hash)
);

-- Near-duplicate candidate index: 1-char prefix (16 buckets). Wider than
-- 4-char because resize+re-encode routinely flips a few leading bits.
-- The in-app Hamming scan handles the final filter.
CREATE INDEX IF NOT EXISTS idx_media_nodes_phash_prefix
    ON media_nodes (tenant_id, LEFT(phash, 1));

CREATE INDEX IF NOT EXISTS idx_media_nodes_recent
    ON media_nodes (tenant_id, first_seen_at DESC);

CREATE INDEX IF NOT EXISTS idx_media_nodes_exif_location
    ON media_nodes USING gist (exif_coordinates)
    WHERE exif_coordinates IS NOT NULL;


CREATE TABLE IF NOT EXISTS media_sources (
    source_id            BIGSERIAL PRIMARY KEY,
    tenant_id            VARCHAR(255) NOT NULL DEFAULT 'default',

    media_node_id        VARCHAR(64)  NOT NULL REFERENCES media_nodes(node_id)
                                      ON DELETE CASCADE,

    -- Where the image came from
    signal_rid           BIGINT,                              -- signal that carried it
    article_node_id      VARCHAR(255),                        -- article/document that used it
    url                  TEXT,                                -- source URL
    outlet               VARCHAR(255),
    caption              TEXT,                                -- alt text / figure caption if any
    role                 VARCHAR(32),                         -- thumbnail|body|og_image|twitter_image

    seen_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- LDU lineage per source
    admitted_under_active_set     VARCHAR(32),
    gdu_canonical                 BOOLEAN NOT NULL DEFAULT TRUE,
    ldu_t_id                      BIGINT,

    CONSTRAINT media_sources_no_duplicate
      UNIQUE (tenant_id, media_node_id, url, article_node_id)
);

CREATE INDEX IF NOT EXISTS idx_media_sources_by_node
    ON media_sources (media_node_id, seen_at DESC);

CREATE INDEX IF NOT EXISTS idx_media_sources_by_outlet
    ON media_sources (tenant_id, outlet, seen_at DESC)
    WHERE outlet IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_media_sources_by_article
    ON media_sources (article_node_id)
    WHERE article_node_id IS NOT NULL;

COMMIT;
