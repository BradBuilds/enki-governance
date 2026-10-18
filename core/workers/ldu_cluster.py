"""
LDU cluster worker — runs core.ldu.cluster.cluster_floating_concepts
on a 10-minute tick.

Cheap Tier-1 clustering for tier-G floating concepts. No LLM. Runs
after the collapse worker so it sees any concepts the dictionary-
driven strategies didn't merge.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Optional

_log = logging.getLogger("enki.workers.ldu_cluster")

_stop_event = threading.Event()
_thread: Optional[threading.Thread] = None
_last_result: dict = {}
_last_run_at: Optional[str] = None

POLL_INTERVAL = 600  # 10 minutes


def _worker_loop(tenant_id: str):
    dsn = os.environ.get("ENKI_PG_DSN", "") or None
    while not _stop_event.is_set():
        try:
            from core.ldu.cluster import cluster_floating_concepts
            result = cluster_floating_concepts(tenant_id=tenant_id, dsn=dsn)
            global _last_result, _last_run_at
            _last_result = result or {}
            _last_run_at = datetime.now(timezone.utc).isoformat()
            if result and result.get("merged", 0):
                _log.info("LDU cluster tick: %s", result)
        except Exception:
            _log.exception("LDU cluster tick failed")
        _stop_event.wait(POLL_INTERVAL)


def start_ldu_cluster(tenant_id: str = "default") -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(
        target=_worker_loop, args=(tenant_id,),
        name="ldu-cluster", daemon=True,
    )
    _thread.start()
    _log.info("LDU cluster worker started (tenant=%s)", tenant_id)


def stop_ldu_cluster() -> None:
    _stop_event.set()
    global _thread
    if _thread:
        _thread.join(timeout=5)
        _thread = None


def get_ldu_cluster_status() -> dict:
    return {
        "running": bool(_thread and _thread.is_alive()),
        "last_run_at": _last_run_at,
        "last_result": dict(_last_result),
        "poll_interval_sec": POLL_INTERVAL,
    }
