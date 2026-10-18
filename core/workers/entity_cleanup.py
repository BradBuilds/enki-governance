"""Continuous entity cleanup — validate, or park junk in the LDU.

Behind the paced investigation/collapse discovery, this trickle-scans low-evidence
entities: clear junk (OCR-garbage names, un-split manifest fragments) is DEMOTED to
the LDU (reversible — reclassified flag + entity_ldu_p row) where the discrepancy
review flow handles it; everything else is stamped validated so it isn't re-checked.

Paced + yields the GPU under pressure, exactly like the investigation worker — it
never bursts, so the graph self-cleans gently in the background. Conservative by
design: it only parks UNAMBIGUOUS junk; anything debatable is left for the operator /
AI review (fence: don't auto-delete real entities).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.entity_cleanup")

BATCH_SIZE = 25
PACE_SECONDS = max(15, int(os.getenv("ENKI_CLEANUP_PACE_SECONDS", "45") or 45))
LOAD_PAUSE = float(os.getenv("ENKI_CLEANUP_LOAD_PAUSE", "10") or 10)
SIGNAL_MAX = 3   # only scrutinise thin entities; well-attested ones are left alone

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP = threading.Event()
_STATUS = {"running": False, "phase": "idle", "checked_total": 0,
           "parked_total": 0, "validated_total": 0, "last_run": None, "errors": 0}


def get_status() -> dict:
    return dict(_STATUS)


def _should_pause() -> bool:
    try:
        from core.workers.investigation import _should_pause as _inv_pause
        return _inv_pause()
    except Exception:
        try:
            return os.getloadavg()[1] > LOAD_PAUSE
        except Exception:
            return False


def run_tick(tenant_id: str = "default") -> dict:
    """Check one batch of thin, unchecked entities. Park junk → LDU, mark the rest
    validated. Returns per-tick counts."""
    from core.gdu.entity_resolution import is_garbage_name
    from core.collapse.manifest_split import looks_like_manifest
    from core.gdu.alias_store import demote_fragment_to_ldup

    parked = validated = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT entity_id, canonical_name FROM entities
                 WHERE tenant_id = %s
                   AND canonical_type IN ('person','organization','company')
                   AND COALESCE(signal_count, 0) < %s
                   AND NOT (envelope ? 'reclassified_to')
                   AND NOT (envelope ? 'cleanup_ok')
                 ORDER BY created_at DESC
                 LIMIT %s
            """, (tenant_id, SIGNAL_MAX, BATCH_SIZE))
            rows = cur.fetchall()

    for eid, name in rows:
        try:
            garbage, reason = is_garbage_name(name or "")
            junk = garbage or looks_like_manifest(name or "")
            if junk:
                demote_fragment_to_ldup(
                    int(eid), f"cleanup: {reason or 'manifest/garbage name'}",
                    tenant_id=tenant_id)
                parked += 1
            else:
                # validated → stamp so we don't re-scan it every pass
                with get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE entities
                               SET envelope = COALESCE(envelope,'{}'::jsonb)
                                   || jsonb_build_object('cleanup_ok', true),
                                   updated_at = NOW()
                             WHERE entity_id = %s
                        """, (int(eid),))
                    conn.commit()
                validated += 1
        except Exception as e:
            log.debug("cleanup failed for %s: %s", eid, e)

    _STATUS["checked_total"] += len(rows)
    _STATUS["parked_total"] += parked
    _STATUS["validated_total"] += validated
    return {"checked": len(rows), "parked": parked, "validated": validated}


def _loop(tenant_id: str):
    _STATUS["running"] = True
    backoff = PACE_SECONDS
    while not _STOP.is_set():
        try:
            if _should_pause():
                _STATUS["phase"] = "paused — yielding GPU (pressure)"
                backoff = PACE_SECONDS
            else:
                r = run_tick(tenant_id)
                _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
                if r["checked"] == 0:
                    _STATUS["phase"] = "idle — all entities checked"
                    backoff = min(backoff + 30, 300)
                else:
                    _STATUS["phase"] = f"cleaned {r['parked']}p/{r['validated']}v (paced {PACE_SECONDS}s)"
                    backoff = PACE_SECONDS
        except Exception as e:
            log.warning("cleanup tick failed: %s", e)
            _STATUS["errors"] += 1
            backoff = min(backoff * 2, 300)
        for _ in range(int(backoff)):
            if _STOP.is_set():
                break
            time.sleep(1)
    _STATUS["running"] = False


def start_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP.clear()
    _WORKER_THREAD = threading.Thread(target=_loop, args=(tenant_id,),
                                      name="entity-cleanup", daemon=True)
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    return {"status": "started"}


def stop_worker():
    _STOP.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=10)
    _STATUS["running"] = False
    return {"status": "stopped"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json
    print(json.dumps(run_tick(), indent=2))
