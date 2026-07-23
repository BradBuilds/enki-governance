"""
LDU promoter worker — walks ready LDU candidates and calls
``core.ldu.promote.promote_ready_candidates`` every 10 minutes.

Distinct from ``ldup_promoter`` (the legacy entity_ldu_p auto-promoter):
this worker is the LDU→GDU bridge for the new ldu_candidates table
introduced in Phase 2. Both can coexist; the legacy ldup_promoter
remains active for entries that landed before the May 20 rebuild.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Optional

_log = logging.getLogger("enki.workers.ldu_promoter")

_stop_event = threading.Event()
_thread: Optional[threading.Thread] = None
_last_result: dict = {}
_last_run_at: Optional[str] = None

POLL_INTERVAL = 600  # 10 minutes


def _worker_loop(tenant_id: str):
    dsn = os.environ.get("ENKI_PG_DSN", "") or None

    while not _stop_event.is_set():
        try:
            from core.ldu.promote import promote_ready_candidates
            result = promote_ready_candidates(tenant_id=tenant_id, dsn=dsn)
            global _last_result, _last_run_at
            _last_result = result or {}
            _last_run_at = datetime.now(timezone.utc).isoformat()
            if result and result.get("promoted", 0):
                _log.info("LDU promoter tick: %s", result)
        except Exception:
            _log.exception("LDU promoter tick failed")

        _stop_event.wait(POLL_INTERVAL)


def start_ldu_promoter(tenant_id: str = "default") -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(
        target=_worker_loop,
        args=(tenant_id,),
        name="ldu-promoter",
        daemon=True,
    )
    _thread.start()
    _log.info("LDU promoter worker started (tenant=%s)", tenant_id)


def stop_ldu_promoter() -> None:
    _stop_event.set()
    global _thread
    if _thread:
        _thread.join(timeout=5)
        _thread = None


def get_ldu_promoter_status() -> dict:
    return {
        "running": bool(_thread and _thread.is_alive()),
        "last_run_at": _last_run_at,
        "last_result": dict(_last_result),
        "poll_interval_sec": POLL_INTERVAL,
    }
