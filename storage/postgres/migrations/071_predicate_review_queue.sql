-- 071_predicate_review_queue.sql
--
-- Audit trail + governance queue for predicate_dictionary growth.
--
-- The predicate_extender worker is the only writer to predicate_dictionary
-- (besides the human-edited seed pack). Every promotion — auto OR human —
-- writes a row here, so we can answer:
--
--   - WHY is "deliver" mapped to "communicated_with"? (audit)
--   - Which mappings did the LLM auto-promote vs which were human-approved?
--   - What's currently pending review for the user to triage?
--
-- Tiered automation, with confidence thresholds:
--   morph_v1   – verb is a morphological variant of an existing dict entry
--                (e.g. "killed" → "kill"). Auto-promoted as alias at 0.95.
--   rule_v1    – type-pair frequency heuristic (legacy predicate_extender).
--                Auto-promoted at 0.5 — flagged in queue for human spot-check.
--   llm_v1     – LLM saw 3 sample sentences + edge taxonomy and proposed a
--                mapping. Auto-promoted if proposed_confidence ≥ 0.85;
--                queued otherwise.
--   human      – operator manually approved / overrode a queued proposal.

BEGIN;

CREATE TABLE IF NOT EXISTS predicate_review_queue (
    queue_id              BIGSERIAL PRIMARY KEY,
    candidate_lemma       TEXT         NOT NULL,
    occurrences           INT          NOT NULL DEFAULT 0,
    sample_statement_ids  BIGINT[]     NOT NULL DEFAULT '{}',
    proposed_edge_type    TEXT,        -- nullable: LLM may have said 'none_apply'
    proposed_confidence   REAL         NOT NULL DEFAULT 0.0,
    proposed_by           TEXT         NOT NULL,
        -- 'morph_v1' | 'rule_v1' | 'llm_v1' | 'human'
    evidence              JSONB        NOT NULL DEFAULT '{}'::jsonb,
        -- { "type_pair_dist": [...],
        --   "sample_sentences": [...],
        --   "llm_reasoning": "...",
        --   "llm_alt_proposals": [...] }
    status                TEXT         NOT NULL DEFAULT 'pending',
        -- 'pending' | 'approved' | 'rejected' | 'auto_promoted'
    reviewed_by           TEXT,        -- user_id or 'auto'
    reviewed_at           TIMESTAMPTZ,
    applied_at            TIMESTAMPTZ, -- when the row hit predicate_dictionary
    final_edge_type       TEXT,        -- what was actually applied (may differ from proposed if remapped)
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- One queue row per (lemma, proposed_edge_type) pair. COALESCE handles
-- NULL proposed_edge_type (LLM said 'none_apply') as a distinct slot.
CREATE UNIQUE INDEX IF NOT EXISTS uq_predicate_queue_lemma_edge
    ON predicate_review_queue (candidate_lemma, COALESCE(proposed_edge_type, ''));

CREATE INDEX IF NOT EXISTS idx_predicate_queue_status_created
    ON predicate_review_queue (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_predicate_queue_lemma
    ON predicate_review_queue (candidate_lemma);

COMMENT ON TABLE predicate_review_queue IS
    'Audit trail + human-review queue for every predicate_dictionary entry. '
    'Worker writes here on auto-promote; UI writes here on human review.';
COMMENT ON COLUMN predicate_review_queue.status IS
    'pending = needs human review; approved = added to dict by human; '
    'rejected = lemma is junk; auto_promoted = added by worker w/o human';

COMMIT;
