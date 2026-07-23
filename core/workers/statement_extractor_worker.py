"""Atomic statement extraction worker — the offline two-pass layer, always-on.

Mirrors collapse_worker: a continuous tick loop that finds library documents
which don't yet have atomic_statements and runs the offline extractor over them
(local model only — no frontier API). The extractor persists faithful, gated
statements to the atomic_statements table (dedup on quote hash).

This is the "who said what" evidence layer the contradiction after-call
(contradiction_worker) reasons over. Fully local: the model only transcribes.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.statement_extractor")

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS: dict = {
    "running": False, "phase": "idle", "last_run": None,
    "ticks": 0, "docs_processed_total": 0, "statements_total": 0, "errors": 0,
}
_TICK_INTERVAL_S = 60
_TICK_MAX_DOCS = 2          # docs per tick — extraction is heavy, keep bounded
_SCRIPT = "/app/scripts/atomic_extract.py"


def _load_extractor():
    spec = importlib.util.spec_from_file_location("atomic_extract", _SCRIPT)
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
        target=_loop, args=(tenant_id,), name="statement-extractor", daemon=True)
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    return {"status": "started"}


def stop_worker():
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=30)
    _STATUS["running"] = False
    return {"status": "stopped"}


def _eligible_documents(limit: int) -> list[int]:
    """Library documents that have no atomic_statements yet."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT lm.member_id AS document_id
            FROM library_members lm
            JOIN documents d ON d.document_id = lm.member_id
            WHERE lm.member_kind = 'document'
              AND d.created_at < NOW() - INTERVAL '30 seconds'
              AND NOT EXISTS (SELECT 1 FROM atomic_statements a
                               WHERE a.document_id = lm.member_id)
            ORDER BY lm.member_id
            LIMIT %s
            """, (limit,))
        return [r[0] for r in cur.fetchall()]


def run_tick(max_docs: int = _TICK_MAX_DOCS) -> dict:
    docs = _eligible_documents(max_docs)
    if not docs:
        return {"docs_processed": 0, "statements": 0}
    ext = _load_extractor()
    processed, stmts = 0, 0
    for did in docs:
        if _STOP_EVENT.is_set():
            break
        try:
            r = ext.run(did)
            stmts += r["funnel"].get("persisted", 0)
            processed += 1
        except Exception as e:
            log.warning("statement extraction failed for doc %s: %s", did, e)
            _STATUS["errors"] += 1
    return {"docs_processed": processed, "statements": stmts}


def _loop(tenant_id: str):
    while not _STOP_EVENT.is_set():
        try:
            stats = run_tick(_TICK_MAX_DOCS)
            _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
            _STATUS["ticks"] += 1
            _STATUS["docs_processed_total"] += stats["docs_processed"]
            _STATUS["statements_total"] += stats["statements"]
            _STATUS["phase"] = f"idle — {stats['docs_processed']} docs / {stats['statements']} statements last tick"
        except Exception as e:
            log.warning("statement tick failed: %s", e)
            _STATUS["errors"] += 1
            _STATUS["phase"] = f"error: {str(e)[:60]}"
        for _ in range(_TICK_INTERVAL_S):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)
    _STATUS["running"] = False
