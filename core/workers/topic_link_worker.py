"""
Topic Library Linking Worker.

Walks every library with a linking_rule and ensures the documents
matching that rule are present in library_members. This is what makes
topic libraries auto-aggregate across source corpora — when a new doc
lands in any corpus (FBI, USAF, Courts, SEC, ...), if it matches a
topic library's rule it auto-appears in that topic library too.

Rule shape (libraries.linking_rule JSONB):
  {
    "focal_entity_ids": [101, 102],          # canonical entity IDs
    "focal_keywords":   ["Acme", "Doe"],
    "match_on":         ["mentions", "title", "extracted_text"]
  }

Matching logic:
  - focal_entity_ids matches a document iff any of those entity_ids
    appears in document_mentions for that document (with entity_id
    bound, not the unresolved bucket — but the resolver runs separately
    and binds incrementally, so unresolved mentions become bound over
    time and topic libraries pick them up on the next worker pass).

  - focal_keywords (case-insensitive) matches a document iff the doc's
    title OR (when match_on includes 'extracted_text') the first 4096
    chars of any page text contain the keyword as a substring.

Idempotent: re-running doesn't create duplicate members
(library_members has a UNIQUE constraint on (library_id, member_kind,
member_id) — we use ON CONFLICT DO NOTHING).

Status surface mirrors place_validator / data_integrity_validator etc.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.topic_link")

# Default cadence: every 10 minutes. Cheap to run on small corpora; for
# a corpus of millions of docs this will need incremental indexing
# (the last_linked_at column exists for that — we just don't use it
# yet because node-A's corpus is small).
CHECK_INTERVAL_SECONDS = 10 * 60

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS = {
    "running":          False,
    "phase":            "idle",
    "last_run_at":      None,
    "libraries_linked": 0,
    "docs_added":       0,
    "errors":           0,
}


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_worker_loop, name="topic-link", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    log.info("Topic link worker started (every %ds)", CHECK_INTERVAL_SECONDS)
    return {"status": "started"}


def stop_worker():
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=10)
    _STATUS["running"] = False
    return {"status": "stopped"}


def _worker_loop():
    _STATUS["running"] = True
    # Short initial wait to let the API finish booting.
    for _ in range(15):
        if _STOP_EVENT.is_set():
            _STATUS["running"] = False
            return
        time.sleep(1)

    while not _STOP_EVENT.is_set():
        try:
            _STATUS["phase"] = "linking"
            stats = run_once()
            _STATUS["libraries_linked"] = stats.get("libraries_processed", 0)
            _STATUS["docs_added"]       = stats.get("docs_added", 0)
            _STATUS["phase"]            = "idle"
        except Exception as e:
            log.warning("topic_link worker error: %s", e)
            _STATUS["errors"] += 1
            _STATUS["phase"] = f"error: {str(e)[:60]}"
        slept = 0
        while slept < CHECK_INTERVAL_SECONDS and not _STOP_EVENT.is_set():
            time.sleep(1)
            slept += 1

    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"


def run_once() -> dict:
    """Walk every library with a linking_rule, link matching docs into
    library_members. Returns counts."""
    libraries_processed = 0
    docs_added = 0
    per_lib: list[dict] = []

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT library_id, focal_term, linking_rule
                  FROM libraries
                 WHERE linking_rule IS NOT NULL
                   AND COALESCE(discovery_status, 'active') != 'archived'
            """)
            rows = cur.fetchall()

    for library_id, focal_term, rule in rows:
        try:
            added = _link_library(library_id, rule or {})
            libraries_processed += 1
            docs_added += added
            per_lib.append({
                "library_id":   library_id,
                "focal_term":   focal_term,
                "docs_added":   added,
            })
        except Exception as e:
            log.exception("topic link failed for library %d: %s", library_id, e)
            per_lib.append({
                "library_id":   library_id,
                "focal_term":   focal_term,
                "error":        str(e)[:120],
            })

    _STATUS["last_run_at"] = time.time()
    return {
        "libraries_processed":  libraries_processed,
        "docs_added":           docs_added,
        "per_library":          per_lib,
    }


def _link_library(library_id: int, rule: dict) -> int:
    """For one library, find documents matching its rule and INSERT
    them into library_members (idempotent). Returns docs_added count."""
    focal_entity_ids = rule.get("focal_entity_ids") or []
    focal_keywords   = rule.get("focal_keywords")   or []
    match_on         = set(rule.get("match_on") or ["mentions", "title"])

    matching_doc_ids: set[int] = set()

    with get_connection() as conn:
        with conn.cursor() as cur:
            # 1. Entity-based matches (strongest signal)
            if focal_entity_ids and "mentions" in match_on:
                cur.execute(
                    """
                    SELECT DISTINCT document_id
                      FROM document_mentions
                     WHERE entity_id = ANY(%s)
                       AND document_id IS NOT NULL
                    """,
                    (focal_entity_ids,),
                )
                matching_doc_ids.update(r[0] for r in cur.fetchall())

            # 2. Keyword matches in title (cheap, scales to large corpora)
            if focal_keywords and "title" in match_on:
                ilike_clauses = " OR ".join(["title ILIKE %s"] * len(focal_keywords))
                params = [f"%{kw}%" for kw in focal_keywords]
                cur.execute(
                    f"""
                    SELECT document_id FROM documents
                     WHERE ({ilike_clauses})
                       AND tenant_id = 'default'
                    """,
                    params,
                )
                matching_doc_ids.update(r[0] for r in cur.fetchall())

            # 3. Keyword matches in extracted page text (heavier)
            if focal_keywords and "extracted_text" in match_on:
                ilike_clauses = " OR ".join(["p.raw_text ILIKE %s"] * len(focal_keywords))
                params = [f"%{kw}%" for kw in focal_keywords]
                cur.execute(
                    f"""
                    SELECT DISTINCT p.document_id
                      FROM document_pages p
                     WHERE ({ilike_clauses})
                       AND p.raw_text IS NOT NULL
                    """,
                    params,
                )
                matching_doc_ids.update(r[0] for r in cur.fetchall())

            if not matching_doc_ids:
                cur.execute(
                    "UPDATE libraries SET last_linked_at = NOW() WHERE library_id = %s",
                    (library_id,),
                )
                conn.commit()
                return 0

            # INSERT into library_members idempotently.
            args_str = ",".join(cur.mogrify("(%s, 'document', %s, NOW())",
                                            (library_id, did)).decode("utf-8")
                                 for did in sorted(matching_doc_ids))
            cur.execute(
                "INSERT INTO library_members (library_id, member_kind, member_id, added_at) "
                f" VALUES {args_str} "
                " ON CONFLICT (library_id, member_kind, member_id) DO NOTHING "
                " RETURNING member_id"
            )
            added_rows = cur.fetchall()
            cur.execute(
                "UPDATE libraries SET last_linked_at = NOW() WHERE library_id = %s",
                (library_id,),
            )
            conn.commit()

    return len(added_rows)
