"""Attestation — the single write path into `gdu_admission_log`.

WHAT IS ATTESTED AND WHY IT LIVES HERE
--------------------------------------
Decision C splits node writes in two. **Derived** properties (completeness,
scores, tiers) are a pure function of the row and the packs, so they can be
recomputed at any time and need no chokepoint — `scripts/reconcile_completeness.py`
is sufficient and proven. **Attested** facts cannot be reconstructed after the
fact: you cannot discover next week that a row was admitted under a
content-hashed pack, or that its subtype was remapped on the way in. If it is not
recorded at write time it is gone. This module is that recording.

It exists as its own authority for the same reason `completeness_core` does: the
gateway was the only thing that could write an admission row, fourteen other
files write `knowledge_nodes` directly, and a primitive only the gateway can call
guarantees the other fourteen stay silent.

WHAT A CHAIN ENTRY MEANS (this changed, deliberately)
-----------------------------------------------------
It used to mean "vocabulary-validated, addressed, collapse-checked", because only
`gateway.admit_node`/`admit_entity` could produce one. Extending attestation to
writers that do not run all three would have made that implication false while
leaving it unstated — the worst of both. So the entry no longer implies a
governance level; it **states** one, in `checks_run`. A write that skipped
vocabulary validation is recorded as having skipped it.

That is *a gap is not a negative* applied to provenance: the missing check is
written down as missing, rather than forged (claiming governance that did not
happen) or omitted (leaving no row, which is what 66.5% of the corpus has today
and is indistinguishable from a row that was never written at all).

`checks_run` and `writer` are INSIDE the hash. A governance claim outside the
chain is a claim anyone can edit.

THE THREE DEFECTS THIS FIXES (measured on node-A, 2026-08-06, 574,409 rows)
-----------------------------------------------------------------------
1. **The chain was unverifiable by construction.** The old payload hashed
   `"ts": datetime.now(timezone.utc).isoformat()` — a Python clock reading that
   was never persisted. The row stored `created_at` (the DB's `now()`), a
   different reading. So `mutation_hash` could not be reproduced from the row by
   anyone, ever. `core/workers/chain_verifier.py` imports `hashlib` and never
   calls it for exactly this reason: it can only compare `previous_hash` against
   the prior `mutation_hash`, which checks POINTERS, not CONTENT. Editing
   `reason` or `operation` in place passed it cleanly.

   Fixed by generating `ts` here and writing it as `created_at` explicitly rather
   than letting the column default fire. The bytes that were hashed are the bytes
   that are stored.

2. **`reason` was hashed in full, stored truncated** to 500 chars. Latent on node-A
   (longest reason: 84) and fatal the first time one ran long. Fixed by
   truncating before hashing — `chain_payload` receives what the row will hold.

3. **The chain forked.** The head was read with an unlocked
   `SELECT ... ORDER BY rid DESC LIMIT 1`, so two concurrent admissions read the
   same head and both claimed it as parent: 671 forks / 1,342 rows, every one
   exactly two members whose rids were <= 8 apart, some with `created_at`
   inverted relative to `rid` (overlapping transactions showing through). Fixed
   with a per-tenant advisory lock taken BEFORE the head read and held to commit.

None of the 1,342 forked rows is repaired, and no legacy row is stamped. A chain
the operator can recompute at will is a chain that proves nothing — see
`scripts/verify_admission_chain.py`.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
from datetime import datetime, timezone
from typing import Optional

_log = logging.getLogger("enki.gdu.attest")

CHAIN_VERSION = "0.1.0"

#: The governance checks a write may run. Every attestation must state ALL of
#: them explicitly — see `_validate_checks`. Partial dicts are refused rather
#: than defaulted, because a default is a claim: `True` forges governance that
#: did not happen, and `False` slanders a writer that did run the check. The
#: writer is the only thing that knows, so the writer must say.
CHECK_NAMES: tuple[str, ...] = (
    # LDU-W -> GDU boundary check (`pack_aware_prompt.validate_llm_output`):
    # did model output get checked against the pack before admission?
    "boundary",
    # (category, subtype) validated against the governed vocabulary
    # (`is_valid_node_type` / `get_valid_entity_subtypes`).
    "vocab",
    # this write carries a base-60 address that came from the governed
    # sequence (`core.gdu.address.assign_address`, or its SQL mirror
    # `gdu_assign_address`) rather than being left NULL or hand-set.
    # True on a collapse that inherits the governed address of the row it
    # collapsed into — the address is governed either way, and an address is
    # a coordinate, not a claim.
    "address",
    # a dedup/collapse check ran before creating, so this is a considered
    # new node rather than a blind INSERT.
    "collapse",
)

#: Convenience for the gateway's own write path, which runs all four.
ALL_CHECKS: dict = {name: True for name in CHECK_NAMES}


def _validate_checks(checks_run: dict) -> dict:
    """Refuse anything but a complete, boolean-valued statement of the checks.

    Fail-closed, matching `core/gdu/completeness.py`: a mis-spelled key would
    otherwise record silently as "check absent" and read, forever after, as a
    governance gap that never existed. The chain is supposed to be the thing you
    can trust when everything else is in doubt; it cannot also be lenient about
    what goes into it.
    """
    if not isinstance(checks_run, dict):
        raise TypeError(
            f"checks_run must be a dict of {CHECK_NAMES}, got {type(checks_run).__name__}")
    unknown = set(checks_run) - set(CHECK_NAMES)
    if unknown:
        raise ValueError(
            f"checks_run has unknown check(s) {sorted(unknown)}; "
            f"valid checks are {list(CHECK_NAMES)}")
    missing = set(CHECK_NAMES) - set(checks_run)
    if missing:
        raise ValueError(
            f"checks_run must state every check explicitly; missing {sorted(missing)}. "
            f"There is no default: True would forge governance that did not run, "
            f"False would slander a writer that did.")
    bad = {k: v for k, v in checks_run.items() if not isinstance(v, bool)}
    if bad:
        raise TypeError(f"checks_run values must be bool, got {bad}")
    return {name: checks_run[name] for name in CHECK_NAMES}


def chain_payload(
    *,
    node_id: Optional[str],
    entity_id: Optional[int],
    edge_id: Optional[str],
    operation: str,
    category: Optional[str],
    subtype: Optional[str],
    base60: Optional[str],
    input_type: Optional[str],
    mapped_type: Optional[str],
    reason: Optional[str],
    writer: Optional[str],
    checks_run: Optional[dict],
    lineage_stamp: Optional[dict],
    ts: datetime,
) -> str:
    """The exact bytes covered by `mutation_hash`. Pure; no I/O.

    THE ONE RULE: every argument here must be recoverable from the stored row,
    byte-identical, or the chain reverts to defect 1 and stops being verifiable.
    `scripts/verify_admission_chain.py` imports THIS function and rebuilds the
    payload from the persisted columns — it does not carry a transcription. A
    hand-synchronised copy is how `slot_coverage_report` ended up mirroring
    `score_node`'s projection while being the baseline for it.

    `ts` is rendered from the value written to `created_at`, not from a fresh
    clock reading. `reason` must already be truncated to what the column holds.

    `input_type` / `mapped_type` are covered because together they ARE the
    collapse decision — that a `ship` was remapped to a `vessel` on the way in is
    precisely the kind of fact that cannot be reconstructed later.
    """
    return json.dumps(
        {
            "node_id": node_id,
            "entity_id": entity_id,
            "edge_id": edge_id,
            "operation": operation,
            "category": category,
            "subtype": subtype,
            "base60": base60,
            "input_type": input_type,
            "mapped_type": mapped_type,
            "reason": reason,
            "writer": writer,
            "checks_run": checks_run,
            "lineage_stamp": lineage_stamp,
            "ts": ts.astimezone(timezone.utc).isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def chain_regime() -> str:
    """Content hash of the hashing rule itself.

    Same discipline as `completeness._scorer_version` and the extraction-regime
    rule (profile version = sha of the prompt): a version you must remember to
    bump by hand is a version that eventually lies. If `chain_payload` changes
    shape, rows hashed under the old shape must not claim the new regime — they
    would fail recomputation and read as tampering.
    """
    try:
        src = inspect.getsource(chain_payload)
    except (OSError, TypeError):
        return f"chain@{CHAIN_VERSION}/payload@unknown"
    sha = hashlib.sha256(src.encode()).hexdigest()[:8]
    return f"chain@{CHAIN_VERSION}/payload@{sha}"


def attest_admission(
    conn,
    tenant_id: str,
    *,
    operation: str,
    writer: str,
    checks_run: dict,
    node_id: Optional[str] = None,
    entity_id: Optional[int] = None,
    edge_id: Optional[str] = None,
    category: Optional[str] = None,
    subtype: Optional[str] = None,
    base60: Optional[str] = None,
    input_type: Optional[str] = None,
    mapped_type: Optional[str] = None,
    reason: str = "",
    lineage_stamp: Optional[dict] = None,
) -> tuple[int, str]:
    """Append one hash-chained admission record. Returns `(rid, mutation_hash)`.

    The rid is returned because `library_seals.admission_rid` (migration 132)
    anchors a seal to its admission row, so the seal path needs the identity of
    the row it just wrote.

    Runs INSIDE the caller's transaction and does not commit — the attestation
    and the write it attests to must land together or not at all. A committed
    node with no record is the 66.5% gap; a committed record with no node is a
    claim about something that does not exist.

    `conn` must not be in autocommit: the advisory lock below is transaction
    scoped, and under autocommit it would release immediately and the fork race
    would remain open.
    """
    checks = _validate_checks(checks_run)
    if not operation:
        raise ValueError("operation is required")
    if not writer:
        raise ValueError(
            "writer is required — an unattributable attestation is the defect "
            "this module exists to close")

    reason_stored = (reason or "")[:500]
    ts = datetime.now(timezone.utc)

    with conn.cursor() as cur:
        # Serialize the chain head PER TENANT. Taken before the head is read and
        # held until the caller commits, which is what makes read-then-append
        # atomic. Without it two concurrent admissions read the same head and
        # both claim it: 671 such forks on node-A before this existed.
        #
        # Per-tenant rather than global so one tenant's write rate cannot stall
        # another's; each tenant is an independent chain anyway. The `gdu_chain:`
        # prefix keeps this out of the way of the other advisory locks in the
        # codebase (`core/ldu/admit_candidate.py` locks on subtype paths).
        #
        # Cost is a single lock held for the length of one INSERT, against a
        # measured ~3,000 admissions/day. Serialization is free at this rate and
        # would still be cheap at a hundred times it.
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"gdu_chain:{tenant_id}",))

        cur.execute(
            """
            SELECT mutation_hash FROM gdu_admission_log
             WHERE tenant_id = %s ORDER BY rid DESC LIMIT 1
            """, (tenant_id,))
        row = cur.fetchone()
        previous_hash = row[0] if row else None

        payload = chain_payload(
            node_id=node_id, entity_id=entity_id, edge_id=edge_id,
            operation=operation, category=category, subtype=subtype,
            base60=base60, input_type=input_type, mapped_type=mapped_type,
            reason=reason_stored, writer=writer, checks_run=checks,
            lineage_stamp=lineage_stamp, ts=ts,
        )
        mutation_hash = hashlib.sha256(
            ((previous_hash or "genesis") + payload).encode()).hexdigest()

        # created_at is written EXPLICITLY. Letting the column default to now()
        # is defect 1: the hashed instant and the stored instant would be two
        # different clock readings and nothing could ever be recomputed.
        cur.execute(
            """
            INSERT INTO gdu_admission_log
                (tenant_id, node_id, entity_id, edge_id,
                 operation, category, subtype, base60_address,
                 input_type, mapped_type, reason,
                 lineage_stamp, mutation_hash, previous_hash,
                 chain_regime, writer, checks_run, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s::jsonb, %s, %s, %s, %s, %s::jsonb, %s)
            RETURNING rid
            """, (
                tenant_id, node_id, entity_id, edge_id,
                operation, category, subtype, base60,
                input_type, mapped_type, reason_stored,
                json.dumps(lineage_stamp) if lineage_stamp is not None else None,
                mutation_hash, previous_hash,
                chain_regime(), writer, json.dumps(checks), ts,
            ))
        rid = cur.fetchone()[0]

    return rid, mutation_hash
