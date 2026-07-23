"""
Divisor governance worker — applies passed divisor proposals to the
subtype_divisors substrate.

Watches ``governance_proposals`` for ``category IN
('divisor_ratify','divisor_reelect')`` with ``status='open'`` AND
``closes_at < NOW()``. For each:

  divisor_ratify   — flip the named subtype's state from 'provisional'
                     to 'locked'. Permanent.

  divisor_reelect  — change the named subtype's divisor + partition
                     labels, then trigger a cascade re-encoding of all
                     concepts under that subtree.
                     **CASCADE IS STUBBED** in this rev — applying the
                     proposal flips the divisor on subtype_divisors and
                     bumps cascade_count, but does NOT re-encode the
                     existing concept_nodes. A separate one-shot
                     ``scripts/cascade_subtree.py`` must run to
                     complete the migration. The stub design lets us
                     observe live re-election decisions without
                     blocking on the cascade complexity.

Tick: every 15 min. Cheap walk over open proposals.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Optional

_log = logging.getLogger("enki.workers.divisor_governance")

_stop_event = threading.Event()
_thread: Optional[threading.Thread] = None
_last_result: dict = {}
_last_run_at: Optional[str] = None

POLL_INTERVAL = 900  # 15 minutes
PASS_THRESHOLD_DEFAULT = 0.5  # fraction of yes-weight required to pass


def _tally_proposal(cur, proposal_id: int) -> dict:
    """Compute yield-weighted yes/no fractions for a proposal."""
    cur.execute(
        """SELECT ballot, COALESCE(sum(effective_weight), 0)
             FROM governance_votes
            WHERE proposal_id = %s
            GROUP BY ballot""",
        (proposal_id,),
    )
    weights = {"yes": 0.0, "no": 0.0, "abstain": 0.0}
    for ballot, w in cur.fetchall():
        weights[ballot] = float(w or 0.0)
    decisive = weights["yes"] + weights["no"]
    yes_fraction = weights["yes"] / decisive if decisive else 0.0
    return {
        "weights": weights,
        "decisive_weight": decisive,
        "yes_fraction": yes_fraction,
    }


def _apply_ratify(cur, proposal_id: int, metadata: dict) -> bool:
    subtype_path = (metadata or {}).get("subtype_path")
    if not subtype_path:
        _log.warning("divisor_ratify %s: missing subtype_path", proposal_id)
        return False

    cur.execute(
        """UPDATE subtype_divisors SET
             state                 = 'locked',
             ratified_at           = NOW(),
             ratifying_proposal_id = %s
           WHERE subtype_path = %s AND state = 'provisional'
           RETURNING subtype_path""",
        (proposal_id, subtype_path),
    )
    row = cur.fetchone()
    if row:
        _log.info(
            "divisor_ratify proposal=%s: %s now LOCKED",
            proposal_id, subtype_path,
        )
        return True
    _log.info(
        "divisor_ratify proposal=%s: %s not provisional (already locked or missing); skip",
        proposal_id, subtype_path,
    )
    return False


def _apply_reelect(cur, proposal_id: int, metadata: dict) -> bool:
    subtype_path = (metadata or {}).get("subtype_path")
    proposed_divisor = (metadata or {}).get("proposed_divisor")
    proposed_labels = (metadata or {}).get("proposed_partition_labels") or []

    if not subtype_path or not proposed_divisor:
        _log.warning("divisor_reelect %s: missing fields", proposal_id)
        return False

    if int(proposed_divisor) not in (2, 3, 4, 5, 6, 10, 12, 15, 20, 30):
        _log.warning(
            "divisor_reelect %s: invalid divisor %s",
            proposal_id, proposed_divisor,
        )
        return False

    cur.execute(
        """SELECT state, divisor FROM subtype_divisors
            WHERE subtype_path = %s""",
        (subtype_path,),
    )
    existing = cur.fetchone()
    if not existing:
        _log.warning(
            "divisor_reelect %s: subtype_path %s has no current divisor entry",
            proposal_id, subtype_path,
        )
        return False
    state, current = existing
    if state == "locked":
        _log.info(
            "divisor_reelect %s: %s is LOCKED, rejecting",
            proposal_id, subtype_path,
        )
        return False

    cur.execute(
        """UPDATE subtype_divisors SET
             divisor               = %s,
             partition_labels      = %s::jsonb,
             cascade_count         = cascade_count + 1,
             last_cascade_at       = NOW(),
             declared_in           = 'governance_election',
             elected_at            = NOW()
           WHERE subtype_path = %s""",
        (int(proposed_divisor), json.dumps(proposed_labels), subtype_path),
    )

    # CASCADE STUB — log that re-encoding is needed.
    _log.warning(
        "divisor_reelect proposal=%s applied to %s (D:%s→%s). "
        "Cascade NOT performed automatically. Run scripts/cascade_subtree.py "
        "to re-encode existing concept_nodes addresses under the new partitioning.",
        proposal_id, subtype_path, current, proposed_divisor,
    )
    return True


def _tick() -> dict:
    from storage.postgres.db import get_connection

    proposals_seen = 0
    ratified = 0
    reelected = 0
    failed = 0

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT proposal_id, category, metadata, pass_threshold
                FROM governance_proposals
                WHERE status = 'open'
                  AND category IN ('divisor_ratify', 'divisor_reelect')
                  AND closes_at <= NOW()
                ORDER BY closes_at ASC
                LIMIT 50
                """,
            )
            for prop_id, category, metadata, threshold in cur.fetchall():
                proposals_seen += 1
                tally = _tally_proposal(cur, prop_id)
                threshold_val = float(threshold or PASS_THRESHOLD_DEFAULT)

                if tally["decisive_weight"] <= 0:
                    # No decisive votes — proposal fails by default.
                    cur.execute(
                        """UPDATE governance_proposals SET
                             status = 'failed', resolved_at = NOW()
                           WHERE proposal_id = %s""",
                        (prop_id,),
                    )
                    continue

                passed = tally["yes_fraction"] >= threshold_val

                if passed:
                    applied = False
                    if category == "divisor_ratify":
                        applied = _apply_ratify(cur, prop_id, metadata or {})
                        if applied:
                            ratified += 1
                    elif category == "divisor_reelect":
                        applied = _apply_reelect(cur, prop_id, metadata or {})
                        if applied:
                            reelected += 1
                    if not applied:
                        failed += 1
                    cur.execute(
                        """UPDATE governance_proposals SET
                             status = 'passed', resolved_at = NOW()
                           WHERE proposal_id = %s""",
                        (prop_id,),
                    )
                else:
                    cur.execute(
                        """UPDATE governance_proposals SET
                             status = 'failed', resolved_at = NOW()
                           WHERE proposal_id = %s""",
                        (prop_id,),
                    )
        conn.commit()

    return {
        "proposals_seen": proposals_seen,
        "ratified": ratified,
        "reelected": reelected,
        "failed": failed,
    }


def _worker_loop():
    while not _stop_event.is_set():
        try:
            result = _tick()
            global _last_result, _last_run_at
            _last_result = result
            _last_run_at = datetime.now(timezone.utc).isoformat()
            if result.get("proposals_seen", 0):
                _log.info("divisor_governance tick: %s", result)
        except Exception:
            _log.exception("divisor_governance tick failed")
        _stop_event.wait(POLL_INTERVAL)


def start_divisor_governance(tenant_id: str = "default") -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(
        target=_worker_loop, name="divisor-governance", daemon=True,
    )
    _thread.start()
    _log.info("divisor_governance worker started")


def stop_divisor_governance() -> None:
    _stop_event.set()
    global _thread
    if _thread:
        _thread.join(timeout=5)
        _thread = None


def get_divisor_governance_status() -> dict:
    return {
        "running": bool(_thread and _thread.is_alive()),
        "last_run_at": _last_run_at,
        "last_result": dict(_last_result),
        "poll_interval_sec": POLL_INTERVAL,
    }
