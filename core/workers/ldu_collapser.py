"""
LDU collapser worker — runs core.ldu.collapse.collapse_pending_candidates
on a 5-minute tick.

Pure Tier 1: no LLM, no embedding, no network. Walks the LDU candidate
table only. Safe to run continuously; the merge pass is idempotent
(once a row is 'merged' it's skipped on subsequent ticks).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

_log = logging.getLogger("enki.workers.ldu_collapser")

_stop_event = threading.Event()
_thread: Optional[threading.Thread] = None
_last_result: dict = {}
_last_run_at: Optional[str] = None

POLL_INTERVAL = 300  # 5 minutes — same cadence as ldup_promoter


def _worker_loop(tenant_id: str):
    dsn = os.environ.get("ENKI_PG_DSN", "") or None

    while not _stop_event.is_set():
        try:
            from core.ldu.collapse import collapse_pending_candidates
            result = collapse_pending_candidates(tenant_id=tenant_id, dsn=dsn)
            global _last_result, _last_run_at
            _last_result = result or {}
            _last_run_at = datetime.now(timezone.utc).isoformat()
            if result and result.get("merged", 0):
                _log.info("LDU collapser tick: %s", result)
        except Exception:
            _log.exception("LDU collapser tick failed")

        _stop_event.wait(POLL_INTERVAL)


# ── Public API (matches worker-manager contract) ────────────────────────────

def start_ldu_collapser(tenant_id: str = "default") -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(
        target=_worker_loop,
        args=(tenant_id,),
        name="ldu-collapser",
        daemon=True,
    )
    _thread.start()
    _log.info("LDU collapser worker started (tenant=%s)", tenant_id)


def stop_ldu_collapser() -> None:
    _stop_event.set()
    global _thread
    if _thread:
        _thread.join(timeout=5)
        _thread = None


def get_ldu_collapser_status() -> dict:
    return {
        "running": bool(_thread and _thread.is_alive()),
        "last_run_at": _last_run_at,
        "last_result": dict(_last_result),
        "poll_interval_sec": POLL_INTERVAL,
    }
