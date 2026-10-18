"""
Candidate Promoter — promotes entity candidates to real entities once
they cross a threshold of mentions across multiple sources.

Runs as a periodic background job (every 5 minutes). When a candidate has
≥5 mentions from ≥2 distinct sources, it gets promoted:
1. Create/link canonical entity via resolve_entity()
2. Queue entity for background research (Wikipedia, Wikidata)
3. Mark candidate as 'promoted' with the new entity_id

This is how new names/concepts that weren't in our known lists get picked up
and added to the canonical entities table automatically.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("enki.workers.candidate_promoter")

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS = {
    "running": False,
    "last_run": None,
    "promoted_count": 0,
    "rejected_count": 0,
    "pending_count": 0,
}

MIN_MENTIONS = 5
MIN_SOURCES = 2
CHECK_INTERVAL = 300  # 5 minutes


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_loop, args=(tenant_id,),
        name="candidate-promoter", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    return {"status": "started"}


def stop_worker():
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=5)
    _STATUS["running"] = False
    return {"status": "stopped"}


def _loop(tenant_id: str):
    _STATUS["running"] = True
    while not _STOP_EVENT.is_set():
        try:
            promote_pending(tenant_id)
        except Exception as e:
            log.warning("Candidate promoter error: %s", e)

        for _ in range(CHECK_INTERVAL):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)
    _STATUS["running"] = False


def promote_pending(tenant_id: str = "default", dsn: str | None = None) -> dict:
    """Find candidates that cross the threshold and promote them.

    Returns summary dict with counts.
    """
    from storage.postgres.db import get_connection
    from core.gdu.entity_resolution import resolve_entity

    promoted = 0
    rejected = 0
    pending_total = 0

    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                # Find candidates ready for promotion
                cur.execute("""
                    SELECT candidate_id, name, normalized_name, suspected_type,
                           mention_count, source_count, sample_headlines, sample_sources
                    FROM entity_candidates
                    WHERE tenant_id = %s AND status = 'pending'
                      AND mention_count >= %s AND source_count >= %s
                    ORDER BY mention_count DESC
                    LIMIT 50
                """, (tenant_id, MIN_MENTIONS, MIN_SOURCES))

                candidates = cur.fetchall()

                for c in candidates:
                    cand_id, name, norm_name, suspected_type, mentions, sources, headlines, src_names = c

                    # Skip generic/short names
                    if len(norm_name) < 4:
                        cur.execute("UPDATE entity_candidates SET status='rejected' WHERE candidate_id=%s", (cand_id,))
                        rejected += 1
                        continue

                    # Map suspected type to canonical entity type
                    entity_type = suspected_type or "unknown"
                    if entity_type == "unknown":
                        # Default guess based on patterns
                        if name.isupper() and len(name) <= 6:
                            entity_type = "organization"
                        elif any(w[0].isupper() for w in name.split()):
                            entity_type = "person"  # Best guess
                        else:
                            cur.execute("UPDATE entity_candidates SET status='rejected' WHERE candidate_id=%s", (cand_id,))
                            rejected += 1
                            continue

                    # Use resolve_entity to either match or create
                    try:
                        # Build context from sample headlines
                        context = " ".join(headlines[:3]) if headlines else ""

                        result = resolve_entity(
                            tenant_id=tenant_id,
                            name=name,
                            entity_type=entity_type,
                            context=context,
                            source_pipeline="candidate_promoter",
                            dsn=dsn,
                        )

                        if result.entity_id:
                            cur.execute("""
                                UPDATE entity_candidates SET
                                    status = 'promoted',
                                    promoted_entity_id = %s,
                                    updated_at = NOW()
                                WHERE candidate_id = %s
                            """, (result.entity_id, cand_id))
                            promoted += 1
                            log.info("Promoted candidate '%s' (%s) → entity_id=%s (%s)",
                                     name, entity_type, result.entity_id, result.action)
                        else:
                            rejected += 1
                            cur.execute("UPDATE entity_candidates SET status='rejected' WHERE candidate_id=%s", (cand_id,))
                    except Exception as e:
                        log.debug("Promotion failed for '%s': %s", name, e)
                        rejected += 1

                # Count remaining pending
                cur.execute("""
                    SELECT count(*) FROM entity_candidates
                    WHERE tenant_id = %s AND status = 'pending'
                """, (tenant_id,))
                pending_total = cur.fetchone()[0]

            conn.commit()

    except Exception as e:
        log.error("promote_pending failed: %s", e)

    _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
    _STATUS["promoted_count"] += promoted
    _STATUS["rejected_count"] += rejected
    _STATUS["pending_count"] = pending_total

    result = {
        "promoted": promoted,
        "rejected": rejected,
        "pending": pending_total,
    }
    if promoted > 0:
        log.info("Candidate promoter run: %s", result)
    return result
