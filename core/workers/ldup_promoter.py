"""
LDU-P Auto-Promotion Worker — promotes provisional entries when vocabulary expands.

Runs every 5 minutes. Scans both signal LDU-P (ldu_p_entries) and entity LDU-P
(entity_ldu_p) for pending entries whose proposed types now exist in the
vocabulary packs.

For signal LDU-P: promotes to canonical signal via the ingestion pipeline.
For entity LDU-P: promotes to entity via the GDU gateway.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

from storage.postgres.db import get_connection

_log = logging.getLogger("enki.workers.ldup_promoter")

_stop_event = threading.Event()
_thread: Optional[threading.Thread] = None

POLL_INTERVAL = 300  # 5 minutes


def _worker_loop(tenant_id: str):
    """Main loop: scan LDU-P tables for auto-promotable entries."""
    # Pass dsn=None so get_connection() uses the live pool / env DSN.
    # Previous version read ENKI_PG_DSN (wrong name; env is ENKI_DB_DSN)
    # and fell back to "" — psycopg2 treats empty-string DSN as a request
    # for the local socket, which doesn't exist inside the api container,
    # so every tick raised OperationalError and was silently swallowed.
    dsn = None

    while not _stop_event.is_set():
        try:
            _promote_signal_ldup(tenant_id, dsn)
            _promote_entity_ldup(tenant_id, dsn)
        except Exception:
            _log.exception("LDU-P promoter tick failed")

        _stop_event.wait(POLL_INTERVAL)


def _promote_signal_ldup(tenant_id: str, dsn: str):
    """Check signal LDU-P entries against current signalvocab."""
    try:
        from core.gdu.signal_vocabulary import load_signal_types_via_gdu
        types, _, _ = load_signal_types_via_gdu()
        valid_types = set(types.keys()) if isinstance(types, dict) else set(types)
    except Exception:
        return  # Can't load vocab — skip

    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT ldu_p_id, proposed_signal_type
                    FROM ldu_p_entries
                    WHERE tenant_id = %s AND resolution = 'pending_review'
                    LIMIT 50
                """, (tenant_id,))
                rows = cur.fetchall()

                promoted = 0
                for ldu_p_id, proposed_type in rows:
                    if proposed_type and proposed_type in valid_types:
                        try:
                            from storage.ingest.signal_ingestion import promote_ldu_p
                            result = promote_ldu_p(ldu_p_id, tenant_id, proposed_type, "ldup_promoter_worker")
                            if result and result.success:
                                promoted += 1
                        except Exception:
                            _log.debug("Signal LDU-P auto-promote failed for %s", ldu_p_id)

                if promoted > 0:
                    _log.info("Auto-promoted %d signal LDU-P entries", promoted)
    except Exception:
        _log.debug("Signal LDU-P scan skipped (table may not exist)")


def _promote_entity_ldup(tenant_id: str, dsn: str):
    """Drain entity_ldu_p — three behaviors keyed on rejection_reason:

      1. No reason / accept-class suspected_type → existing admit_entity path
         (vocab expanded enough that the original LLM type now fits entities)

      2. rejection_reason='route:<cat>.<sub>' → forward to the correct
         substrate via admit_node (places, topics, events, chronicle,
         document) instead of letting the row sit forever.

      3. rejection_reason='ldu_drop:*' or 'normalize_rejected:*' →
         pre-classified garbage. Mark status='dropped' so the scan doesn't
         keep paging through ~2,000 known-bad rows every tick.

      4. rejection_reason='ldu_unknown:*' → leave pending; type may
         become admittable in a future vocab bump.
    """
    try:
        from core.ldu.subtype_validator import validate_for_ldu
    except Exception:
        return

    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT ldu_p_id, raw_name, suspected_type, rejection_reason
                    FROM entity_ldu_p
                    WHERE tenant_id = %s AND status = 'pending'
                    LIMIT 200
                """, (tenant_id,))
                rows = cur.fetchall()

                promoted = 0
                routed = 0
                dropped = 0
                for ldu_p_id, raw_name, suspected_type, rej in rows:
                    rej = rej or ""

                    # Pre-classified garbage from the original admission —
                    # honor it without re-validating.
                    if rej.startswith(("ldu_drop:", "normalize_rejected:")):
                        cur.execute("""
                            UPDATE entity_ldu_p
                            SET status = 'dropped', reviewed_by = 'ldup_promoter_worker',
                                reviewed_at = NOW()
                            WHERE ldu_p_id = %s
                        """, (ldu_p_id,))
                        dropped += 1
                        continue

                    # Re-consult entityvocab on every pending row. Earlier
                    # rejection_reasons may have predated route additions or
                    # set ldu_unknown for types that now route.
                    v = validate_for_ldu(suspected_type)

                    # Pre-stamped route override (still honored if present).
                    target_cat = target_sub = None
                    if rej.startswith("route:"):
                        target = rej[len("route:"):]
                        target_cat, _, target_sub = target.partition(".")
                        if not target_cat or not target_sub:
                            target_cat = target_sub = None

                    if not target_cat and v.action == "route":
                        target_cat, target_sub = v.route_to

                    if target_cat and target_sub:
                        try:
                            from core.gdu.enforce import admit_node
                            result = admit_node(
                                tenant_id=tenant_id,
                                category=target_cat,
                                subtype=target_sub,
                                title=raw_name,
                                description="",
                                signal_rid=0,
                            )
                            if result.action in ("admit", "collapse"):
                                cur.execute("""
                                    UPDATE entity_ldu_p
                                    SET status = 'routed', reviewed_by = 'ldup_promoter_worker',
                                        reviewed_at = NOW()
                                    WHERE ldu_p_id = %s
                                """, (ldu_p_id,))
                                routed += 1
                        except Exception:
                            _log.debug("LDU-P route to %s.%s failed for %s",
                                       target_cat, target_sub, ldu_p_id, exc_info=True)
                        continue

                    if v.action == "accept":
                        try:
                            from core.gdu.enforce import admit_entity
                            result = admit_entity(
                                tenant_id=tenant_id,
                                name=raw_name,
                                entity_type=v.canonical_subtype or suspected_type,
                                signal_rid=0,
                            )
                            if result.action in ("admit", "collapse"):
                                cur.execute("""
                                    UPDATE entity_ldu_p
                                    SET status = 'promoted', reviewed_by = 'ldup_promoter_worker',
                                        reviewed_at = NOW()
                                    WHERE ldu_p_id = %s
                                """, (ldu_p_id,))
                                promoted += 1
                        except Exception:
                            _log.warning("Entity LDU-P auto-promote failed for %s",
                                         ldu_p_id, exc_info=True)
                        continue

                    if v.action == "drop":
                        cur.execute("""
                            UPDATE entity_ldu_p
                            SET status = 'dropped', reviewed_by = 'ldup_promoter_worker',
                                reviewed_at = NOW(),
                                rejection_reason = COALESCE(rejection_reason || ' | ', '')
                                                 || 'ldup_promoter_drop: ' || %s
                            WHERE ldu_p_id = %s
                        """, (v.reason[:200], ldu_p_id))
                        dropped += 1
                        continue

                    # v.action == "unknown" — leave pending. May become
                    # routable/droppable in a future entityvocab update.

                if promoted or routed or dropped:
                    conn.commit()
                    _log.info(
                        "LDU-P drain: promoted=%d routed=%d dropped=%d",
                        promoted, routed, dropped,
                    )
    except Exception:
        _log.debug("Entity LDU-P scan skipped (table may not exist)")


# ─── Public API ──────────────────────────────────────────────────────────────

def start_ldup_promoter(tenant_id: str = "default"):
    """Start the LDU-P auto-promotion worker."""
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(
        target=_worker_loop, args=(tenant_id,),
        name="ldup-promoter", daemon=True,
    )
    _thread.start()
    _log.info("LDU-P promoter worker started (interval=%ds)", POLL_INTERVAL)


def stop_ldup_promoter():
    """Stop the LDU-P auto-promotion worker."""
    _stop_event.set()
    _log.info("LDU-P promoter worker stopped")


def get_ldup_promoter_status() -> dict:
    """Return current status."""
    return {
        "running": _thread is not None and _thread.is_alive(),
        "interval_s": POLL_INTERVAL,
    }
