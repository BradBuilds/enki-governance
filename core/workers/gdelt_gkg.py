"""
GDELT GKG worker — runs sync_latest_gkg() on a 15-minute cadence.

GDELT publishes a fresh GKG slice every 15 minutes; we pull it and
populate `gdelt_mentions` for the map layer. Retention is handled inside
sync_latest_gkg (rows older than 48h get purged each cycle).

Idempotent on its own state: if the worker hits a freshly-published
file URL it has already processed, the sync function returns
``skipped=True`` and we no-op until the next slice is available.

Exposes the standard supervisor surface: start_worker / stop_worker /
get_status.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from datetime import datetime, timezone

logger = logging.getLogger("enki.workers.gdelt_gkg")

# GDELT's published cadence is 15 minutes. We poll slightly more often
# (every 5 min) so that on the rare missed cycle we recover quickly —
# the no-op skipped result is cheap.
POLL_INTERVAL_S = int(os.getenv("ENKI_GDELT_GKG_POLL", "300"))

_thread: threading.Thread | None = None
_stop_event = threading.Event()
_last_result: dict = {}
_last_run_at: datetime | None = None


def _gkg_loop():
    global _last_result, _last_run_at
    from core.connectors.gdelt_gkg import sync_latest_gkg

    while not _stop_event.is_set():
        try:
            _last_result = asyncio.run(sync_latest_gkg())
            _last_run_at = datetime.now(timezone.utc)
            if _last_result.get("error"):
                logger.warning("gdelt sync error: %s", _last_result["error"])
            elif _last_result.get("skipped"):
                logger.debug("gdelt sync: nothing new (%s)", _last_result.get("fetched_url"))
            else:
                logger.info(
                    "gdelt sync: %d mentions inserted, %d purged (%s)",
                    _last_result.get("inserted", 0),
                    _last_result.get("purged", 0),
                    _last_result.get("fetched_url"),
                )
        except Exception as e:
            logger.warning("gdelt loop iteration failed: %s", e)
            _last_result = {"error": str(e), "inserted": 0}
            _last_run_at = datetime.now(timezone.utc)
        _stop_event.wait(POLL_INTERVAL_S)


def start_worker(tenant_id: str = "default"):
    """Start the GKG sync thread. Tenant id is unused — GDELT is global."""
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_gkg_loop, daemon=True, name="gdelt-gkg")
    _thread.start()
    logger.info("GDELT GKG worker started (interval %ds)", POLL_INTERVAL_S)


def stop_worker():
    global _thread
    _stop_event.set()
    if _thread:
        _thread.join(timeout=5)
        _thread = None
    logger.info("GDELT GKG worker stopped")


def get_status() -> dict:
    return {
        "running": _thread is not None and _thread.is_alive(),
        "interval_s": POLL_INTERVAL_S,
        "last_run_at": _last_run_at.isoformat() if _last_run_at else None,
        "last_result": _last_result or None,
    }
