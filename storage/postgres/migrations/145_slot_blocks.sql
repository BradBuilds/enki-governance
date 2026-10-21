-- Migration 145 — a gap is not a negative, at slot level
--
-- Migration 140 records blocked sources per RUN
-- (entity_investigations.sources_blocked). That is the right idea at the wrong
-- granularity: under constitutive expansion a refused source leaves an
-- UNEXPANDED SUBTREE, and everything beneath it is silently absent. The run
-- knows "opencorporates refused"; the score does not, so it counts the vessel's
-- flag state as asked-and-missing rather than never-asked.
--
-- The consequence is a number that lies in the flattering direction: a vessel
-- with no route data scores IDENTICALLY whether routes do not exist or the
-- route source was behind Cloudflare. One of those is knowledge about the
-- world; the other is knowledge about our own reach.
--
-- WHAT A ROW HERE MEANS
--
--   "We tried to fill <slot> for <node>, and <source> refused, at <blocked_at>."
--
-- It is a claim about an ATTEMPT, not about the world. That is why it carries
-- the source and the reason: a block with no named refuser is indistinguishable
-- from an excuse, and would let any slot be withdrawn from the denominator by
-- asserting it. `source` is NOT NULL for exactly that reason.
--
-- WHY NOT A COLUMN ON knowledge_nodes
--
-- Blocks are per (node, slot) and a node has many slots, so a column would mean
-- a JSON blob nobody can index or expire. They also decay: a Cloudflare block
-- today says nothing about next month, which is why `blocked_at` is here and
-- why the reader is expected to age them out. A score computed against a
-- two-year-old block is asserting a reach we have not tested since.
--
-- NO BACKFILL. Every block is written at the moment of refusal by the code that
-- was refused. Nothing here can be reconstructed later — the same rule as
-- migrations 140, 141 and 143 — and a block invented after the fact would
-- silently shrink a denominator on the strength of a guess.

BEGIN;

CREATE TABLE IF NOT EXISTS slot_blocks (
    block_id     BIGSERIAL    PRIMARY KEY,
    tenant_id    VARCHAR(255) NOT NULL,
    node_id      VARCHAR(255) NOT NULL,
    -- Spec path, e.g. 'registration.flag_state'. Matches slot_specs exactly;
    -- a value that is not a declared slot for that subtype is ignored by the
    -- scorer rather than shrinking anything.
    slot         VARCHAR(128) NOT NULL,
    -- WHO refused. NOT NULL on purpose: an unattributed block is an excuse.
    source       VARCHAR(128) NOT NULL,
    -- HOW it refused: 'waf', 'auth', 'rate_limit', 'timeout', 'http_403', …
    -- Free text rather than a CHECK: the failure modes of the open web are not
    -- a closed vocabulary, and a constraint here would make the honest answer
    -- unrecordable.
    reason       TEXT,
    blocked_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    -- The investigation run that hit the wall, when there was one. Ties a slot
    -- block back to migration 140's run-level record.
    run_id       BIGINT,
    CONSTRAINT slot_blocks_slot_not_blank CHECK (length(trim(slot)) > 0),
    CONSTRAINT slot_blocks_source_not_blank CHECK (length(trim(source)) > 0)
);

-- The scorer's read path: every current block for one node.
CREATE INDEX IF NOT EXISTS idx_slot_blocks_node
    ON slot_blocks (tenant_id, node_id, blocked_at DESC);

-- "What is this source costing us?" — the question that decides whether a
-- connector is worth fixing.
CREATE INDEX IF NOT EXISTS idx_slot_blocks_source
    ON slot_blocks (source, blocked_at DESC);

-- One live block per (node, slot, source). Re-refusals update the timestamp
-- rather than piling up rows, so a flapping connector cannot inflate its own
-- footprint and make a node look more blocked than it is.
CREATE UNIQUE INDEX IF NOT EXISTS uq_slot_blocks_live
    ON slot_blocks (tenant_id, node_id, slot, source);

COMMENT ON TABLE slot_blocks IS
    'Per-slot record of a source refusing to answer. Read by the completeness '
    'scorer, which WITHDRAWS blocked slots from the denominator rather than '
    'counting them as missing — unknown is not the same as unasked. Written '
    'only at the moment of refusal; never backfilled.';

COMMIT;
