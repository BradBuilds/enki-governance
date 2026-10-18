"""Hub catalog janitor — keeps hub_node_hosting health_status fresh.

Runs only on the hub (where hub_node_hosting lives). Peers don't run it.

Cadence: every 6 hours.

Two passes:
  1. mark stale: hosts with no announce in STALE_AFTER_DAYS get
     health_status='stale'. Other peers' library_subscriber discovery
     filters these out, so a flaky host stops getting traffic without
     being permanently removed.
  2. prune dead: hosts stale for PRUNE_AFTER_DAYS get deleted. The
     catalog entry itself stays if other hosts remain; otherwise the
     catalog entry can be deleted too (deferred — catalog cleanup is
     a separate decision since it's part of the federation history).

Schema (migration 115):
    hub_node_hosting(federation_id, node_id, manifest_url,
                     manifest_sha256, last_announced_at, health_status,
                     ...)

The peer's hub_announce_worker runs every 30 min. So STALE_AFTER_DAYS=2
is a generous 96 missed announces before flagging stale. PRUNE_AFTER_DAYS=30
is a month — long enough that a temporarily-offline host doesn't lose its
slot.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

log = logging.getLogger("enki.workers.hub_catalog_janitor")

CHECK_INTERVAL_SECONDS = 6 * 60 * 60  # 6 h
STALE_AFTER_DAYS = 2
PRUNE_AFTER_DAYS = 30

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS = {
    "running":              False,
    "phase":                "idle",
    "last_run_at":          None,
    "last_marked_stale":    0,
    "last_pruned":          0,
    "total_marked_stale":   0,
    "total_pruned":         0,
    "errors":               0,
}


def get_status() -> dict:
    return dict(_STATUS)


def start_worker():
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_worker_loop, name="hub-catalog-janitor", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    log.info("Hub catalog janitor started (every %ds, stale=%dd, prune=%dd)",
             CHECK_INTERVAL_SECONDS, STALE_AFTER_DAYS, PRUNE_AFTER_DAYS)
    return {"status": "started"}


def stop_worker():
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=10)
    _STATUS["running"] = False
    return {"status": "stopped"}


def _worker_loop():
    _STATUS["running"] = True
    # Wait 60s after boot so we don't race startup load.
    for _ in range(60):
        if _STOP_EVENT.is_set():
            _STATUS["running"] = False
            return
        time.sleep(1)
    while not _STOP_EVENT.is_set():
        try:
            _STATUS["phase"] = "scanning"
            r = run_once()
            _STATUS["last_run_at"]      = time.time()
            _STATUS["last_marked_stale"] = r.get("marked_stale", 0)
            _STATUS["last_pruned"]       = r.get("pruned", 0)
            _STATUS["total_marked_stale"] += r.get("marked_stale", 0)
            _STATUS["total_pruned"]       += r.get("pruned", 0)
            _STATUS["phase"] = "idle"
        except Exception as e:
            log.warning("hub catalog janitor error: %s", e)
            _STATUS["errors"] += 1
            _STATUS["phase"] = f"error: {str(e)[:60]}"
        slept = 0
        while slept < CHECK_INTERVAL_SECONDS and not _STOP_EVENT.is_set():
            time.sleep(1)
            slept += 1
    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"


def run_once() -> dict:
    """One janitor pass: mark stale + prune dead. Safe to call manually
    for tests / one-off cleanup. Skipped silently if hub_node_hosting
    table doesn't exist (peer-side install)."""
    from storage.postgres.db import get_connection

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 1 FROM information_schema.tables
                     WHERE table_name = 'hub_node_hosting'
                """)
                if cur.fetchone() is None:
                    return {"skipped": True,
                            "reason": "hub_node_hosting table not present (peer-side install)"}

                cur.execute(
                    f"""
                    UPDATE hub_node_hosting
                       SET health_status = 'stale'
                     WHERE health_status = 'fresh'
                       AND last_announced_at < NOW() - INTERVAL '{STALE_AFTER_DAYS} days'
                    """
                )
                marked_stale = cur.rowcount

                cur.execute(
                    f"""
                    DELETE FROM hub_node_hosting
                     WHERE health_status IN ('stale', 'unreachable')
                       AND last_announced_at < NOW() - INTERVAL '{PRUNE_AFTER_DAYS} days'
                    """
                )
                pruned = cur.rowcount

                conn.commit()
    except Exception as e:
        log.warning("hub_catalog_janitor run_once failed: %s", e)
        return {"error": str(e)}

    log.info("hub catalog janitor: marked %d stale, pruned %d", marked_stale, pruned)
    return {"marked_stale": marked_stale, "pruned": pruned}
