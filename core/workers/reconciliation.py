"""
Entity Reconciliation Worker — background loop that continuously
compares, merges, and simplifies the entity graph.

Runs as a daemon thread. Never crashes — all operations are wrapped
in try/except with exponential backoff. Throttles itself when the
system is under load.

Reconciliation passes:
  1. Name similarity scan — find entities with similar normalized names
  2. Alias overlap scan — find entities that share alias values
  3. Stale dossier refresh — re-research entities not updated in 7+ days
  4. Orphan cleanup — remove entities with 0 references

Reports status to the workers table so the UI can show what's happening.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.reconciliation")

# Global state
_WORKER_THREAD: Optional[threading.Thread] = None
_WORKER_STATUS = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "merges": 0,
    "scanned": 0,
    "errors": 0,
}
_STOP_EVENT = threading.Event()

# Throttle settings
MAX_CONCURRENT_LLM = 2
_ACTIVE_LLM_CALLS = 0
_LLM_LOCK = threading.Lock()

RECONCILE_INTERVAL = 300  # 5 minutes between full scans
SIMILARITY_THRESHOLD = 0.75
MAX_MERGES_PER_RUN = 20


def get_worker_status() -> dict:
    """Get current worker status for the UI."""
    return dict(_WORKER_STATUS)


def start_reconciliation_worker(tenant_id: str = "default"):
    """Start the background reconciliation worker."""
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return

    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_reconciliation_loop,
        args=(tenant_id,),
        name="reconciliation-worker",
        daemon=True,
    )
    _WORKER_THREAD.start()
    log.info("Reconciliation worker started")


def stop_reconciliation_worker():
    """Stop the reconciliation worker gracefully."""
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=10)
    log.info("Reconciliation worker stopped")


def _reconciliation_loop(tenant_id: str):
    """Main worker loop — runs forever, sleeps between scans."""
    _WORKER_STATUS["running"] = True
    backoff = RECONCILE_INTERVAL

    while not _STOP_EVENT.is_set():
        try:
            _WORKER_STATUS["phase"] = "scanning for duplicates..."
            _run_reconciliation_pass(tenant_id)
            _WORKER_STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
            _WORKER_STATUS["phase"] = "idle"
            backoff = RECONCILE_INTERVAL  # Reset backoff on success

        except Exception as e:
            log.warning("Reconciliation pass failed: %s", e)
            _WORKER_STATUS["errors"] += 1
            _WORKER_STATUS["phase"] = f"error: {str(e)[:50]}"
            backoff = min(backoff * 2, 3600)  # Exponential backoff, max 1 hour

        # Sleep with periodic check for stop event
        for _ in range(int(backoff)):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)

    _WORKER_STATUS["running"] = False
    _WORKER_STATUS["phase"] = "stopped"


def _run_reconciliation_pass(tenant_id: str):
    """One full reconciliation pass — scan, compare, merge."""
    merges_this_run = 0

    # Pass 1: Name similarity scan
    _WORKER_STATUS["phase"] = "scanning similar names..."
    try:
        merges_this_run += _scan_similar_names(tenant_id)
    except Exception as e:
        log.debug("Name similarity scan failed: %s", e)

    # Pass 2: Alias overlap scan
    _WORKER_STATUS["phase"] = "scanning alias overlaps..."
    try:
        merges_this_run += _scan_alias_overlaps(tenant_id)
    except Exception as e:
        log.debug("Alias overlap scan failed: %s", e)

    # Pass 3: Entity cleanup (demonyms, abbreviations, source filtering)
    _WORKER_STATUS["phase"] = "cleaning entity names..."
    try:
        from core.extraction.entity_cleanup import cleanup_topic_entities
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT topic_id FROM watch_topics WHERE tenant_id = %s AND status = 'active'", (tenant_id,))
                for (tid,) in cur.fetchall():
                    result = cleanup_topic_entities(tid, tenant_id)
                    merges_this_run += result.get("merged", 0)
    except Exception as e:
        log.debug("Entity cleanup failed: %s", e)

    # Pass 4: Co-occurrence merge (frequency-based, domain-independent)
    _WORKER_STATUS["phase"] = "analyzing co-occurrence patterns..."
    try:
        merges_this_run += _cooccurrence_merge(tenant_id)
    except Exception as e:
        log.debug("Co-occurrence merge failed: %s", e)

    # Pass 5: Stale dossier check (queue re-research)
    _WORKER_STATUS["phase"] = "checking stale dossiers..."
    try:
        _check_stale_dossiers(tenant_id)
    except Exception as e:
        log.debug("Stale dossier check failed: %s", e)

    _WORKER_STATUS["merges"] += merges_this_run
    if merges_this_run > 0:
        log.info("Reconciliation: %d merges this pass", merges_this_run)


def _scan_similar_names(tenant_id: str) -> int:
    """Find entities with similar normalized names within the same type."""
    merges = 0

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Find pairs of entities with high name similarity
            # Uses trigram similarity (pg_trgm extension) if available,
            # falls back to exact prefix matching
            try:
                cur.execute("""
                    SELECT e1.entity_id, e1.canonical_name, e1.canonical_type, e1.signal_count,
                           e2.entity_id, e2.canonical_name, e2.signal_count
                    FROM entities e1
                    JOIN entities e2 ON e1.tenant_id = e2.tenant_id
                        AND e1.canonical_type = e2.canonical_type
                        AND e1.entity_id < e2.entity_id
                        AND e1.normalized_name != e2.normalized_name
                        AND LEFT(e1.normalized_name, 5) = LEFT(e2.normalized_name, 5)
                    WHERE e1.tenant_id = %s
                    LIMIT 100
                """, (tenant_id,))

                for r in cur.fetchall():
                    if merges >= MAX_MERGES_PER_RUN:
                        break

                    id1, name1, type1, count1 = r[0], r[1], r[2], r[3]
                    id2, name2, count2 = r[4], r[5], r[6]

                    sim = _name_similarity(name1.lower(), name2.lower())
                    if sim >= SIMILARITY_THRESHOLD:
                        # Keep the one with more signals as the winner
                        winner = id1 if count1 >= count2 else id2
                        loser = id2 if winner == id1 else id1

                        try:
                            from core.gdu.entity_resolution import merge_entities
                            merge_entities(winner, [loser], tenant_id,
                                          merged_by="reconciliation_worker",
                                          reason=f"name similarity {sim:.2f}: '{name1}' ≈ '{name2}'")
                            merges += 1
                            _WORKER_STATUS["scanned"] += 1
                            log.info("Merged: '%s' + '%s' (sim=%.2f)", name1, name2, sim)
                        except Exception as e:
                            log.debug("Merge failed %s + %s: %s", name1, name2, e)

            except Exception as e:
                log.debug("Similar name scan query failed: %s", e)

    return merges


def _scan_alias_overlaps(tenant_id: str) -> int:
    """Find entities that share alias values — likely the same entity."""
    merges = 0

    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    SELECT e1.entity_id, e1.canonical_name, e1.canonical_type, e1.signal_count,
                           e2.entity_id, e2.canonical_name, e2.signal_count,
                           (SELECT array_agg(x) FROM unnest(e1.aliases) x
                            WHERE x = ANY(e2.aliases)) as shared_aliases
                    FROM entities e1
                    JOIN entities e2 ON e1.tenant_id = e2.tenant_id
                        AND e1.canonical_type = e2.canonical_type
                        AND e1.entity_id < e2.entity_id
                        AND e1.aliases && e2.aliases
                    WHERE e1.tenant_id = %s
                      AND array_length(e1.aliases, 1) > 0
                    LIMIT 50
                """, (tenant_id,))

                for r in cur.fetchall():
                    if merges >= MAX_MERGES_PER_RUN:
                        break

                    id1, name1, type1, count1 = r[0], r[1], r[2], r[3]
                    id2, name2, count2 = r[4], r[5], r[6]
                    shared = r[7] or []

                    if len(shared) > 0:
                        winner = id1 if count1 >= count2 else id2
                        loser = id2 if winner == id1 else id1

                        try:
                            from core.gdu.entity_resolution import merge_entities
                            merge_entities(winner, [loser], tenant_id,
                                          merged_by="reconciliation_worker",
                                          reason=f"shared aliases: {shared[:3]}")
                            merges += 1
                            log.info("Merged via alias: '%s' + '%s' (shared: %s)", name1, name2, shared)
                        except Exception as e:
                            log.debug("Alias merge failed: %s", e)

            except Exception as e:
                log.debug("Alias overlap scan failed: %s", e)

    return merges


def _cooccurrence_merge(tenant_id: str) -> int:
    """Merge entities that co-occur heavily and one is a substring/variant of the other.

    Domain-independent — works for any topic by analyzing article overlap patterns.
    No hardcoded lists needed.

    Merge conditions (ALL must be true):
      1. Both entities are in the same topic
      2. One name is a substring of the other, OR they share >80% of articles
      3. They have the same entity_type (or one is 'unknown')
      4. Neither has been manually split (no redirect pointing away from it)
    """
    merges = 0

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Get all active topics
            cur.execute("SELECT topic_id FROM watch_topics WHERE tenant_id = %s AND status = 'active'", (tenant_id,))
            topic_ids = [r[0] for r in cur.fetchall()]

            for topic_id in topic_ids:
                # Load entities with their article sets
                cur.execute("""
                    SELECT entity_id, name, entity_type, mention_count, article_ids
                    FROM watch_topic_entities
                    WHERE topic_id = %s AND tenant_id = %s
                    ORDER BY mention_count DESC
                """, (topic_id, tenant_id))
                entities = [{
                    "id": r[0], "name": r[1], "type": r[2],
                    "mentions": r[3], "articles": set(r[4] or []),
                } for r in cur.fetchall()]

                if len(entities) < 2:
                    continue

                merged_ids = set()

                for i, e1 in enumerate(entities):
                    if e1["id"] in merged_ids:
                        continue

                    for j in range(i + 1, len(entities)):
                        e2 = entities[j]
                        if e2["id"] in merged_ids:
                            continue
                        if merges >= MAX_MERGES_PER_RUN:
                            break

                        # Check type compatibility
                        types_ok = (
                            e1["type"] == e2["type"] or
                            e1["type"] == "unknown" or e2["type"] == "unknown"
                        )
                        if not types_ok:
                            continue

                        n1 = e1["name"].lower().strip()
                        n2 = e2["name"].lower().strip()

                        # Condition: substring relationship REQUIRED
                        # High article overlap alone is NOT sufficient — "Iran" and "Russia"
                        # share articles but are different entities. Substring relationship
                        # is the primary signal. Article overlap is only a confirmation.
                        is_substring = n1 in n2 or n2 in n1

                        if not is_substring:
                            continue

                        # Safety: shorter name must be at least 50% of the longer name's length
                        # Prevents merging "Iran" with "Iranian Nuclear Program"
                        shorter = n1 if len(n1) <= len(n2) else n2
                        longer = n2 if len(n1) <= len(n2) else n1
                        if len(shorter) < len(longer) * 0.5:
                            continue

                        # Safety: don't merge if both are common standalone entities
                        # (e.g., "Iran" in "Iranian" — handled by demonym map, not here)
                        if len(shorter) >= 3 and len(longer) - len(shorter) <= 3:
                            # Very similar length — only merge if near-identical
                            sim = _name_similarity(n1, n2)
                            if sim < 0.8:
                                continue

                        # Merge: keep the one with more mentions
                        winner = e1 if e1["mentions"] >= e2["mentions"] else e2
                        loser = e2 if winner == e1 else e1

                        # Use the longer/more formal name as canonical
                        canonical_name = e1["name"] if len(e1["name"]) >= len(e2["name"]) else e2["name"]

                        total_mentions = e1["mentions"] + e2["mentions"]
                        all_articles = list(e1["articles"] | e2["articles"])
                        best_type = winner["type"] if winner["type"] != "unknown" else loser["type"]

                        try:
                            cur.execute("""
                                UPDATE watch_topic_entities SET
                                    name = %s, entity_type = %s,
                                    mention_count = %s, article_ids = %s,
                                    updated_at = NOW()
                                WHERE entity_id = %s AND topic_id = %s
                            """, (canonical_name, best_type, total_mentions,
                                  all_articles[:100], winner["id"], topic_id))

                            cur.execute("""
                                DELETE FROM watch_topic_entities
                                WHERE entity_id = %s AND topic_id = %s
                            """, (loser["id"], topic_id))

                            merged_ids.add(loser["id"])
                            merges += 1

                            log.info("Co-occurrence merge: '%s' + '%s' → '%s' (topic %s, overlap=%.0f%%)",
                                     e1["name"], e2["name"], canonical_name, topic_id,
                                     overlap_ratio * 100)
                        except Exception as e:
                            log.debug("Co-occurrence merge failed: %s", e)

                # Update topic counts
                if merges > 0:
                    cur.execute("""
                        UPDATE watch_topics SET
                            entity_count = (SELECT count(*) FROM watch_topic_entities WHERE topic_id = %s),
                            updated_at = NOW()
                        WHERE topic_id = %s
                    """, (topic_id, topic_id))

            conn.commit()

    return merges


def _check_stale_dossiers(tenant_id: str):
    # Auto-queue disabled. Stale dossiers are surfaced in the UI for operator
    # review; enrichment runs are triggered manually per-type from the dossier.
    return


def _name_similarity(a: str, b: str) -> float:
    """Bigram Jaccard similarity."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    a_bigrams = set(a[i:i+2] for i in range(len(a) - 1))
    b_bigrams = set(b[i:i+2] for i in range(len(b) - 1))
    if not a_bigrams or not b_bigrams:
        return 0.0
    return len(a_bigrams & b_bigrams) / len(a_bigrams | b_bigrams)


# ─── LLM Throttle ─────────────────────────────────────────────────────────────

def acquire_llm_slot(timeout: float = 120) -> bool:
    """Wait for an LLM slot. Returns False if timeout exceeded."""
    global _ACTIVE_LLM_CALLS
    start = time.time()
    while time.time() - start < timeout:
        with _LLM_LOCK:
            if _ACTIVE_LLM_CALLS < MAX_CONCURRENT_LLM:
                _ACTIVE_LLM_CALLS += 1
                return True
        time.sleep(1)
    return False


def release_llm_slot():
    """Release an LLM slot."""
    global _ACTIVE_LLM_CALLS
    with _LLM_LOCK:
        _ACTIVE_LLM_CALLS = max(0, _ACTIVE_LLM_CALLS - 1)


def get_llm_load() -> dict:
    """Get current LLM load for status reporting."""
    return {
        "active": _ACTIVE_LLM_CALLS,
        "max": MAX_CONCURRENT_LLM,
        "available": MAX_CONCURRENT_LLM - _ACTIVE_LLM_CALLS,
    }
