-- Migration 117: extraction_claims table — work-claim protocol substrate.
--
-- Part of Phase 4 of the library federation plan
-- (the internal library federation plan).
--
-- Goal: before node A spends 30 minutes running an LLM extraction on a
-- document, it CHECKS the hub to see if some other node is already
-- claiming this (content_sha256, profile) tuple. If yes, A skips
-- entirely and waits for the peer's eventual extraction-publish to
-- arrive via the subscriber. If no, A claims the work, extracts, then
-- releases the claim (completed or abandoned).
--
-- This migration is the substrate; the hub endpoints
-- (POST /fleet/extractions/claim, GET /check, POST /release) and the
-- extractor.py integration land in subsequent commits.
--
-- DEPLOY TARGETS: both peer nodes AND the cloud hub (same schema).
-- The unique constraint prevents two nodes from holding overlapping
-- active claims for the same tuple.

CREATE TABLE IF NOT EXISTS extraction_claims (
    claim_id            BIGSERIAL PRIMARY KEY,
    content_sha256      CHAR(64)      NOT NULL,
    profile_fingerprint TEXT          NOT NULL,
    claimant_node_id    TEXT          NOT NULL,
    claimed_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    expires_at          TIMESTAMPTZ   NOT NULL,
    -- status enum (by convention, not CHECK — multiple code paths update):
    --   'active'       | claim is in flight, peers should skip
    --   'completed'    | extraction succeeded and was published
    --   'abandoned'    | extraction failed or was cancelled; other nodes may try
    --   'expired'      | TTL passed; eligible for cleanup
    status              TEXT          NOT NULL DEFAULT 'active',
    completed_at        TIMESTAMPTZ
);

-- Active-claim lookup index: the hot path is "is anyone actively
-- claiming this tuple?" Partial index on status='active' keeps it tiny.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_extraction_claims_active
    ON extraction_claims (content_sha256, profile_fingerprint)
    WHERE status = 'active';

-- Full history index for audit / leaderboard queries.
CREATE INDEX IF NOT EXISTS idx_extraction_claims_content
    ON extraction_claims (content_sha256, profile_fingerprint, claimed_at DESC);

-- Per-node-id index for "what is this node currently working on?" + cleanup.
CREATE INDEX IF NOT EXISTS idx_extraction_claims_claimant
    ON extraction_claims (claimant_node_id, status, claimed_at DESC);
