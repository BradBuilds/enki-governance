"""
Mention Backfill — resolve document_mentions outside the library workflow.

Phase 3.2 of the internal pipeline restart plan.

The boundary failure this fixes:
    `core/workers/collapse_worker._resolve_mentions()` already does the
    surface-form → entity resolution well, but it is only called from
    `process_document()`, which itself only runs for documents that
    entered the system via the library_members workflow. PDFs ingested
    directly into the documents table (bulk Acme corpus, FOIA dumps,
    case-file imports) skip that path entirely, so 97% of their
    extracted mentions sit unresolved forever.

This worker scans documents that have at least one unresolved mention
and runs `_resolve_mentions(document_id)` on each. Idempotent —
processed mentions get `entity_id` populated; further runs see fewer
candidates.

Tier 1 — always runs (no LLM dependency; entity resolution is rule +
gazetteer + identifier lookup).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.mention_backfill")

CHECK_INTERVAL = 60   # 1 min between sweeps when work exists
IDLE_MAX = 600        # cap empty-loop sleep at 10 min
BATCH_DOCS = 20       # process this many documents per tick

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()

_STATUS = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "tick_count": 0,
    "docs_processed_total": 0,
    "mentions_scanned_total": 0,
    "mentions_resolved_total": 0,
    "mentions_skipped_total": 0,
    "errors": 0,
    "last_error": None,
}


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default") -> dict:
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_worker_loop, args=(tenant_id,),
        name="mention-backfill", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    log.info("Mention backfill started")
    return {"status": "started"}


def stop_worker() -> dict:
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=10)
    _STATUS["running"] = False
    return {"status": "stopped"}


def _worker_loop(tenant_id: str) -> None:
    backoff = CHECK_INTERVAL
    while not _STOP_EVENT.is_set():
        try:
            _STATUS["phase"] = "scanning"
            docs_processed = _backfill_batch(tenant_id, BATCH_DOCS)
            _STATUS["tick_count"] += 1
            _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
            _STATUS["last_error"] = None
            if docs_processed > 0:
                backoff = CHECK_INTERVAL
                _STATUS["phase"] = f"backfilled {docs_processed} doc(s)"
            else:
                backoff = min(backoff + 30, IDLE_MAX)
                _STATUS["phase"] = "idle — no unresolved mentions"
        except Exception as e:
            log.warning("Mention backfill tick failed: %s", e)
            _STATUS["errors"] += 1
            _STATUS["last_error"] = str(e)[:300]
            _STATUS["phase"] = f"error: {str(e)[:60]}"
            backoff = min(backoff * 2, IDLE_MAX)

        for _ in range(int(backoff)):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)

    _STATUS["running"] = False


def _backfill_batch(tenant_id: str, batch: int) -> int:
    """Find documents with unresolved mentions, run the resolver. Returns docs processed."""
    # Reuse the existing resolver in collapse_worker — same code path
    # that the library workflow uses, just invoked outside of it.
    from core.workers.collapse_worker import _resolve_mentions

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT dm.document_id
                  FROM document_mentions dm
                  JOIN documents d ON d.document_id = dm.document_id
                 WHERE dm.entity_id IS NULL
                   AND dm.resolution_status = 'unresolved'
                   AND d.tenant_id = %s
                 ORDER BY dm.document_id
                 LIMIT %s
                """,
                (tenant_id, batch),
            )
            rows = cur.fetchall()

    if not rows:
        return 0

    processed = 0
    for (doc_id,) in rows:
        if _STOP_EVENT.is_set():
            break
        try:
            stats = _resolve_mentions(doc_id)
            _STATUS["mentions_scanned_total"] += int(stats.get("scanned", 0))
            _STATUS["mentions_resolved_total"] += int(stats.get("resolved", 0))
            _STATUS["mentions_skipped_total"] += int(stats.get("skipped", 0))
            _STATUS["errors"] += int(stats.get("errors", 0))
            processed += 1
            if stats.get("resolved", 0) > 0:
                log.info(
                    "doc %d: scanned=%d resolved=%d skipped=%d errors=%d",
                    doc_id, stats.get("scanned", 0), stats.get("resolved", 0),
                    stats.get("skipped", 0), stats.get("errors", 0),
                )
        except Exception as e:
            log.warning("backfill failed for document %d: %s", doc_id, e)
            _STATUS["errors"] += 1
            _STATUS["last_error"] = str(e)[:300]
            continue

    _STATUS["docs_processed_total"] += processed
    return processed
