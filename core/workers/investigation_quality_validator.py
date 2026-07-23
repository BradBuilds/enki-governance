"""
Investigation Quality Validator — scores completed investigations and flags
garbage entities for review.

Phase 7c of the internal research validation plan.

Polls completed investigations every CHECK_INTERVAL seconds. For each
unscored row, computes a quality band from (findings_count,
entities_discovered) and writes the score back to entity_investigations.
When the band is LOW and the entity's signal_count is low too, queues
the entity for human review (entity_review_queue).

Quality bands:
  HEALTHY  (2): findings_count >= HEALTHY_FINDINGS AND entities_discovered >= 1
  MARGINAL (1): partial signal — has findings or has discovered, but not both,
                or below the HEALTHY thresholds
  LOW      (0): findings_count == 0 AND entities_discovered == 0

Action:
  HEALTHY  -> nothing
  MARGINAL -> nothing yet (Phase 7c v2 will track for re-investigation cadence)
  LOW + entity.signal_count < REVIEW_SIGNAL_THRESHOLD -> insert into
              entity_review_queue with reason='low_value_investigation'

Pattern matches the other Phase-1+ workers (start/stop/get_status, daemon
thread, supervisor-friendly), so it slots into the manager (Tier 2) the
same way enrichment_queue_worker does.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.investigation_quality_validator")

CHECK_INTERVAL = 300         # poll every 5 min
BATCH_SIZE = 50              # score this many investigations per tick
HEALTHY_FINDINGS = 4         # >=N raw facts to count as healthy
REVIEW_SIGNAL_THRESHOLD = 3  # LOW investigations on entities with fewer than
                             # this many news mentions get flagged for review

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()

_STATUS = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "tick_count": 0,
    "scored_total": 0,
    "scored_low": 0,
    "scored_marginal": 0,
    "scored_healthy": 0,
    "review_queued": 0,
    "last_error": None,
}


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default") -> dict:
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_worker_loop,
        args=(tenant_id,),
        name="investigation-quality-validator",
        daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    log.info("Investigation quality validator started")
    return {"status": "started"}


def stop_worker() -> dict:
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=5)
    _STATUS["running"] = False
    return {"status": "stopped"}


def _classify(findings_count: int, entities_discovered: int) -> tuple[int, str, str]:
    """Return (score, band, reason) tuple for a (findings, discovered) pair."""
    f = int(findings_count or 0)
    d = int(entities_discovered or 0)
    if f == 0 and d == 0:
        return (0, "LOW", "0 findings + 0 discovered: investigation returned nothing")
    if f >= HEALTHY_FINDINGS and d >= 1:
        return (2, "HEALTHY", f"{f} findings + {d} discovered: full coverage")
    return (1, "MARGINAL", f"{f} findings + {d} discovered: partial signal")


def _worker_loop(tenant_id: str) -> None:
    while not _STOP_EVENT.is_set():
        try:
            _STATUS["phase"] = "scoring"
            scored = _score_batch(tenant_id, BATCH_SIZE)
            _STATUS["tick_count"] += 1
            _STATUS["scored_total"] += scored
            _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
            _STATUS["last_error"] = None
            _STATUS["phase"] = f"idle (last batch {scored})"
        except Exception as e:
            log.warning("Quality validator tick failed: %s", e)
            _STATUS["last_error"] = str(e)[:300]
            _STATUS["phase"] = f"error: {str(e)[:60]}"

        for _ in range(CHECK_INTERVAL):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)

    _STATUS["running"] = False


def _score_batch(tenant_id: str, batch: int) -> int:
    """Score the next batch of completed-but-unscored investigations.
    Returns the number of investigations scored this batch."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT investigation_id, entity_id, findings_count, entities_discovered
                  FROM entity_investigations
                 WHERE tenant_id = %s
                   AND status = 'complete'
                   AND quality_score IS NULL
                 ORDER BY completed_at DESC NULLS LAST
                 LIMIT %s
                """,
                (tenant_id, batch),
            )
            rows = cur.fetchall()

        if not rows:
            return 0

        # Score in Python; one UPDATE per row keeps the SQL simple and the
        # transaction small (this is a backfill-style worker, not on the
        # hot path).
        for inv_id, entity_id, findings, discovered in rows:
            score, band, reason = _classify(findings, discovered)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE entity_investigations
                       SET quality_score = %s,
                           quality_band  = %s,
                           quality_reason = %s,
                           quality_scored_at = now()
                     WHERE investigation_id = %s
                    """,
                    (score, band, reason, inv_id),
                )

            # Update per-band counters
            if band == "LOW":
                _STATUS["scored_low"] += 1
                _maybe_flag_for_review(conn, tenant_id, entity_id, inv_id,
                                       findings, discovered)
            elif band == "MARGINAL":
                _STATUS["scored_marginal"] += 1
            else:
                _STATUS["scored_healthy"] += 1

        conn.commit()
        return len(rows)


def _maybe_flag_for_review(conn, tenant_id: str, entity_id: int, inv_id: int,
                            findings: int, discovered: int) -> None:
    """If a LOW-scored investigation belongs to an entity with low signal_count,
    insert into entity_review_queue. Caller already holds the open transaction.
    """
    if not entity_id:
        return
    with conn.cursor() as cur:
        cur.execute(
            "SELECT signal_count FROM entities WHERE entity_id = %s",
            (entity_id,),
        )
        row = cur.fetchone()
        if not row:
            return
        signal_count = int(row[0] or 0)
        if signal_count >= REVIEW_SIGNAL_THRESHOLD:
            # Real entity (people are mentioning it in news) — even if SEC/FEC/etc
            # don't know about it, we shouldn't auto-flag for cleanup.
            return
        # Check if there's already an open review for this entity
        cur.execute(
            """
            INSERT INTO entity_review_queue
                (tenant_id, entity_id, flagged_reason, investigation_id,
                 findings_count, entities_discovered, signal_count, status)
            VALUES (%s, %s, 'low_value_investigation', %s, %s, %s, %s, 'pending')
            ON CONFLICT (tenant_id, entity_id, status) DO NOTHING
            """,
            (tenant_id, entity_id, inv_id, findings, discovered, signal_count),
        )
        if cur.rowcount > 0:
            _STATUS["review_queued"] += 1
            log.info(
                "review_queue: flagged entity_id=%s (LOW + signal_count=%d)",
                entity_id, signal_count,
            )
