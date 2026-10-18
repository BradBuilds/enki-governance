"""
Library work assigner — hub-side worker that assigns unprocessed
library documents to active subscriber nodes.

Cross-verification design (Phase 11): each (library_id, document_id) is
assigned to at least ``MIN_ASSIGNMENTS_PER_DOC`` distinct subscribers so
that results can be compared. Consensus → yield reward; mismatch →
contested-flag for human or LLM review.

Ticks every 5 minutes. Cheap O(N) scan keyed on libraries with active
subscribers. Skips when no public libraries / no active subscriptions.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Optional

_log = logging.getLogger("enki.workers.library_work_assigner")

_stop_event = threading.Event()
_thread: Optional[threading.Thread] = None
_last_result: dict = {}
_last_run_at: Optional[str] = None

POLL_INTERVAL = 300
MIN_ASSIGNMENTS_PER_DOC = 2
MAX_ASSIGNMENTS_PER_TICK = 200


def _assign_tick() -> dict:
    from storage.postgres.db import get_connection

    assigned = 0
    skipped_no_subs = 0
    libraries_walked = 0

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Pull every public library that has at least one active
            # subscriber.
            cur.execute(
                """
                SELECT DISTINCT l.library_id
                FROM libraries l
                JOIN library_subscriptions s ON s.library_id = l.library_id
                WHERE l.is_public = TRUE
                  AND s.status = 'active'
                """,
            )
            lib_ids = [r[0] for r in cur.fetchall()]

            for lib_id in lib_ids:
                libraries_walked += 1
                # Active subscribers for this library.
                cur.execute(
                    """
                    SELECT subscriber_node_id FROM library_subscriptions
                    WHERE library_id = %s AND status = 'active'
                    ORDER BY subscribed_at ASC
                    """,
                    (lib_id,),
                )
                subs = [r[0] for r in cur.fetchall()]
                if len(subs) < MIN_ASSIGNMENTS_PER_DOC:
                    skipped_no_subs += 1
                    continue

                # Candidate docs: members of this library that don't
                # yet have MIN_ASSIGNMENTS_PER_DOC assignments.
                cur.execute(
                    """
                    SELECT lm.document_id
                    FROM library_members lm
                    WHERE lm.library_id = %s
                      AND lm.document_id IS NOT NULL
                      AND (
                        SELECT count(*) FROM library_work_assignments a
                        WHERE a.library_id = %s
                          AND a.document_id = lm.document_id
                      ) < %s
                    ORDER BY lm.added_at DESC NULLS LAST
                    LIMIT %s
                    """,
                    (lib_id, lib_id, MIN_ASSIGNMENTS_PER_DOC,
                     MAX_ASSIGNMENTS_PER_TICK),
                )
                doc_ids = [r[0] for r in cur.fetchall()]

                for doc_id in doc_ids:
                    # Who's already assigned this doc?
                    cur.execute(
                        """
                        SELECT subscriber_node_id
                        FROM library_work_assignments
                        WHERE library_id = %s AND document_id = %s
                        """,
                        (lib_id, doc_id),
                    )
                    already = {r[0] for r in cur.fetchall()}

                    # Pick subscribers not yet on this doc, round-robin
                    # by subscribed_at order.
                    new_subs = [s for s in subs if s not in already]
                    needed = MIN_ASSIGNMENTS_PER_DOC - len(already)
                    if needed <= 0 or not new_subs:
                        continue
                    for node_id in new_subs[:needed]:
                        cur.execute(
                            """
                            INSERT INTO library_work_assignments
                              (library_id, document_id, subscriber_node_id,
                               status)
                            VALUES (%s, %s, %s, 'assigned')
                            ON CONFLICT (library_id, document_id,
                                          subscriber_node_id) DO NOTHING
                            """,
                            (lib_id, doc_id, node_id),
                        )
                        if cur.rowcount:
                            assigned += 1
                            if assigned >= MAX_ASSIGNMENTS_PER_TICK:
                                break
                    if assigned >= MAX_ASSIGNMENTS_PER_TICK:
                        break
                if assigned >= MAX_ASSIGNMENTS_PER_TICK:
                    break
        conn.commit()

    return {
        "libraries_walked": libraries_walked,
        "skipped_no_subs": skipped_no_subs,
        "assigned": assigned,
    }


def _worker_loop():
    while not _stop_event.is_set():
        try:
            result = _assign_tick()
            global _last_result, _last_run_at
            _last_result = result
            _last_run_at = datetime.now(timezone.utc).isoformat()
            if result.get("assigned", 0):
                _log.info("library_work_assigner tick: %s", result)
        except Exception:
            _log.exception("library_work_assigner tick failed")
        _stop_event.wait(POLL_INTERVAL)


def start_library_work_assigner(tenant_id: str = "default") -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(
        target=_worker_loop, name="library-work-assigner", daemon=True,
    )
    _thread.start()
    _log.info("library_work_assigner started")


def stop_library_work_assigner() -> None:
    _stop_event.set()
    global _thread
    if _thread:
        _thread.join(timeout=5)
        _thread = None


def get_library_work_assigner_status() -> dict:
    return {
        "running": bool(_thread and _thread.is_alive()),
        "last_run_at": _last_run_at,
        "last_result": dict(_last_result),
        "poll_interval_sec": POLL_INTERVAL,
    }
