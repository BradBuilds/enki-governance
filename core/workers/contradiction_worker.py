"""Contradiction after-call worker — proves contradictions by juxtaposition.

A low-cadence after-call (NOT per-document): periodically embeds the pool's
atomic_statements, clusters them by proposition, and writes proven contradictions
(opposite deterministic polarity on a shared proposition) to
statement_contradictions. No judgment model — embeddings are geometry, the
verdict is the polarity flip, and the evidence is two verbatim quotes.

Runs only when new statements have appeared since the last pass, so it doesn't
re-embed needlessly. Uses the local embedding model (nomic-embed-text); on a
single-GPU node this should run in a phase where the extractor is idle.
"""
from __future__ import annotations

import importlib.util
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.contradiction")

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS: dict = {
    "running": False, "phase": "idle", "last_run": None,
    "ticks": 0, "contradictions_total": 0, "errors": 0,
}
_TICK_INTERVAL_S = 600     # 10 min — this is an after-call, not hot path
_SCRIPT = "/app/scripts/compare_statements.py"
_last_statement_count = -1


def _load_comparator():
    spec = importlib.util.spec_from_file_location("compare_statements", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_loop, args=(tenant_id,), name="contradiction-aftercall", daemon=True)
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    return {"status": "started"}


def stop_worker():
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=30)
    _STATUS["running"] = False
    return {"status": "stopped"}


def _statement_count(tenant="default") -> int:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM atomic_statements WHERE tenant_id=%s", (tenant,))
        return cur.fetchone()[0]


def run_tick(tenant: str = "default", sim: float = 0.80) -> dict:
    """Compare only if statements changed since last pass."""
    global _last_statement_count
    n = _statement_count(tenant)
    if n == _last_statement_count or n < 2:
        return {"ran": False, "statements": n, "contradictions": 0}
    cmp = _load_comparator()
    stmts = cmp.load_from_db(tenant)
    groups = cmp.cluster(stmts, sim)
    res = cmp._compare(stmts, groups)
    persisted = cmp.persist_contradictions(res["contradictions"], tenant)
    _last_statement_count = n
    return {"ran": True, "statements": n, "contradictions": persisted,
            "found": len(res["contradictions"])}


def _loop(tenant_id: str):
    while not _STOP_EVENT.is_set():
        try:
            stats = run_tick(tenant_id)
            _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
            _STATUS["ticks"] += 1
            if stats.get("ran"):
                _STATUS["contradictions_total"] += stats.get("contradictions", 0)
                _STATUS["phase"] = f"compared {stats['statements']} statements → {stats.get('found', 0)} contradictions"
            else:
                _STATUS["phase"] = f"idle — no new statements ({stats['statements']})"
        except Exception as e:
            log.warning("contradiction tick failed: %s", e)
            _STATUS["errors"] += 1
            _STATUS["phase"] = f"error: {str(e)[:60]}"
        for _ in range(_TICK_INTERVAL_S):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)
    _STATUS["running"] = False
