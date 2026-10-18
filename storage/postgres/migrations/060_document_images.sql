-- 060: Document image substrate (Phase 3.1)
--
-- Until now, every embedded image in every PDF was discarded. The image
-- worker extracts them, hashes them, stores them with full provenance
-- back to the source document + page + bbox, then runs vision-LLM +
-- face-detection + OCR on each one. Images become first-class evidence
-- nodes attached to the canonical entities that own them.
--
-- See the internal refdata hierarchical collapse plan
-- and the internal phase 3 image worker research notes

-- ─── Image extraction ──────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS document_images (
    image_id              BIGSERIAL PRIMARY KEY,
    tenant_id             TEXT NOT NULL DEFAULT 'default',
    document_id           BIGINT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    page_id               BIGINT NOT NULL REFERENCES document_pages(page_id) ON DELETE CASCADE,
    image_index           INT NOT NULL,              -- 0..N within the page
    bbox                  JSONB,                     -- {x, y, w, h} on the rendered page
    on_disk_path          TEXT NOT NULL,             -- /opt/enki/storage/doc_images/{doc}/p{page}_i{idx}.{ext}
    sha256                TEXT NOT NULL,             -- content hash (dedup across docs)
    phash                 BIGINT,                    -- perceptual hash for near-duplicate clustering
    mime                  TEXT,
    width                 INT,
    height                INT,
    bytes                 INT,
    is_redacted_block     BOOLEAN DEFAULT FALSE,     -- pure-black redaction
    extraction_engine     TEXT NOT NULL DEFAULT 'pdfimages',
    extracted_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (document_id, page_id, image_index)
);

CREATE INDEX IF NOT EXISTS idx_doc_images_doc ON document_images(document_id);
CREATE INDEX IF NOT EXISTS idx_doc_images_page ON document_images(page_id);
CREATE INDEX IF NOT EXISTS idx_doc_images_sha ON document_images(sha256);
CREATE INDEX IF NOT EXISTS idx_doc_images_phash ON document_images(phash)
    WHERE phash IS NOT NULL;

-- ─── Per-image analyses ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS document_image_analyses (
    analysis_id           BIGSERIAL PRIMARY KEY,
    image_id              BIGINT NOT NULL REFERENCES document_images(image_id) ON DELETE CASCADE,
    engine                TEXT NOT NULL,             -- 'vision_gemma4' | 'vision_llava' |
                                                     -- 'tesseract_ocr' | 'face_mediapipe' |
                                                     -- 'classifier_quick' | etc.
    -- High-level results
    classification        TEXT,                      -- 'photo' | 'diagram' | 'signature' |
                                                     -- 'seal' | 'map' | 'chart' | 'id_card' |
                                                     -- 'redaction' | 'decorative' | 'other'
    caption               TEXT,                      -- vision LLM's one-sentence description
    is_intelligence       BOOLEAN DEFAULT FALSE,     -- evidence-grade vs decorative
    confidence            REAL,
    -- Engine-specific data
    ocr_text              TEXT,
    objects               TEXT[],
    tags                  TEXT[],
    faces_count           INT DEFAULT 0,
    faces                 JSONB,                     -- [{bbox, confidence}, ...]
    metadata              JSONB,                     -- engine-specific extras
    error                 TEXT,                      -- non-null when analysis failed
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (image_id, engine)
);

CREATE INDEX IF NOT EXISTS idx_doc_image_analyses_image
    ON document_image_analyses(image_id);
CREATE INDEX IF NOT EXISTS idx_doc_image_analyses_class
    ON document_image_analyses(classification);
CREATE INDEX IF NOT EXISTS idx_doc_image_analyses_intel
    ON document_image_analyses(is_intelligence)
    WHERE is_intelligence;

-- ─── Cross-doc image clusters (pHash dedup) ─────────────────────────────

CREATE TABLE IF NOT EXISTS document_image_clusters (
    cluster_id            BIGSERIAL PRIMARY KEY,
    corpus_id             TEXT NOT NULL,
    representative_image_id BIGINT REFERENCES document_images(image_id) ON DELETE SET NULL,
    phash                 BIGINT NOT NULL,
    member_count          INT NOT NULL,
    document_count        INT NOT NULL,
    classification        TEXT,                      -- inherited modal classification
    is_template           BOOLEAN DEFAULT FALSE,     -- decorative letterhead vs evidence
    sample_caption        TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS document_image_cluster_members (
    cluster_id            BIGINT NOT NULL REFERENCES document_image_clusters(cluster_id) ON DELETE CASCADE,
    image_id              BIGINT NOT NULL REFERENCES document_images(image_id) ON DELETE CASCADE,
    hamming_distance      INT,
    PRIMARY KEY (cluster_id, image_id)
);

CREATE INDEX IF NOT EXISTS idx_doc_image_clusters_corpus
    ON document_image_clusters(corpus_id);

-- ─── Backlink: an image-sourced mention ─────────────────────────────────
-- A face recognized in an image, or a name OCR'd from inside a photo,
-- becomes an entity_mention with extraction_method='image_*' and the
-- new image_id FK so we can trace the entity back to which photo on
-- which page of which document it came from.

ALTER TABLE document_mentions
    ADD COLUMN IF NOT EXISTS image_id BIGINT
        REFERENCES document_images(image_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_doc_mentions_image
    ON document_mentions(image_id)
    WHERE image_id IS NOT NULL;
