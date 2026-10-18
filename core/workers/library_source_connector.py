"""
Library source-connector worker.

Walks library_source_connectors WHERE enabled=TRUE every tick. For
each connector whose refresh_cadence has elapsed, calls the matching
handler in core.connectors.<type>, ingests each yielded item via
core.enki.ingest.ingest_document.

Ticks every 5 min. Per-connector politeness comes from refresh_cadence
(hourly / daily / weekly / static) — `static` connectors only fire
when last_run_at IS NULL.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional

_log = logging.getLogger("enki.workers.library_source_connector")

_stop_event = threading.Event()
_thread: Optional[threading.Thread] = None
_last_result: dict = {}
_last_run_at: Optional[str] = None

POLL_INTERVAL = 300  # 5 minutes

CADENCE_SECONDS = {
    "hourly":  3600,
    "daily":   24 * 3600,
    "weekly":  7 * 24 * 3600,
    "static":  None,   # never auto-refresh
}


def _resolve_handler(connector_type: str):
    """Dynamic import of the handler module by connector_type."""
    name = (connector_type or "").lower().strip()
    if not name:
        return None
    try:
        if name == "rss":
            from core.connectors import rss
            return rss
        if name == "rest_api":
            from core.connectors import rest_api
            return rest_api
        # 'web_scrape' and 'bulk_pull' need bespoke handlers — not
        # built yet; the connector stays enabled but no-ops with a
        # one-time log message.
        _log.info("no handler for connector_type=%s (yet)", name)
        return None
    except Exception as e:
        _log.warning("failed to load handler %s: %s", name, e)
        return None


def _due(refresh_cadence: str, last_run_at) -> bool:
    secs = CADENCE_SECONDS.get(refresh_cadence)
    if secs is None:
        return last_run_at is None
    if last_run_at is None:
        return True
    elapsed = (datetime.now(timezone.utc) - last_run_at).total_seconds()
    return elapsed >= secs


def _tick() -> dict:
    from storage.postgres.db import get_connection
    from core.enki.ingest import ingest_document

    connectors_walked = 0
    fetches = 0
    items_ingested = 0
    items_dedup = 0
    errors = 0

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT connector_id, library_id, connector_type, display_name,
                       config, refresh_cadence, last_run_at
                FROM library_source_connectors
                WHERE enabled = TRUE
                ORDER BY COALESCE(last_run_at, '1970-01-01'::timestamptz) ASC
                LIMIT 40
                """,
            )
            rows = cur.fetchall()

    for (cid, lib_id, ctype, dname, config, cadence, last_run) in rows:
        connectors_walked += 1
        if not _due(cadence, last_run):
            continue

        handler = _resolve_handler(ctype)
        if not handler:
            continue

        config = config or {}
        items_for_this = 0
        items_dedup_this = 0
        error_msg = None
        run_status = "ok"

        since = last_run

        try:
            for item in handler.fetch_recent(config, since=since):
                fetches += 1
                try:
                    r = ingest_document(
                        body=item["body"],
                        source_url=item["source_url"],
                        source_title=item.get("source_title"),
                        mime_type=item.get("mime_type", "text/plain"),
                        acquired_via=ctype,
                        tenant_id="default",
                        library_id=lib_id,
                        tags=[ctype, dname],
                        extra_metadata={
                            "item_id": item.get("item_id"),
                            "published_at": item.get("published_at"),
                            "connector_id": cid,
                            "connector_display_name": dname,
                        },
                    )
                    if r.was_already_present:
                        items_dedup_this += 1
                        items_dedup += 1
                    else:
                        items_for_this += 1
                        items_ingested += 1
                except Exception as e:
                    _log.warning("ingest failed for %s: %s",
                                 item.get("source_url"), e)
                    errors += 1
                    error_msg = str(e)[:200]
        except Exception as e:
            _log.exception("connector %s (cid=%s) fetch_recent crashed: %s",
                           dname, cid, e)
            errors += 1
            run_status = "error"
            error_msg = str(e)[:200]

        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE library_source_connectors SET
                          last_run_at      = NOW(),
                          last_run_status  = %s,
                          last_run_message = %s,
                          documents_ingested = documents_ingested + %s,
                          errors_count     = errors_count + %s
                        WHERE connector_id = %s
                        """,
                        (run_status, error_msg, items_for_this,
                         1 if error_msg else 0, cid),
                    )
                conn.commit()
        except Exception:
            _log.exception("failed to update connector last_run for cid=%s", cid)

        if items_for_this or error_msg:
            _log.info(
                "connector %s (lib=%s): ingested=%d dedup=%d status=%s",
                dname, lib_id, items_for_this, items_dedup_this, run_status,
            )

    return {
        "connectors_walked": connectors_walked,
        "fetches": fetches,
        "items_ingested": items_ingested,
        "items_dedup": items_dedup,
        "errors": errors,
    }


def _worker_loop():
    while not _stop_event.is_set():
        try:
            result = _tick()
            global _last_result, _last_run_at
            _last_result = result
            _last_run_at = datetime.now(timezone.utc).isoformat()
        except Exception:
            _log.exception("connector worker tick failed")
        _stop_event.wait(POLL_INTERVAL)


def start_library_source_connector(tenant_id: str = "default") -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(
        target=_worker_loop, name="library-source-connector", daemon=True,
    )
    _thread.start()
    _log.info("library_source_connector worker started")


def stop_library_source_connector() -> None:
    _stop_event.set()
    global _thread
    if _thread:
        _thread.join(timeout=5)
        _thread = None


def get_library_source_connector_status() -> dict:
    return {
        "running": bool(_thread and _thread.is_alive()),
        "last_run_at": _last_run_at,
        "last_result": dict(_last_result),
        "poll_interval_sec": POLL_INTERVAL,
    }
