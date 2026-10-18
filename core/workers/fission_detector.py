"""Fission detector — background worker.

Wraps core.substrate.fission.detect_candidates in a daemon thread that
ticks every 30 minutes. Candidates that meet the conservative auto-apply
threshold (partition_score >= 0.95, >= 2 signals, first_name_conflict)
auto-fission/rebind; the rest land in fission_proposals for human review.

Different from the entity-resolution layer:
  resolver decides at INGEST time who to merge
  fission detector audits the substrate AFTER for accumulated mistakes

Public surface:
  start_worker(tenant_id) → start the daemon
  stop_worker()
  get_status()
  run_tick(max_candidates) → one-shot
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("enki.workers.fission_detector")

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS: dict = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "ticks": 0,
    "candidates_total": 0,
    "auto_applied_total": 0,
    "queued_for_review_total": 0,
    "errors": 0,
    "last_error": None,
    "by_action": {"rebind": 0, "fission": 0, "rename": 0, "review": 0},
}

_TICK_INTERVAL_S = 1800        # 30 min
_MAX_CANDIDATES_PER_TICK = 5
_IDLE_BACKOFF_S = 3600         # 1h when nothing surfaces


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default") -> dict:
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_loop, args=(tenant_id,),
        name="fission-detector", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    log.info("Fission detector started")
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
            result = run_tick(max_candidates=_MAX_CANDIDATES_PER_TICK,
                              tenant_id=tenant_id)
            _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
            _STATUS["ticks"] += 1
            _STATUS["candidates_total"] += result.get("candidates", 0)
            _STATUS["auto_applied_total"] += result.get("auto_applied", 0)
            _STATUS["queued_for_review_total"] += result.get("queued", 0)
            for action, n in result.get("by_action", {}).items():
                _STATUS["by_action"][action] = _STATUS["by_action"].get(action, 0) + n
            _STATUS["phase"] = (
                f"tick: {result.get('candidates',0)} candidates, "
                f"{result.get('auto_applied',0)} applied, "
                f"{result.get('queued',0)} queued"
            )
            backoff = _IDLE_BACKOFF_S if result.get("candidates", 0) == 0 else _TICK_INTERVAL_S
        except Exception as e:
            log.warning("fission detector tick failed: %s", e, exc_info=False)
            _STATUS["errors"] += 1
            _STATUS["last_error"] = str(e)[:300]
            _STATUS["phase"] = f"error: {str(e)[:60]}"
            backoff = _IDLE_BACKOFF_S

        for _ in range(int(backoff)):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)
    _STATUS["running"] = False


def run_tick(*, max_candidates: int = 5, tenant_id: str = "default") -> dict:
    """One tick of detection. Auto-applies qualifying candidates; queues the rest."""
    from core.substrate.fission import (
        detect_candidates, apply, rebind, propose_for_review,
        ACTION_REBIND, ACTION_FISSION, ACTION_RENAME, ACTION_REVIEW,
    )
    candidates = detect_candidates(limit=max_candidates * 4)
    if not candidates:
        return {"candidates": 0, "auto_applied": 0, "queued": 0, "by_action": {}}

    auto_applied = 0
    queued = 0
    by_action: dict[str, int] = {}

    for c in candidates[:max_candidates]:
        by_action[c.action] = by_action.get(c.action, 0) + 1
        if not c.qualifies_for_auto_apply:
            # Queue for review
            try:
                propose_for_review(c, tenant_id=tenant_id)
                queued += 1
            except Exception as e:
                log.debug("propose_for_review failed for entity %d: %s", c.entity_id, e)
            continue

        # Auto-apply path
        try:
            if c.action == ACTION_REBIND and c.rebind_target_id:
                rebind(
                    entity_id=c.entity_id,
                    target_entity_id=c.rebind_target_id,
                    reason=f"auto-fission-detector: {','.join(c.signals_fired)}",
                    tenant_id=tenant_id,
                )
                auto_applied += 1
            elif c.action == ACTION_FISSION:
                apply(c.entity_id, applied_by="fission_detector_worker",
                      tenant_id=tenant_id)
                auto_applied += 1
            else:
                # ACTION_RENAME / ACTION_REVIEW — queue for human
                propose_for_review(c, tenant_id=tenant_id)
                queued += 1
        except Exception as e:
            log.exception("fission auto-apply failed for entity %d", c.entity_id)
            queued += 1
            try:
                propose_for_review(c, tenant_id=tenant_id)
            except Exception:
                pass

    return {
        "candidates": len(candidates),
        "auto_applied": auto_applied,
        "queued": queued,
        "by_action": by_action,
    }


if __name__ == "__main__":
    import argparse, json
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--max", type=int, default=5)
    p.add_argument("--tick", action="store_true")
    args = p.parse_args()
    if args.tick:
        print(json.dumps(run_tick(max_candidates=args.max), indent=2, default=str))
