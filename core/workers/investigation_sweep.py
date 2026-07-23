"""
Investigation Sweep — threshold-cross trigger for entity investigations.

Phase 3.1 of the internal pipeline restart plan.

The boundary failure this fixes:
    `_maybe_queue_investigation` in core/workers/node_builder.py is
    called ONLY from `gateway.admit_entity()` — i.e., when an entity is
    first created. After admission, signal_count grows via the linker
    code which does NOT call the gateway again. Entities that crossed
    the 5-signal threshold AFTER admission (Alex Acme at 57,
    Jane Doe at 87, all the peripheral names) never
    triggered an investigation and stayed un-researched.

This worker scans entities whose signal_count >= INVESTIGATION_THRESHOLD
and have no pending/running/recent investigation, and queues each one.
Same threshold + cooldown + sources-by-type routing as the original
admit-time trigger — fires on signal-count growth rather than
first-admit, which is the missing boundary.

Tier 1 — always runs (no LLM dependency; investigation worker that
actually executes the queued items is separate and is Tier 2).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.investigation_sweep")

CHECK_INTERVAL = 300                  # 5 min between sweeps
BATCH_SIZE = 50                       # scan this many candidate entities per tick
INVESTIGATION_THRESHOLD = 5           # match node_builder.INVESTIGATION_THRESHOLD
INVESTIGATION_COOLDOWN_HOURS = 24     # match node_builder.INVESTIGATION_COOLDOWN_HOURS
ELIGIBLE_TYPES = ("person", "organization")

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()

_STATUS = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "tick_count": 0,
    "candidates_scanned_total": 0,
    "investigations_queued_total": 0,
    "skipped_active_or_recent": 0,
    "errors": 0,
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
        target=_worker_loop, args=(tenant_id,),
        name="investigation-sweep", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    log.info("Investigation sweep started")
    return {"status": "started"}


def stop_worker() -> dict:
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=5)
    _STATUS["running"] = False
    return {"status": "stopped"}


def _worker_loop(tenant_id: str) -> None:
    backoff = CHECK_INTERVAL
    while not _STOP_EVENT.is_set():
        try:
            _STATUS["phase"] = "sweeping"
            queued = _sweep_batch(tenant_id, BATCH_SIZE)
            _STATUS["tick_count"] += 1
            _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
            _STATUS["last_error"] = None
            if queued > 0:
                backoff = CHECK_INTERVAL
                _STATUS["phase"] = f"queued {queued} investigation(s)"
            else:
                backoff = min(backoff + 60, CHECK_INTERVAL * 3)
                _STATUS["phase"] = "idle — caught up"
        except Exception as e:
            log.warning("Investigation sweep tick failed: %s", e)
            _STATUS["errors"] += 1
            _STATUS["last_error"] = str(e)[:300]
            _STATUS["phase"] = f"error: {str(e)[:60]}"
            backoff = min(backoff * 2, 1800)

        for _ in range(int(backoff)):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)

    _STATUS["running"] = False


def _sweep_batch(tenant_id: str, batch: int) -> int:
    """Find threshold-crossers without an active/recent investigation, queue them."""
    from core.research.investigation_queue import try_queue_investigation

    queued = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            # The LEFT JOIN filter is the cheap pre-filter — it skips
            # entities we already have an active or recent run on.
            # The investigation_queue helper re-checks the same window
            # to close the race, but the pre-filter keeps batch sizes
            # honest so we don't burn cycles re-deciding the same
            # entities every tick.
            cur.execute(
                """
                SELECT e.entity_id, e.canonical_name, e.canonical_type, e.signal_count
                  FROM entities e
                  LEFT JOIN entity_investigations ei
                    ON ei.entity_id = e.entity_id
                   AND (ei.status IN ('pending','running')
                        OR ei.completed_at > NOW() - (%s::text || ' hours')::interval)
                 WHERE e.tenant_id = %s
                   AND e.gdu_canonical = TRUE
                   AND e.signal_count >= %s
                   AND e.canonical_type = ANY(%s)
                   AND ei.investigation_id IS NULL
                 ORDER BY e.signal_count DESC, e.entity_id
                 LIMIT %s
                """,
                (
                    str(INVESTIGATION_COOLDOWN_HOURS),
                    tenant_id,
                    INVESTIGATION_THRESHOLD,
                    list(ELIGIBLE_TYPES),
                    batch,
                ),
            )
            candidates = cur.fetchall()

    _STATUS["candidates_scanned_total"] += len(candidates)
    if not candidates:
        return 0

    for entity_id, name, ctype, signal_count in candidates:
        inv_id, outcome = try_queue_investigation(
            entity_id=entity_id,
            tenant_id=tenant_id,
            seed_name=name,
            entity_type=ctype,
            discovered_from="threshold_sweep",
        )
        if outcome == "queued":
            queued += 1
            _STATUS["investigations_queued_total"] += 1
            log.info(
                "queued investigation %s for entity_id=%s (%s, %s, signal_count=%d)",
                inv_id, entity_id, name, ctype, signal_count,
            )
        elif outcome in ("active", "cooldown"):
            _STATUS["skipped_active_or_recent"] += 1
        elif outcome == "error":
            _STATUS["errors"] += 1
            _STATUS["last_error"] = f"queue failed for entity_id={entity_id}"

    return queued
