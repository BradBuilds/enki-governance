-- Migration 143 — the admission chain becomes verifiable, and honest about who wrote it
--
-- C3 (attested half). Three defects measured on node-A 2026-08-06, all found by
-- reading the DEPLOYED gateway rather than the tree:
--
--   1. THE CHAIN COULD NOT BE VERIFIED BY ANYONE. `_log_admission` hashed a
--      payload containing a Python-side `"ts": datetime.now(...)` that was never
--      stored; the row kept the DB's `created_at` instead. mutation_hash was
--      therefore unreproducible from the persisted row, which is why
--      core/workers/chain_verifier.py imports hashlib and never calls it: it
--      compares previous_hash to the prior mutation_hash and nothing else. That
--      is a statement about POINTERS, not CONTENT — editing `reason` or
--      `operation` in place passed it cleanly for 574,409 rows.
--
--   2. `reason` was hashed in full and stored truncated to 500 chars. Latent on
--      node-A (longest reason: 84) and fatal the first time a reason ran long.
--
--   3. THE CHAIN FORKED. The head was read with an unlocked
--      `SELECT ... ORDER BY rid DESC LIMIT 1`, so concurrent admissions read the
--      same head and both claimed it: 671 forks / 1,342 rows, every one exactly
--      two members with rids <= 8 apart. A write race, not tampering — but
--      indistinguishable from tampering to the verifier, which has been
--      reporting them as CHAIN INTEGRITY VIOLATION on a 30-minute timer.
--
-- Extending a chain with defect 1 to 100% of writes (which is what C3 does)
-- would have built a much larger mechanism that still proved nothing. So the
-- chain is fixed before it is widened.
--
-- THREE COLUMNS, NO BACKFILL.
--
--   chain_regime  names the hashing rule that produced mutation_hash. NULL on
--                 every existing row and deliberately left NULL: those rows are
--                 unverifiable and inventing a regime for them would assert a
--                 property they do not have. This is the same object as
--                 `legacy:unattributable` on completeness_regime, and it is
--                 matched the same way — as a PREFIX namespace, with the `%` as
--                 a BOUND PARAMETER. Matching such a marker as a fixed string
--                 has already caused two bugs in this codebase, the second
--                 inside the script that verified the first.
--
--   writer        the code path that produced the row. Until C3 the log could
--                 only ever be written by the gateway, so this was implicit;
--                 once the 14 direct-INSERT files attest, it stops being.
--
--   checks_run    which governance checks ACTUALLY ran. The chain's meaning
--                 changes here and the change is deliberate: an entry used to
--                 imply "vocabulary-validated, addressed, collapse-checked"
--                 because only the gateway could write one. Extending
--                 attestation to writers that do not run all three would have
--                 made that implication false. Instead the row states which
--                 checks ran, so an unchecked write is RECORDED as unchecked
--                 rather than either forged or omitted. A gap is not a negative:
--                 the absent check is written down as absent.
--
-- checks_run and writer are inside the hash. They are governance claims; a
-- claim outside the chain is a claim anyone can edit.

BEGIN;

ALTER TABLE gdu_admission_log
    ADD COLUMN IF NOT EXISTS chain_regime VARCHAR(64),
    ADD COLUMN IF NOT EXISTS writer       VARCHAR(128),
    ADD COLUMN IF NOT EXISTS checks_run   JSONB;

COMMENT ON COLUMN gdu_admission_log.chain_regime IS
    'Hashing rule that produced mutation_hash, e.g. chain@0.1.0/payload@<sha8>. '
    'NULL = written before the verifiable-chain rule and unreproducible by '
    'construction (the hashed ts was never stored). Never backfilled. Matched '
    'as a prefix namespace, never as a fixed string.';

COMMENT ON COLUMN gdu_admission_log.writer IS
    'Code path that wrote this admission, e.g. gdu.gateway.admit_node. Inside '
    'the hash.';

COMMENT ON COLUMN gdu_admission_log.checks_run IS
    'Which governance checks actually ran for this write, e.g. '
    '{"boundary":true,"vocab":true,"address":true,"collapse":true}. A chain '
    'entry no longer implies all of them ran; it states which did. Inside the '
    'hash.';

-- The verifier splits legacy from current on every run, and C3.3 measures
-- unattested writes per writer. Both scan on these two columns.
CREATE INDEX IF NOT EXISTS idx_gdu_admission_chain_regime
    ON gdu_admission_log (chain_regime);

CREATE INDEX IF NOT EXISTS idx_gdu_admission_writer
    ON gdu_admission_log (writer);

COMMIT;
