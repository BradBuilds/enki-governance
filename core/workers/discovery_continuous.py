"""discovery_continuous.py — continuous acquire + ingest daemon.

Two-step pipeline that nothing was driving on a cadence:

  1. acquire  pending → acquired   download PDFs from refs URL
  2. ingest   acquired → ingested  PDF→text→document_paragraphs +
                                   link into library_members

Focus-aware: when library_manager has an active run, processes that
library's refs first; otherwise round-robins.

This is the worker that turns the 940-ref / 9-doc backlog into facts.
Without it, the pipeline sits idle on 122+ acquired PDFs.

Run modes:
    python -m core.workers.discovery_continuous            # one tick
    python -m core.workers.discovery_continuous --once
    python -m core.workers.discovery_continuous --continuous   (the daemon)

Also: start_worker() / stop_worker() for FastAPI startup.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.discovery_continuous")

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS: dict = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "ticks": 0,
    "acquired_total": 0,
    "ingested_total": 0,
    "errors": 0,
}

# Per-tick caps — keep modest so a stuck PDF doesn't block the whole loop
_ACQUIRE_BATCH = 6
_INGEST_BATCH = 4
_TICK_INTERVAL_S = 60


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_loop, args=(tenant_id,),
        name="discovery-continuous", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    return {"status": "started"}


def stop_worker():
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=10)
    _STATUS["running"] = False
    return {"status": "stopped"}


def _loop(tenant_id: str):
    while not _STOP_EVENT.is_set():
        try:
            stats = run_once()
            _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
            _STATUS["acquired_total"] += stats.get("acquired", 0)
            _STATUS["ingested_total"] += stats.get("ingested", 0)
            _STATUS["ticks"] += 1
            _STATUS["phase"] = (
                f"idle — acquired={stats.get('acquired',0)} "
                f"ingested={stats.get('ingested',0)} last tick"
            )
        except Exception as e:
            log.warning("tick failed: %s", e)
            _STATUS["errors"] += 1
            _STATUS["phase"] = f"error: {str(e)[:60]}"
        for _ in range(_TICK_INTERVAL_S):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)
    _STATUS["running"] = False


def _focused_library_id() -> Optional[int]:
    """Read the current focus from library_manager — if a run is active,
    we prioritize that library's refs."""
    try:
        from core.workers.library_manager import get_current_focus
        focus = get_current_focus()
        if focus:
            return focus.get("library_id")
    except Exception:
        pass
    return None


def run_once() -> dict:
    """One pass: acquire some pending refs, then ingest some acquired ones."""
    stats = {"acquired": 0, "acquire_failed": 0, "ingested": 0, "ingest_failed": 0}
    focus = _focused_library_id()

    # ── Step 1: acquire pending refs (focused first)
    try:
        from core.discovery.acquirer import run_queue
        if focus is not None:
            # Try focused library first
            r = run_queue(library_id=focus, limit=_ACQUIRE_BATCH)
            stats["acquired"] += r.get("acquired", 0)
            stats["acquire_failed"] += r.get("failed", 0)
            # Fill any remaining budget with ambient work
            remaining = _ACQUIRE_BATCH - r.get("scanned", 0)
            if remaining > 0:
                r2 = run_queue(library_id=None, limit=remaining)
                stats["acquired"] += r2.get("acquired", 0)
                stats["acquire_failed"] += r2.get("failed", 0)
        else:
            r = run_queue(library_id=None, limit=_ACQUIRE_BATCH)
            stats["acquired"] += r.get("acquired", 0)
            stats["acquire_failed"] += r.get("failed", 0)
    except Exception as e:
        log.warning("acquire step failed: %s", e)
        stats["acquire_failed"] += 1

    # ── Step 2: ingest acquired refs (PDF → text → mentions, focused first)
    try:
        from core.discovery.pdf_ingest_bridge import ingest_acquired_reference
        ids = _pick_ingest_candidates(focus_library_id=focus,
                                      limit=_INGEST_BATCH)
        for ref_id in ids:
            try:
                r = ingest_acquired_reference(ref_id)
                if r.get("ok"):
                    stats["ingested"] += 1
                else:
                    stats["ingest_failed"] += 1
                    log.debug("ingest skipped ref %d: %s", ref_id,
                              r.get("error"))
            except Exception:
                log.exception("ingest crashed for ref %d", ref_id)
                stats["ingest_failed"] += 1
    except Exception as e:
        log.warning("ingest step failed: %s", e)
        stats["ingest_failed"] += 1

    log.info(
        "tick done (focus=%s): acquired=%d/%d ingested=%d/%d",
        focus,
        stats["acquired"], stats["acquired"] + stats["acquire_failed"],
        stats["ingested"], stats["ingested"] + stats["ingest_failed"],
    )
    return stats


def _pick_ingest_candidates(focus_library_id: Optional[int],
                            limit: int) -> list[int]:
    """Pick acquired refs to ingest — focused library first."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT reference_id
                  FROM bibliographic_references
                 WHERE acquisition_status = 'acquired'
                 ORDER BY
                   CASE WHEN %s IS NOT NULL AND library_id = %s THEN 0 ELSE 1 END,
                   reference_id
                 LIMIT %s
                """,
                (focus_library_id, focus_library_id, limit),
            )
            return [r[0] for r in cur.fetchall()]


if __name__ == "__main__":
    import argparse, json
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )
    p = argparse.ArgumentParser()
    p.add_argument("--continuous", action="store_true")
    args = p.parse_args()
    if args.continuous:
        log.info("continuous mode — tick every %ds", _TICK_INTERVAL_S)
        while True:
            try:
                stats = run_once()
                print(json.dumps(stats))
            except Exception as e:
                log.error("cycle failed: %s", e)
            time.sleep(_TICK_INTERVAL_S)
    else:
        print(json.dumps(run_once(), indent=2))
