"""
Event Validator Worker — sanity-checks event nodes.

For every event node, validates:
  - Temporal sanity: event_time not future, not impossibly old
  - Participant validation: linked entities must exist and be validated
  - Location validation: linked places must exist and be validated
  - Duplicate detection: same event time + place + similar title = collapse
  - Source corroboration: how many independent sources reported this event

Updates:
  - envelope.validation.status: validated | unvalidated | contested | impossible
  - envelope.evidence.source_count: documents that mention this event
  - envelope.evidence.corroboration_count: independent sources
  - completeness_score / completeness_tier

Future expansion: GDELT CAMEO event coding cross-check (when API key available).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.event_validator")

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "validated": 0,
    "duplicates_merged": 0,
    "impossible_flagged": 0,
    "errors": 0,
}

CHECK_INTERVAL = 30
BATCH_SIZE = 10


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_worker_loop, args=(tenant_id,),
        name="event-validator", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    log.info("Event validator worker started")
    return {"status": "started"}


def stop_worker():
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=10)
    _STATUS["running"] = False
    return {"status": "stopped"}


def _worker_loop(tenant_id: str):
    _STATUS["running"] = True
    backoff = CHECK_INTERVAL

    while not _STOP_EVENT.is_set():
        try:
            processed = _process_batch(tenant_id)
            if processed > 0:
                backoff = CHECK_INTERVAL
                _STATUS["phase"] = "processing"
            else:
                backoff = min(backoff + 30, 300)
                _STATUS["phase"] = "idle — no pending events"
        except Exception as e:
            log.warning("Event validator error: %s", e)
            _STATUS["errors"] += 1
            _STATUS["phase"] = f"error: {str(e)[:60]}"
            backoff = min(backoff * 2, 600)

        for _ in range(int(backoff)):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)

    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"


def _process_batch(tenant_id: str) -> int:
    nodes = _find_pending_events(tenant_id, BATCH_SIZE)
    if not nodes:
        return 0

    processed = 0
    for node in nodes:
        try:
            _validate_event(node, tenant_id)
            processed += 1
        except Exception as e:
            log.debug("Event validation failed for %s: %s", node["node_id"], e)
            _STATUS["errors"] += 1

    _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
    return processed


def _find_pending_events(tenant_id: str, limit: int) -> list:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT node_id, title, subtype, envelope, signal_rid, created_at
                    FROM knowledge_nodes
                    WHERE tenant_id = %s
                      AND node_type = 'event'
                      AND (envelope->'validation'->>'status') IS DISTINCT FROM 'validated'
                      AND title IS NOT NULL
                      AND LENGTH(title) >= 5
                    ORDER BY created_at DESC
                    LIMIT %s
                """, (tenant_id, limit))

                return [
                    {"node_id": r[0], "title": r[1], "subtype": r[2],
                     "envelope": r[3] or {}, "signal_rid": r[4],
                     "created_at": r[5]}
                    for r in cur.fetchall()
                ]
    except Exception as e:
        log.debug("Failed to find pending events: %s", e)
        return []


def _validate_event(node: dict, tenant_id: str):
    """Validate an event node against multiple criteria."""
    node_id = node["node_id"]
    title = node["title"]
    subtype = node["subtype"]
    envelope = node["envelope"] or {}
    created_at = node.get("created_at")

    validation_status = "validated"
    validation_issues: list[str] = []
    completeness_score = 0.4

    now = datetime.now(timezone.utc)

    with get_connection() as conn:
        # ── Temporal sanity ───────────────────────────────────────
        event_time = envelope.get("temporal", {}).get("event_time")
        if event_time:
            try:
                if isinstance(event_time, str):
                    et = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
                else:
                    et = event_time
                if et.tzinfo is None:
                    et = et.replace(tzinfo=timezone.utc)
                # Future events are suspicious unless they're scheduled
                if et > now + timedelta(days=1):
                    if subtype not in ("appointment", "meeting", "publication"):
                        validation_issues.append(f"event_time in future: {et}")
                        validation_status = "impossible"
                # Events before 1900 are almost certainly wrong
                if et.year < 1900:
                    validation_issues.append(f"event_time pre-1900: {et}")
                    validation_status = "impossible"
                completeness_score += 0.1
            except (ValueError, TypeError, AttributeError):
                validation_issues.append("event_time unparseable")

        # ── Participant validation ───────────────────────────────
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM knowledge_edges ke
                JOIN knowledge_nodes kn ON kn.node_id = ke.source_node_id
                WHERE ke.target_node_id = %s
                  AND ke.relationship IN ('mentions', 'participated_in', 'derived_from')
                  AND kn.node_type IN ('entity', 'place')
            """, (node_id,))
            participant_count = cur.fetchone()[0]
            if participant_count > 0:
                completeness_score += 0.15

            # ── Source corroboration ─────────────────────────────
            cur.execute("""
                SELECT COUNT(DISTINCT kn.node_id) as doc_count,
                       COUNT(DISTINCT kn.envelope->'source'->>'domain') as domain_count,
                       COUNT(*) FILTER (WHERE kn.envelope->'validation'->>'is_satire' = 'true') as satire,
                       COUNT(*) FILTER (WHERE kn.envelope->'validation'->>'is_state_media' = 'true') as state_media
                FROM knowledge_edges ke
                JOIN knowledge_nodes kn ON kn.node_id = ke.source_node_id
                WHERE ke.target_node_id = %s
                  AND ke.relationship = 'mentions'
                  AND kn.node_type = 'document'
            """, (node_id,))
            row = cur.fetchone()
            doc_count = row[0] or 0
            domain_count = row[1] or 0
            satire_count = row[2] or 0
            state_media_count = row[3] or 0

            # Independent source count (excluding satire and dedup state media)
            independent_count = doc_count - satire_count
            if state_media_count > 1:
                independent_count -= (state_media_count - 1)
            independent_count = max(independent_count, 0)

            if independent_count >= 3:
                completeness_score += 0.25
            elif independent_count >= 2:
                completeness_score += 0.15
            elif independent_count >= 1:
                completeness_score += 0.05

            # ── Duplicate detection ──────────────────────────────
            cur.execute("""
                SELECT node_id, title, source_count
                FROM knowledge_nodes
                WHERE tenant_id = %s
                  AND node_type = 'event'
                  AND subtype = %s
                  AND node_id != %s
                  AND base60_address IS NOT NULL
                  AND similarity(LOWER(title), LOWER(%s)) > 0.75
                  AND ABS(EXTRACT(EPOCH FROM (created_at - %s::timestamptz))) < 86400 * 3
                ORDER BY source_count DESC, created_at ASC
                LIMIT 1
            """, (tenant_id, subtype, node_id, title, created_at))
            dup = cur.fetchone()

            if dup:
                # Merge into the older / higher-source-count event
                canonical_id = dup[0]
                log.info("Merging duplicate event %s → %s (similar title within 3 days)",
                         node_id, canonical_id)
                cur.execute("UPDATE knowledge_edges SET target_node_id = %s WHERE target_node_id = %s",
                            (canonical_id, node_id))
                cur.execute("UPDATE knowledge_edges SET source_node_id = %s WHERE source_node_id = %s",
                            (canonical_id, node_id))
                cur.execute("""
                    UPDATE knowledge_nodes
                    SET source_count = source_count + 1, updated_at = NOW()
                    WHERE node_id = %s
                """, (canonical_id,))
                cur.execute("DELETE FROM knowledge_nodes WHERE node_id = %s", (node_id,))
                conn.commit()
                _STATUS["duplicates_merged"] += 1
                return

        # ── Compute final tier ──────────────────────────────────
        if validation_status == "impossible":
            completeness_tier = "stub"
            _STATUS["impossible_flagged"] += 1
        elif independent_count >= 2:
            completeness_tier = "solid"
        elif completeness_score >= 0.7:
            completeness_tier = "solid"
        elif completeness_score >= 0.4:
            completeness_tier = "partial"
        else:
            completeness_tier = "stub"

        # ── Store result ────────────────────────────────────────
        try:
            with conn.cursor() as cur:
                envelope["validation"] = {
                    "status": validation_status,
                    "source": "event_validator",
                    "issues": validation_issues,
                    "validated_at": now.isoformat(),
                }
                envelope["evidence"] = envelope.get("evidence", {})
                envelope["evidence"].update({
                    "source_count": doc_count,
                    "independent_source_count": independent_count,
                    "domain_count": domain_count,
                    "participant_count": participant_count,
                    "validated_at": now.isoformat(),
                })

                # completeness_score/_tier deliberately not written — see
                # core/gdu/completeness.py. The inline weights here (0.4 base,
                # +0.1/+0.15/+0.25/+0.05) were a second, ungoverned slot spec
                # living in Python, with its own tier thresholds that disagreed
                # with the pack ladder — which is how event 0.90 came to read
                # 'bronze' while event 0.83 read 'solid'.
                cur.execute("""
                    UPDATE knowledge_nodes
                    SET envelope = %s,
                        enrichment_status = 'complete',
                        updated_at = NOW()
                    WHERE node_id = %s AND tenant_id = %s
                """, (
                    json.dumps(envelope),
                    node_id, tenant_id,
                ))

            conn.commit()

            _STATUS["validated"] += 1
            if validation_status == "impossible":
                log.warning("IMPOSSIBLE event flagged: %s — %s", node_id, validation_issues)

        except Exception as e:
            log.warning("Failed to store event validation for %s: %s", node_id, e)
            _STATUS["errors"] += 1
