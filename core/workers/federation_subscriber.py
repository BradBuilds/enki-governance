"""Federation subscriber — closes the redirect-propagation loop.

The publisher side already works: Node A fissions an entity, posts the
redirect to the hub. This worker pulls the hub's recent redirects every
N minutes and applies them locally — so Node B, which has its own local
entity_id mapping to the same canonical key, automatically rebinds when
Node A's decision lands.

Also pulls recent membership publications + conflict broadcasts so the
node's local view of the network stays fresh without manual queries.

Run modes (same pattern as the other workers):
  start_worker(tenant_id) / stop_worker / get_status / run_tick

ENKI_HUB_URL gated — no-op in single-node mode.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("enki.workers.federation_subscriber")

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS: dict = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "ticks": 0,
    "redirects_applied": 0,
    "redirects_skipped": 0,
    "errors": 0,
    "last_error": None,
}

_TICK_INTERVAL_S = 900       # 15 min
_IDLE_BACKOFF_S = 1800       # 30 min when nothing new


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default") -> dict:
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    if not os.getenv("ENKI_HUB_URL"):
        return {"status": "disabled", "reason": "ENKI_HUB_URL not set"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_loop, args=(tenant_id,),
        name="federation-subscriber", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    log.info("Federation subscriber started")
    return {"status": "started"}


def stop_worker() -> dict:
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=10)
    _STATUS["running"] = False
    return {"status": "stopped"}


def _loop(tenant_id: str) -> None:
    backoff = _TICK_INTERVAL_S
    while not _STOP_EVENT.is_set():
        try:
            res = run_tick(tenant_id=tenant_id)
            _STATUS["ticks"] += 1
            _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
            _STATUS["redirects_applied"] += res.get("redirects_applied", 0)
            _STATUS["redirects_skipped"] += res.get("redirects_skipped", 0)
            _STATUS["phase"] = (
                f"tick: {res.get('redirects_applied',0)} applied, "
                f"{res.get('redirects_skipped',0)} skipped"
            )
            backoff = _IDLE_BACKOFF_S if res.get("redirects_applied", 0) == 0 else _TICK_INTERVAL_S
        except Exception as e:
            log.warning("federation_subscriber tick failed: %s", e)
            _STATUS["errors"] += 1
            _STATUS["last_error"] = str(e)[:300]
            _STATUS["phase"] = f"error: {str(e)[:60]}"
            backoff = _IDLE_BACKOFF_S

        for _ in range(int(backoff)):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)
    _STATUS["running"] = False


def run_tick(*, tenant_id: str = "default", lookback_seconds: int = 7200) -> dict:
    """Pull recent redirects from the hub. For each one this node has
    matching local entities for, apply the rebind."""
    from core.federation.phase4 import (
        fetch_recent_redirects, apply_remote_redirect, NODE_ID,
    )
    redirects = fetch_recent_redirects(since_seconds=lookback_seconds)
    applied = 0
    skipped = 0
    no_match = 0

    for r in redirects:
        # Skip redirects that originated from THIS node
        if r.get("originating_node_id") == NODE_ID:
            skipped += 1
            continue
        origin = r.get("origin_canonical_key")
        target = r.get("target_canonical_key")
        if not origin or not target:
            continue
        try:
            result = apply_remote_redirect(origin, target, tenant_id=tenant_id)
            if result.get("ok") and result.get("status") not in ("already_aligned", "no_local_match"):
                applied += 1
                log.info(
                    "applied remote redirect: %s -> %s (originating %s)",
                    origin[:8], target[:8], r.get("originating_node_id"),
                )
            elif result.get("status") == "no_local_match":
                no_match += 1
            else:
                skipped += 1
        except Exception:
            log.exception("apply remote redirect failed: %s -> %s", origin, target)

    return {
        "redirects_fetched": len(redirects),
        "redirects_applied": applied,
        "redirects_skipped": skipped,
        "redirects_no_local_match": no_match,
    }
