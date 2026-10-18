"""On-demand library processing — the "click → Process" action.

A user opens a catalog library and hits Process; the node runs the full chain on
its manifest (or a slice = "grab some"): acquire the documents from archive.org,
ingest the PDFs, then collapse them (mentions → entities → statements). Runs in a
background thread; progress is written to libraries.notes.process so it is
visible across uvicorn worker processes (and to the live GPU/compute panel).

This is user-driven and independent of the background worker fleet (which stays
disabled on controlled nodes) — the collapse output is deterministic + sealable,
so a processed library can be shared and other nodes need not re-mine it.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.request
from datetime import datetime, timezone

from storage.postgres.db import get_connection
from core.discovery import acquirer, pdf_ingest_bridge
from core.workers.collapse_worker import process_document

_RUNNING: set[int] = set()          # per-process reentrancy guard


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _merge_status(library_id: int, patch: dict) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT notes FROM libraries WHERE library_id=%s", (library_id,))
            row = cur.fetchone()
            notes = row[0] if isinstance(row[0], dict) else json.loads(row[0] or "{}")
            proc = notes.get("process") or {}
            proc.update(patch)
            proc["updated_at"] = _now()
            notes["process"] = proc
            cur.execute("UPDATE libraries SET notes=%s WHERE library_id=%s",
                        (json.dumps(notes), library_id))
        conn.commit()


def _library_doc_ids(library_id: int) -> list[int]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT member_id FROM library_members "
                "WHERE library_id=%s AND member_kind='document' ORDER BY member_id",
                (library_id,))
            return [r[0] for r in cur.fetchall()]


def _counts(library_id: int) -> tuple[int, int]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT
                     (SELECT count(*) FROM document_mentions dm
                        JOIN library_members lm ON lm.member_id=dm.document_id
                         AND lm.member_kind='document' WHERE lm.library_id=%s),
                     (SELECT count(*) FROM statements s
                        JOIN library_members lm ON lm.member_id=s.document_id
                         AND lm.member_kind='document' WHERE lm.library_id=%s)""",
                (library_id, library_id))
            return cur.fetchone()


def process_library(library_id: int, limit: int) -> None:
    try:
        _merge_status(library_id, {"phase": "acquiring", "started_at": _now(),
                                   "limit": limit, "error": None})
        acq = acquirer.run_queue(library_id=library_id, limit=limit)
        _merge_status(library_id, {"phase": "ingesting",
                                   "acquired": acq.get("acquired") if isinstance(acq, dict) else None})
        pdf_ingest_bridge.run_queue(library_id=library_id, limit=limit)

        docs = _library_doc_ids(library_id)
        _merge_status(library_id, {"phase": "collapsing",
                                   "docs_total": len(docs), "docs_done": 0})
        for i, did in enumerate(docs):
            try:
                process_document(did, library_id)
            except Exception:
                pass
            m, s = _counts(library_id)
            _merge_status(library_id, {"docs_done": i + 1, "mentions": m, "statements": s})
        _merge_status(library_id, {"phase": "done"})
    except Exception as e:
        _merge_status(library_id, {"phase": "error", "error": str(e)})
    finally:
        _RUNNING.discard(library_id)


def start_process(library_id: int, limit: int = 10) -> dict:
    if library_id in _RUNNING:
        return {"started": False, "reason": "already running", "library_id": library_id}
    _RUNNING.add(library_id)
    threading.Thread(target=process_library, args=(library_id, limit), daemon=True).start()
    return {"started": True, "library_id": library_id, "limit": limit}


# ─── live GPU + compute stats ────────────────────────────────────────

def _ollama_vram() -> list[dict]:
    base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    try:
        with urllib.request.urlopen(f"{base}/api/ps", timeout=4) as r:
            data = json.load(r)
        return [{"model": m.get("name"),
                 "vram_gb": round((m.get("size_vram") or 0) / 1e9, 2)}
                for m in data.get("models", [])]
    except Exception:
        return []


def _host_gpu() -> dict | None:
    """Real GPU util/mem/temp/power, written by a host-side nvidia-smi feed
    into storage/ (the API container has no GPU access itself)."""
    try:
        with open("/app/storage/gpu_stats.json") as f:
            return json.load(f)
    except Exception:
        return None


def compute_stats() -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT library_id, notes::jsonb->>'display_title', notes::jsonb->'process' "
                "FROM libraries "
                "WHERE notes::jsonb->'process'->>'phase' IS NOT NULL "
                "  AND notes::jsonb->'process'->>'phase' NOT IN ('done','error')")
            active = [{"library_id": r[0], "title": r[1], "process": r[2]}
                      for r in cur.fetchall()]
    return {"gpu": {"host": _host_gpu(), "ollama": _ollama_vram()},
            "processing": active}
