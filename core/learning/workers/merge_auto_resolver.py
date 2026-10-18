"""
Aged auto-resolver for event_merge_review.

The event_clusterer queues plausible-but-not-strong matches to the review
queue. This worker applies them after they've aged past a score-adjusted
threshold — higher-confidence matches age out faster.

If either event has been deleted (e.g. merged into a different cluster
while this proposal was pending), the review row is marked `stale`.

Runs at REVIEW priority (tier 4), so foreground activity can pause it
entirely. The whole design assumes a human is NOT in the loop — if you
want to block a match, reject it via the API before it ages.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("enki.learning.merge_auto_resolver")

# Score → required age (seconds) before auto-apply
AGING_TIERS = [
    (0.75, 60 * 60),        # ≥0.75 → age 1h
    (0.65, 6 * 60 * 60),    # ≥0.65 → age 6h
    (0.55, 24 * 60 * 60),   # ≥0.55 → age 24h
]
BATCH_LIMIT = 20


def run(conn) -> dict:
    stats = {"examined": 0, "applied": 0, "stale": 0, "not_ready": 0, "errors": 0}

    rows = _fetch_pending(conn, limit=BATCH_LIMIT)
    stats["examined"] = len(rows)

    for r in rows:
        try:
            required_age = _required_age_for(r["match_score"])
            if r["age_seconds"] < required_age:
                stats["not_ready"] += 1
                continue

            survivor = _fetch_event_by_node_id(conn, r["tenant_id"], r["survivor_node_id"])
            loser = _fetch_event_by_node_id(conn, r["tenant_id"], r["loser_node_id"])
            if survivor is None or loser is None:
                _mark_stale(conn, r["review_id"],
                            reason=f"survivor={survivor is not None} loser={loser is not None}")
                stats["stale"] += 1
                continue

            # Re-use the same merge path as the live clusterer
            from core.learning.workers.event_clusterer import _merge_events
            conflicts = _merge_events(conn, survivor=survivor, loser=loser)
            _mark_applied(conn, r["review_id"], conflicts_written=conflicts)
            stats["applied"] += 1
        except Exception as e:
            log.exception("auto-resolver: review %s failed: %s", r.get("review_id"), e)
            try: conn.rollback()
            except Exception: pass
            stats["errors"] += 1

    return stats


def register(governor) -> None:
    from core.learning.governor import Priority
    governor.register(
        name="event_merge_auto_resolver",
        priority=Priority.REVIEW,
        interval_seconds=300.0,
        fn=run,
    )


# ── internals ──────────────────────────────────────────────────────────

def _required_age_for(score: float) -> float:
    for threshold, age in AGING_TIERS:
        if score >= threshold:
            return age
    return float("inf")         # below 0.55 never auto-applies


def _fetch_pending(conn, *, limit: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT review_id, tenant_id, survivor_node_id, loser_node_id,
                   match_score, match_criteria,
                   EXTRACT(EPOCH FROM (NOW() - proposed_at))::bigint AS age_seconds
            FROM event_merge_review
            WHERE status = 'pending'
            ORDER BY match_score DESC, proposed_at ASC
            LIMIT %s
            """,
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _fetch_event_by_node_id(conn, tenant_id: str, node_id: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT event_id, tenant_id, node_id, event_type, title,
                   event_time, latitude, longitude, place_text, place_node_id,
                   actor_entity_ids, target_entity_ids, source_signal_rids,
                   confidence, created_at
            FROM events
            WHERE tenant_id = %s AND node_id = %s
            LIMIT 1
            """,
            (tenant_id, node_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


def _mark_applied(conn, review_id: int, *, conflicts_written: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE event_merge_review
            SET status = 'auto_applied',
                resolved_at = NOW(),
                resolved_by = 'auto_resolver_v1',
                resolution_note = %s
            WHERE review_id = %s
            """,
            (f"{conflicts_written} conflict(s) recorded", review_id),
        )
    conn.commit()


def _mark_stale(conn, review_id: int, *, reason: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE event_merge_review
            SET status = 'stale',
                resolved_at = NOW(),
                resolved_by = 'auto_resolver_v1',
                resolution_note = %s
            WHERE review_id = %s
            """,
            (reason[:500], review_id),
        )
    conn.commit()
