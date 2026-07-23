"""
Yield accountant — hub-side worker that walks completed library work
assignments, compares result hashes for cross-verification, and applies
yield deltas per the Phase 11 rules.

Rules (May 20 plan):
  - Processing a doc (verified by ≥2 node consensus): +1.0
  - Discovering a new entity (corroborated later):     +5.0  (out of scope here)
  - Verification disagreement, your side wins:         +2.0
  - Verification disagreement, your side loses:        -3.0
  - Inactive for >30 days:                             -10% decay

Idempotent: node_contributions has a unique index keyed on
(node_id, target_id, contribution_type) for the doc-processing types,
so reruns don't double-count.

Ticks every 15 minutes.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

_log = logging.getLogger("enki.workers.yield_accountant")

_stop_event = threading.Event()
_thread: Optional[threading.Thread] = None
_last_result: dict = {}
_last_run_at: Optional[str] = None

POLL_INTERVAL = 900  # 15 minutes
INACTIVITY_DECAY_DAYS = 30
INACTIVITY_DECAY_RATE = 0.10


def _record_contribution(
    cur, *, node_id: str, contribution_type: str,
    target_id: Optional[int], weight: float,
    metadata: Optional[dict] = None,
) -> bool:
    """Insert a contribution row. Returns True if a new row landed."""
    cur.execute(
        """
        INSERT INTO node_contributions
          (node_id, contribution_type, target_id, weight, metadata)
        VALUES (%s, %s, %s, %s, %s::jsonb)
        ON CONFLICT DO NOTHING
        RETURNING contribution_id
        """,
        (node_id, contribution_type, target_id, float(weight),
         json.dumps(metadata or {})),
    )
    return cur.fetchone() is not None


def _apply_delta(cur, node_id: str, delta: float, reason: str) -> None:
    """Apply a yield delta to federation_node_trust. Creates the row if
    missing (innocent prior 0.0 yield)."""
    cur.execute(
        """
        INSERT INTO federation_node_trust
          (node_id, yield_score, yield_history, last_yield_update_at)
        VALUES (%s, %s,
                jsonb_build_object('last_reason', %s, 'last_delta', %s,
                                   'updated_at', NOW()::text),
                NOW())
        ON CONFLICT (node_id) DO UPDATE SET
          yield_score = federation_node_trust.yield_score + %s,
          yield_history = jsonb_set(
            COALESCE(federation_node_trust.yield_history, '{}'::jsonb),
            '{recent}',
            COALESCE(federation_node_trust.yield_history->'recent', '[]'::jsonb)
              || jsonb_build_array(jsonb_build_object(
                   'delta', %s,
                   'reason', %s,
                   'at', NOW()::text)),
            true),
          last_yield_update_at = NOW()
        """,
        (node_id, float(delta), reason, float(delta),
         float(delta), float(delta), reason),
    )


def _settle_completed_assignments(conn) -> dict:
    """Walk library_work_assignments completed but unsettled, group by
    (library, document), settle consensus / dispute. Returns counts."""
    consensus = 0
    disputed = 0
    settled_groups = 0

    with conn.cursor() as cur:
        # Pull groups where ALL >=2 assignments are completed AND no
        # contribution has been recorded yet for any of them.
        cur.execute(
            """
            SELECT lwa.library_id, lwa.document_id,
                   array_agg(lwa.assignment_id ORDER BY lwa.completed_at) AS aids,
                   array_agg(lwa.subscriber_node_id ORDER BY lwa.completed_at) AS nids,
                   array_agg(lwa.result_hash ORDER BY lwa.completed_at) AS hashes
            FROM library_work_assignments lwa
            WHERE lwa.status = 'completed'
              AND lwa.result_hash IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM node_contributions nc
                WHERE nc.target_id = lwa.assignment_id
                  AND nc.contribution_type IN (
                    'doc_processed_consensus',
                    'doc_processed_disputed_won',
                    'doc_processed_disputed_lost'
                  )
              )
            GROUP BY lwa.library_id, lwa.document_id
            HAVING count(*) >= 2
            LIMIT 200
            """,
        )
        groups = cur.fetchall()

        for lib_id, doc_id, aids, nids, hashes in groups:
            settled_groups += 1
            tally = Counter(hashes)
            majority_hash, majority_count = tally.most_common(1)[0]

            if majority_count == len(hashes):
                # Full consensus → reward every participant
                for aid, nid in zip(aids, nids):
                    if _record_contribution(
                        cur, node_id=nid,
                        contribution_type="doc_processed_consensus",
                        target_id=aid, weight=1.0,
                        metadata={"library_id": int(lib_id),
                                  "document_id": int(doc_id)},
                    ):
                        _apply_delta(cur, nid, +1.0,
                                     "doc_processed_consensus")
                consensus += 1
            else:
                # Dispute. Majority wins +2; minority loses -3.
                for aid, nid, h in zip(aids, nids, hashes):
                    if h == majority_hash:
                        if _record_contribution(
                            cur, node_id=nid,
                            contribution_type="doc_processed_disputed_won",
                            target_id=aid, weight=2.0,
                            metadata={"library_id": int(lib_id),
                                      "document_id": int(doc_id)},
                        ):
                            _apply_delta(cur, nid, +2.0,
                                         "doc_processed_disputed_won")
                    else:
                        if _record_contribution(
                            cur, node_id=nid,
                            contribution_type="doc_processed_disputed_lost",
                            target_id=aid, weight=-3.0,
                            metadata={"library_id": int(lib_id),
                                      "document_id": int(doc_id)},
                        ):
                            _apply_delta(cur, nid, -3.0,
                                         "doc_processed_disputed_lost")
                disputed += 1

    conn.commit()
    return {"settled_groups": settled_groups,
            "consensus": consensus, "disputed": disputed}


def _apply_inactivity_decay(conn) -> dict:
    """Nodes inactive for >30 days lose 10% of yield_score on each
    decay pass. Only applies once per day per node (gated by
    last_yield_update_at)."""
    decayed = 0
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT node_id, yield_score
            FROM federation_node_trust
            WHERE yield_score > 0.01
              AND (last_yield_update_at IS NULL
                   OR last_yield_update_at < NOW()
                                              - INTERVAL '{INACTIVITY_DECAY_DAYS} days')
              AND (last_updated_at IS NULL
                   OR last_updated_at < NOW()
                                         - INTERVAL '{INACTIVITY_DECAY_DAYS} days')
            LIMIT 500
            """,
        )
        rows = cur.fetchall()
        for node_id, score in rows:
            delta = -float(score) * INACTIVITY_DECAY_RATE
            _record_contribution(
                cur, node_id=node_id,
                contribution_type="inactivity_decay",
                target_id=None, weight=delta,
                metadata={"prior_score": float(score)},
            )
            _apply_delta(cur, node_id, delta, "inactivity_decay")
            decayed += 1
    conn.commit()
    return {"decayed": decayed}


def _tick() -> dict:
    from storage.postgres.db import get_connection
    settled = {}
    decay = {}
    with get_connection() as conn:
        settled = _settle_completed_assignments(conn)
    with get_connection() as conn:
        decay = _apply_inactivity_decay(conn)
    return {**settled, **decay}


def _worker_loop():
    while not _stop_event.is_set():
        try:
            result = _tick()
            global _last_result, _last_run_at
            _last_result = result
            _last_run_at = datetime.now(timezone.utc).isoformat()
            if (result.get("settled_groups", 0)
                    or result.get("decayed", 0)):
                _log.info("yield_accountant tick: %s", result)
        except Exception:
            _log.exception("yield_accountant tick failed")
        _stop_event.wait(POLL_INTERVAL)


def start_yield_accountant(tenant_id: str = "default") -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(
        target=_worker_loop, name="yield-accountant", daemon=True,
    )
    _thread.start()
    _log.info("yield_accountant started")


def stop_yield_accountant() -> None:
    _stop_event.set()
    global _thread
    if _thread:
        _thread.join(timeout=5)
        _thread = None


def get_yield_accountant_status() -> dict:
    return {
        "running": bool(_thread and _thread.is_alive()),
        "last_run_at": _last_run_at,
        "last_result": dict(_last_result),
        "poll_interval_sec": POLL_INTERVAL,
    }
