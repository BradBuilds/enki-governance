"""
Enrichment Queue Worker — drains entity_enrichment_queue.

Phase 1 of the internal research collapse plan.

Pipeline B (Wikipedia/Wikidata/sanctions research) had 2,741 pending
tasks and never ran because no worker called process_enrichment_queue.
This worker fills that gap. It runs at Tier 2 (LLM-medium) under the
manager's pressure-aware scheduling, so it pauses when load is high
or Ollama is unhealthy.

Pattern matches core/workers/feed_enrichment.py (status dict +
start/stop/get_status + supervisor-friendly).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from core.enrichment.entity_research import process_enrichment_queue
from core.workers.stuck_running_reaper import reap_stuck_rows

log = logging.getLogger("enki.workers.enrichment_queue")

CHECK_INTERVAL = 60   # poll every 60s
BATCH_SIZE = 10       # tasks per tick
IDLE_BACKOFF = 120    # backoff when queue is empty
REAP_EVERY_N_TICKS = 10  # ~10 min cadence at CHECK_INTERVAL=60s

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()

_STATUS = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "tick_count": 0,
    "total_processed": 0,
    "total_succeeded": 0,
    "total_failed": 0,
    "total_skipped": 0,
    "last_batch": None,
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
        name="enrichment-queue-worker",
        daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    _STATUS["phase"] = "starting"
    reap_stuck_rows("entity_enrichment_queue", tenant_id=tenant_id)
    log.info("Enrichment queue worker started")
    return {"status": "started"}


def stop_worker() -> dict:
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=5)
    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"
    return {"status": "stopped"}


def _worker_loop(tenant_id: str) -> None:
    backoff = CHECK_INTERVAL
    while not _STOP_EVENT.is_set():
        try:
            _STATUS["phase"] = "processing"
            stats = process_enrichment_queue(tenant_id, batch_size=BATCH_SIZE)
            _STATUS["tick_count"] += 1
            if _STATUS["tick_count"] % REAP_EVERY_N_TICKS == 0:
                reap_stuck_rows("entity_enrichment_queue", tenant_id=tenant_id)
            _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
            _STATUS["last_batch"] = dict(stats)
            _STATUS["total_processed"] += stats.get("processed", 0)
            _STATUS["total_succeeded"] += stats.get("succeeded", 0)
            _STATUS["total_failed"] += stats.get("failed", 0)
            _STATUS["total_skipped"] += stats.get("skipped", 0)
            _STATUS["last_error"] = None

            # Adaptive backoff: empty queue → back off; busy queue → poll fast
            if stats.get("processed", 0) == 0:
                backoff = min(backoff * 2, IDLE_BACKOFF)
                _STATUS["phase"] = f"idle (backoff={backoff}s)"
            else:
                backoff = CHECK_INTERVAL
        except Exception as e:
            log.warning("Enrichment queue tick failed: %s", e)
            _STATUS["last_error"] = str(e)[:300]
            _STATUS["phase"] = f"error: {str(e)[:60]}"
            backoff = IDLE_BACKOFF

        # Interruptible sleep
        for _ in range(int(backoff)):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)

    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"
