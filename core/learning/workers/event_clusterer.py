"""
Event clusterer — collapses events that describe the same real-world
happening but got different deterministic node_ids (because the LLM
phrased the title differently when extracting from different articles).

Run path:
    every ~60s under RECENT priority
    → find events with clustered_at IS NULL
    → for each, search events table for candidate matches
    → score candidates (type / time / place / entity overlap)
    → strong + medium scores auto-merge (newer → older)
    → weak scores queue to event_merge_review (aged auto-resolver in 1.4)
    → no match: just mark clustered_at=NOW() and move on

Merge semantics (newer folds into older):
    - source_signal_rids / actor_entity_ids / target_entity_ids → union
    - source_count → cardinality of merged source_signal_rids
    - confidence → MAX
    - place_text → union
    - conflicts detected and recorded to node_conflicts (different titles,
      different event_times beyond sub-hour slop, different place_text)
    - edges pointing at the loser's node_id get repointed to the survivor
    - loser's knowledge_nodes and events rows are deleted
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Optional

log = logging.getLogger("enki.learning.event_clusterer")

# ── tunables ───────────────────────────────────────────────────────────
TIME_WINDOW_SECONDS = 6 * 3600          # ±6h for same-event candidates
PLACE_PROXIMITY_KM = 25.0                # lat/lon distance threshold
BATCH_LIMIT = 50                         # events examined per tick
STRONG_SCORE_THRESHOLD = 0.80            # auto-merge at/above
WEAK_SCORE_THRESHOLD = 0.55              # review queue at/above
TIME_CONFLICT_SECONDS = 3600             # >1h apart = recorded as conflict


def run(conn) -> dict:
    """Governor entry point. Returns stats dict for learning_log."""
    stats = {
        "examined": 0,
        "merged": 0,
        "reviewed": 0,
        "no_match": 0,
        "conflicts_written": 0,
        "errors": 0,
    }

    # Connection may come from a pool with leftover InFailedSqlTransaction
    # state from a prior worker. Roll back unconditionally before starting.
    try: conn.rollback()
    except Exception: pass

    candidates = _fetch_unclustered(conn, limit=BATCH_LIMIT)
    stats["examined"] = len(candidates)

    for ev in candidates:
        try:
            match = _find_best_match(conn, ev)
            if match is None:
                _mark_clustered(conn, ev["event_id"])
                stats["no_match"] += 1
                continue

            survivor, score, criteria = match
            if score >= STRONG_SCORE_THRESHOLD:
                conflicts = _merge_events(conn, survivor=survivor, loser=ev)
                stats["merged"] += 1
                stats["conflicts_written"] += conflicts
            elif score >= WEAK_SCORE_THRESHOLD:
                _queue_review(conn, survivor=survivor, loser=ev,
                              score=score, criteria=criteria)
                _mark_clustered(conn, ev["event_id"])
                stats["reviewed"] += 1
            else:
                _mark_clustered(conn, ev["event_id"])
                stats["no_match"] += 1
        except Exception as e:
            log.exception("clusterer: event %s failed: %s", ev.get("node_id"), e)
            try: conn.rollback()
            except Exception: pass
            stats["errors"] += 1

    return stats


def register(governor) -> None:
    """Wire this worker into the governor at RECENT priority, 60s interval."""
    from core.learning.governor import Priority
    governor.register(
        name="event_clusterer",
        priority=Priority.RECENT,
        interval_seconds=60.0,
        fn=run,
    )


# ── internals ──────────────────────────────────────────────────────────

def _fetch_unclustered(conn, *, limit: int) -> list[dict]:
    """Pick recently-created events that haven't been clustered yet.
    Newest first so we fold new events into older survivors."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT event_id, tenant_id, node_id, event_type, title,
                   event_time, latitude, longitude, place_text, place_node_id,
                   actor_entity_ids, target_entity_ids, source_signal_rids,
                   confidence, created_at
            FROM events
            WHERE clustered_at IS NULL
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _find_best_match(conn, ev: dict) -> Optional[tuple[dict, float, dict]]:
    """Scan events that share type + near time window + place/entity overlap.
    Returns the best (candidate, score, criteria) or None."""
    if ev.get("event_time") is None:
        return None    # can't time-window match without an event_time

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT event_id, tenant_id, node_id, event_type, title,
                   event_time, latitude, longitude, place_text, place_node_id,
                   actor_entity_ids, target_entity_ids, source_signal_rids,
                   confidence, created_at
            FROM events
            WHERE tenant_id = %(tenant)s
              AND event_type = %(etype)s
              AND event_id <> %(self_id)s
              AND event_time BETWEEN %(t_lo)s AND %(t_hi)s
              AND created_at < %(self_created)s   -- only older rows survive
            ORDER BY created_at ASC
            LIMIT 200
            """,
            {
                "tenant": ev["tenant_id"],
                "etype": ev["event_type"],
                "self_id": ev["event_id"],
                "t_lo": ev["event_time"] - timedelta(seconds=TIME_WINDOW_SECONDS),
                "t_hi": ev["event_time"] + timedelta(seconds=TIME_WINDOW_SECONDS),
                "self_created": ev["created_at"],
            },
        )
        cols = [d[0] for d in cur.description]
        candidates = [dict(zip(cols, row)) for row in cur.fetchall()]

    best: Optional[tuple[dict, float, dict]] = None
    for c in candidates:
        score, crit = _score_match(ev, c)
        if score <= 0:
            continue
        if best is None or score > best[1]:
            best = (c, score, crit)
    return best


def _score_match(new_ev: dict, candidate: dict) -> tuple[float, dict]:
    """Score a candidate 0.0-1.0. Criteria dict records what matched."""
    score = 0.0
    criteria: dict = {"type_match": True}    # both share event_type (pre-filtered)
    # Type match is baseline (pre-filtered); worth 0.20
    score += 0.20

    # Temporal proximity — 0.20 if within 1h, 0.10 if 1-6h
    t_delta = abs((new_ev["event_time"] - candidate["event_time"]).total_seconds())
    criteria["time_delta_seconds"] = int(t_delta)
    if t_delta <= 3600:
        score += 0.20
    elif t_delta <= TIME_WINDOW_SECONDS:
        score += 0.10

    # Spatial — node_id > coords > text overlap
    place_match = None
    if new_ev.get("place_node_id") and new_ev["place_node_id"] == candidate.get("place_node_id"):
        score += 0.25
        place_match = "node"
    elif (new_ev.get("latitude") is not None and new_ev.get("longitude") is not None
          and candidate.get("latitude") is not None and candidate.get("longitude") is not None):
        dist = _haversine_km(new_ev["latitude"], new_ev["longitude"],
                             candidate["latitude"], candidate["longitude"])
        if dist <= PLACE_PROXIMITY_KM:
            score += 0.25
            place_match = "coord"
        elif dist <= PLACE_PROXIMITY_KM * 3:
            score += 0.10
            place_match = "coord_loose"
    else:
        # Fallback: place_text overlap
        a_places = set(_norm(p) for p in (new_ev.get("place_text") or []))
        b_places = set(_norm(p) for p in (candidate.get("place_text") or []))
        if a_places and b_places and (a_places & b_places):
            score += 0.15
            place_match = "text"
    criteria["place_match"] = place_match

    # Entity overlap
    a_actors = set(new_ev.get("actor_entity_ids") or [])
    b_actors = set(candidate.get("actor_entity_ids") or [])
    actor_overlap = len(a_actors & b_actors)
    criteria["actor_overlap"] = actor_overlap
    if actor_overlap > 0:
        score += 0.15 + min(0.10, 0.05 * (actor_overlap - 1))

    a_targets = set(new_ev.get("target_entity_ids") or [])
    b_targets = set(candidate.get("target_entity_ids") or [])
    target_overlap = len(a_targets & b_targets)
    criteria["target_overlap"] = target_overlap
    if target_overlap > 0:
        score += 0.15 + min(0.10, 0.05 * (target_overlap - 1))

    # Title similarity (cheap Jaccard on word tokens)
    sim = _title_jaccard(new_ev.get("title") or "", candidate.get("title") or "")
    criteria["title_similarity"] = round(sim, 2)
    if sim >= 0.6:
        score += 0.10
    elif sim >= 0.3:
        score += 0.05

    return min(score, 1.0), criteria


def _merge_events(conn, *, survivor: dict, loser: dict) -> int:
    """Fold loser into survivor. Returns number of conflict rows written."""
    conflicts_written = 0

    # Defensive: clear any in-flight transaction state from earlier work
    # on this shared connection. Without this, an InFailedSqlTransaction
    # from a prior worker poisons every statement here.
    try: conn.rollback()
    except Exception: pass

    # Detect field-level disagreements BEFORE merging so we can record them
    # with clean provenance (each side's source_signal_rids).
    conflicts_written += _record_conflicts(conn, survivor, loser)

    # Reset txn state — _record_conflicts has nested try/except blocks that
    # swallow exceptions without always rolling back, leaving the connection
    # in InFailedSqlTransaction state.
    try: conn.rollback()
    except Exception: pass

    with conn.cursor() as cur:
        # 1. Union-merge arrays on survivor, take MAX confidence, bump source_count
        cur.execute(
            """
            UPDATE events SET
                source_signal_rids = ARRAY(SELECT DISTINCT unnest(
                    COALESCE(source_signal_rids, ARRAY[]::bigint[])
                    || COALESCE(%(loser_signals)s::bigint[], ARRAY[]::bigint[])
                )),
                actor_entity_ids = ARRAY(SELECT DISTINCT unnest(
                    COALESCE(actor_entity_ids, ARRAY[]::bigint[])
                    || COALESCE(%(loser_actors)s::bigint[], ARRAY[]::bigint[])
                )),
                target_entity_ids = ARRAY(SELECT DISTINCT unnest(
                    COALESCE(target_entity_ids, ARRAY[]::bigint[])
                    || COALESCE(%(loser_targets)s::bigint[], ARRAY[]::bigint[])
                )),
                place_text = ARRAY(SELECT DISTINCT unnest(
                    COALESCE(place_text, ARRAY[]::text[])
                    || COALESCE(%(loser_places)s::text[], ARRAY[]::text[])
                )),
                confidence = GREATEST(confidence, %(loser_conf)s),
                clustered_at = NOW(),
                updated_at = NOW()
            WHERE event_id = %(survivor_id)s
            """,
            {
                "survivor_id": survivor["event_id"],
                "loser_signals": loser.get("source_signal_rids") or [],
                "loser_actors": loser.get("actor_entity_ids") or [],
                "loser_targets": loser.get("target_entity_ids") or [],
                "loser_places": loser.get("place_text") or [],
                "loser_conf": float(loser.get("confidence") or 0.0),
            },
        )
        # Bring source_count in line with the new array cardinality
        cur.execute(
            "UPDATE events SET source_count = cardinality(source_signal_rids) "
            "WHERE event_id = %s",
            (survivor["event_id"],),
        )

        # 2. Repoint edges: anything pointing at loser → point at survivor.
        # knowledge_edges has no updated_at column (created_at only).
        # UNIQUE (source, target, relationship) means a naive UPDATE collides
        # whenever survivor already has the same triple. Pre-delete the
        # loser-side rows that would collide, then UPDATE the rest.
        params = {"survivor_node": survivor["node_id"], "loser_node": loser["node_id"]}
        cur.execute(
            """
            DELETE FROM knowledge_edges ke
            WHERE ke.target_node_id = %(loser_node)s
              AND EXISTS (
                  SELECT 1 FROM knowledge_edges k2
                  WHERE k2.source_node_id = ke.source_node_id
                    AND k2.target_node_id = %(survivor_node)s
                    AND k2.relationship   = ke.relationship
              )
            """,
            params,
        )
        cur.execute(
            """
            UPDATE knowledge_edges
            SET target_node_id = %(survivor_node)s
            WHERE target_node_id = %(loser_node)s
            """,
            params,
        )
        cur.execute(
            """
            DELETE FROM knowledge_edges ke
            WHERE ke.source_node_id = %(loser_node)s
              AND EXISTS (
                  SELECT 1 FROM knowledge_edges k2
                  WHERE k2.source_node_id = %(survivor_node)s
                    AND k2.target_node_id = ke.target_node_id
                    AND k2.relationship   = ke.relationship
              )
            """,
            params,
        )
        cur.execute(
            """
            UPDATE knowledge_edges
            SET source_node_id = %(survivor_node)s
            WHERE source_node_id = %(loser_node)s
            """,
            params,
        )

        # 3. Drop loser's knowledge_nodes card + events row
        cur.execute(
            "DELETE FROM knowledge_nodes WHERE node_id = %s",
            (loser["node_id"],),
        )
        cur.execute(
            "DELETE FROM events WHERE event_id = %s",
            (loser["event_id"],),
        )

    conn.commit()
    return conflicts_written


def _record_conflicts(conn, survivor: dict, loser: dict) -> int:
    """Write node_conflicts rows for field-level disagreements between the
    two events. Returns count of rows written."""
    written = 0
    sources_survivor = _source_rows(conn, survivor.get("source_signal_rids") or [])
    sources_loser = _source_rows(conn, loser.get("source_signal_rids") or [])

    # Title disagreement — both have a title, and they differ on normalized form
    s_title = (survivor.get("title") or "").strip()
    l_title = (loser.get("title") or "").strip()
    if s_title and l_title and _norm(s_title) != _norm(l_title):
        if _insert_conflict(
            conn,
            node_id=survivor["node_id"],
            event_id=survivor["event_id"],
            field_path="identity.title_display",
            field_kind="attribution",
            value_a=s_title, sources_a=sources_survivor,
            value_b=l_title, sources_b=sources_loser,
            severity="info",
        ):
            written += 1

    # Event_time disagreement beyond sub-hour slop
    s_t = survivor.get("event_time")
    l_t = loser.get("event_time")
    if s_t and l_t and abs((s_t - l_t).total_seconds()) > TIME_CONFLICT_SECONDS:
        if _insert_conflict(
            conn,
            node_id=survivor["node_id"],
            event_id=survivor["event_id"],
            field_path="temporal.event_time",
            field_kind="date",
            value_a=s_t.isoformat(), sources_a=sources_survivor,
            value_b=l_t.isoformat(), sources_b=sources_loser,
            severity="warning",
        ):
            written += 1

    # Place disagreement — disjoint place_text sets
    s_p = set(_norm(p) for p in (survivor.get("place_text") or []))
    l_p = set(_norm(p) for p in (loser.get("place_text") or []))
    if s_p and l_p and not (s_p & l_p):
        if _insert_conflict(
            conn,
            node_id=survivor["node_id"],
            event_id=survivor["event_id"],
            field_path="spatial.place_text",
            field_kind="place",
            value_a=sorted(survivor.get("place_text") or []), sources_a=sources_survivor,
            value_b=sorted(loser.get("place_text") or []), sources_b=sources_loser,
            severity="warning",
        ):
            written += 1

    return written


def _insert_conflict(
    conn, *, node_id: str, event_id: int, field_path: str,
    field_kind: str, value_a, sources_a, value_b, sources_b,
    severity: str,
) -> bool:
    # Stabilize order so (a,b) vs (b,a) hash to the same row
    key_a = json.dumps(value_a, default=str, sort_keys=True)
    key_b = json.dumps(value_b, default=str, sort_keys=True)
    if key_a > key_b:
        value_a, value_b = value_b, value_a
        sources_a, sources_b = sources_b, sources_a

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT conflict_id FROM node_conflicts
                WHERE node_id = %(node)s AND field_path = %(field)s
                  AND value_a = %(va)s::jsonb AND value_b = %(vb)s::jsonb
                LIMIT 1
                """,
                {"node": node_id, "field": field_path,
                 "va": json.dumps(value_a, default=str),
                 "vb": json.dumps(value_b, default=str)},
            )
            if cur.fetchone():
                return False    # already recorded
            cur.execute(
                """
                INSERT INTO node_conflicts (
                    tenant_id, node_id, event_id, field_path, field_kind,
                    value_a, sources_a, value_b, sources_b,
                    detected_by, severity
                ) VALUES (
                    'default', %(node)s, %(eid)s, %(field)s, %(kind)s,
                    %(va)s::jsonb, %(sa)s::jsonb,
                    %(vb)s::jsonb, %(sb)s::jsonb,
                    'event_clusterer', %(sev)s
                )
                """,
                {
                    "node": node_id, "eid": event_id,
                    "field": field_path, "kind": field_kind,
                    "va": json.dumps(value_a, default=str),
                    "sa": json.dumps(sources_a, default=str),
                    "vb": json.dumps(value_b, default=str),
                    "sb": json.dumps(sources_b, default=str),
                    "sev": severity,
                },
            )
        return True
    except Exception as e:
        log.debug("conflict insert failed for %s.%s: %s", node_id, field_path, e)
        try: conn.rollback()
        except Exception: pass
        return False


def _source_rows(conn, signal_rids: list) -> list[dict]:
    """Fetch source attribution for each signal_rid: outlet, url, extracted_at."""
    if not signal_rids:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.signal_rid,
                       COALESCE(se.source_outlet, '') AS outlet,
                       COALESCE(se.source_url, '') AS url,
                       s.created_at AS extracted_at
                FROM signals s
                LEFT JOIN signal_extractions se ON se.signal_rid = s.signal_rid
                WHERE s.signal_rid = ANY(%s)
                """,
                (list(signal_rids),),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception:
        return [{"signal_rid": r} for r in signal_rids]


def _queue_review(conn, *, survivor: dict, loser: dict,
                  score: float, criteria: dict) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO event_merge_review (
                    tenant_id, survivor_node_id, loser_node_id,
                    match_score, match_criteria
                ) VALUES (%s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (tenant_id, survivor_node_id, loser_node_id)
                DO UPDATE SET
                    match_score = EXCLUDED.match_score,
                    match_criteria = EXCLUDED.match_criteria,
                    proposed_at = NOW(),
                    status = 'pending'
                """,
                (
                    loser["tenant_id"],
                    survivor["node_id"], loser["node_id"],
                    score, json.dumps(criteria, default=str),
                ),
            )
        conn.commit()
    except Exception as e:
        log.debug("queue_review failed: %s", e)
        try: conn.rollback()
        except Exception: pass


def _mark_clustered(conn, event_id: int) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE events SET clustered_at = NOW() WHERE event_id = %s",
                (event_id,),
            )
        conn.commit()
    except Exception:
        try: conn.rollback()
        except Exception: pass


# ── small helpers ──────────────────────────────────────────────────────

def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _title_jaccard(a: str, b: str) -> float:
    ta = set(_norm(a).split())
    tb = set(_norm(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    from math import radians, sin, cos, sqrt, atan2
    r = 6371.0
    dlat = radians(float(lat2) - float(lat1))
    dlon = radians(float(lon2) - float(lon1))
    a = (sin(dlat / 2) ** 2
         + cos(radians(float(lat1))) * cos(radians(float(lat2))) * sin(dlon / 2) ** 2)
    return 2 * r * atan2(sqrt(a), sqrt(1 - a))
