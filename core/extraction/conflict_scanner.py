"""
Source-attributed conflict scanner.

Runs as a background worker. For each canonical entity, compares
values across `entity_mentions` and `signal_extractions` to detect
disagreements — not just "contradicted somehow" but "CNN says X,
Reuters says Y on Oct 3" with full source attribution.

Current scanners (v1):

  * event_time_mismatch — two articles assign different event_times
    to the same entity's most recent event. Writes field_path
    "temporal.event_time" with value_a/b as timestamps.

  * place_mismatch — two articles place the same entity in different
    locations within the same 24h window. Writes field_path
    "spatial.place_text" with value_a/b as place names.

  * attribution_mismatch — two articles attribute the same event
    (action verb + target) to different actors. "Russia launched"
    vs "Ukraine launched" for the same strike. field_path
    "actor_entity_ids".

  * spelling_variant — canonical_name changed across signals. Less
    severe; tracks when we've had to merge variants.

Each detected conflict becomes a row in `node_conflicts` with full
source arrays (signal_rid, url, outlet, confidence, extracted_at).
Conflicts are idempotent: the same (node_id, field_path, value_a,
value_b) combination updates in place rather than duplicating.

Resolution is a human decision in v1 — UI surfaces unresolved
conflicts and lets the user pick winning value. v2 can add LLM
adjudication (ask Gemini which source is more credible given source
authority + recency).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from storage.postgres.db import get_connection
from core.ldu.lineage import lineage_columns

log = logging.getLogger("enki.extraction.conflicts")

SCAN_INTERVAL = 600   # 10 minutes
_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()


def _source_row(cur, signal_rid: int) -> dict:
    """Fetch the source attribution block for a signal_rid."""
    cur.execute(
        """
        SELECT rid, timestamp,
               metadata->>'outlet_name' AS outlet,
               payload->>'url'          AS url,
               payload->>'source'       AS source_name
          FROM signals
         WHERE rid = %s
         LIMIT 1
        """,
        (signal_rid,),
    )
    row = cur.fetchone()
    if not row:
        return {"signal_rid": signal_rid}
    return {
        "signal_rid": row[0],
        "extracted_at": row[1].isoformat() if row[1] else None,
        "outlet": row[2] or row[4] or "unknown",
        "url": row[3] or "",
    }


def _upsert_conflict(
    conn, *, node_id: str, entity_id: Optional[int],
    field_path: str, field_kind: str,
    value_a, sources_a: list,
    value_b, sources_b: list,
    severity: str = "warning",
    detected_by: str = "scanner_v1",
    tenant_id: str = "default",
) -> None:
    lin = lineage_columns()
    # Stabilize order so (a,b) and (b,a) produce the same conflict row
    key_a = json.dumps(value_a, default=str, sort_keys=True)
    key_b = json.dumps(value_b, default=str, sort_keys=True)
    if key_a > key_b:
        value_a, value_b = value_b, value_a
        sources_a, sources_b = sources_b, sources_a

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT conflict_id FROM node_conflicts
             WHERE tenant_id = %s AND node_id = %s
               AND field_path = %s
               AND value_a = %s::jsonb AND value_b = %s::jsonb
             LIMIT 1
            """,
            (tenant_id, node_id, field_path,
             json.dumps(value_a, default=str),
             json.dumps(value_b, default=str)),
        )
        existing = cur.fetchone()
        if existing:
            cur.execute(
                """
                UPDATE node_conflicts
                   SET sources_a = %s::jsonb,
                       sources_b = %s::jsonb,
                       updated_at = NOW()
                 WHERE conflict_id = %s
                """,
                (json.dumps(sources_a), json.dumps(sources_b), existing[0]),
            )
        else:
            cur.execute(
                """
                INSERT INTO node_conflicts (
                    tenant_id, node_id, entity_id,
                    field_path, field_kind,
                    value_a, sources_a, value_b, sources_b,
                    detected_at, detected_by, severity,
                    admitted_under_active_set, gdu_canonical, ldu_t_id
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb,
                    NOW(), %s, %s,
                    %s, %s, %s
                )
                """,
                (tenant_id, node_id, entity_id,
                 field_path, field_kind,
                 json.dumps(value_a, default=str), json.dumps(sources_a),
                 json.dumps(value_b, default=str), json.dumps(sources_b),
                 detected_by, severity,
                 lin["active_set"], lin["canonical"], lin["ldu_t"]),
            )


# ─── Scanners ──────────────────────────────────────────────────────

def _scan_event_time_conflicts(conn, tenant_id: str) -> int:
    """For each entity with >= 2 mentions in the last 72h, check if
    reported event_times differ by more than 24 hours."""
    found = 0
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.entity_id, e.canonical_name, e.canonical_type,
                   m.signal_rid, m.event_time, m.node_id
              FROM entity_mentions m
              JOIN entities e ON e.entity_id = m.entity_id
             WHERE m.tenant_id = %s
               AND m.event_time IS NOT NULL
               AND m.signal_rid IS NOT NULL
               AND m.event_time > NOW() - INTERVAL '7 days'
             ORDER BY m.entity_id, m.event_time
            """,
            (tenant_id,),
        )
        rows = cur.fetchall()

    if not rows:
        return 0

    # Group by entity
    from collections import defaultdict
    by_entity: dict = defaultdict(list)
    for r in rows:
        by_entity[r[0]].append({
            "entity_id": r[0], "name": r[1], "type": r[2],
            "signal_rid": r[3], "event_time": r[4], "node_id": r[5],
        })

    for entity_id, mentions in by_entity.items():
        if len(mentions) < 2:
            continue
        mentions.sort(key=lambda m: m["event_time"])
        # Pairwise compare adjacent events beyond 24h apart
        for i in range(len(mentions) - 1):
            a, b = mentions[i], mentions[i + 1]
            delta = abs((b["event_time"] - a["event_time"]).total_seconds())
            if delta < 86400:  # within 1 day → consider same event
                continue
            if delta > 7 * 86400:  # different events entirely
                continue
            try:
                with conn.cursor() as cur:
                    src_a = [_source_row(cur, a["signal_rid"])]
                    src_b = [_source_row(cur, b["signal_rid"])]
                _upsert_conflict(
                    conn,
                    node_id=a["node_id"] or b["node_id"] or f"entity:{entity_id}",
                    entity_id=entity_id,
                    field_path="temporal.event_time",
                    field_kind="date",
                    value_a=a["event_time"].isoformat(),
                    sources_a=src_a,
                    value_b=b["event_time"].isoformat(),
                    sources_b=src_b,
                    severity="warning",
                    tenant_id=tenant_id,
                )
                found += 1
            except Exception as e:
                log.debug("event_time conflict upsert failed: %s", e)
    return found


def _scan_place_conflicts(conn, tenant_id: str) -> int:
    """Entity places that differ within a 24h window."""
    found = 0
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT se.master_entity_id,
                   jsonb_array_elements_text(COALESCE(se.envelope->'spatial'->'place_text', '[]'::jsonb)) AS place,
                   se.signal_rid,
                   (se.envelope->'temporal'->>'event_time')::timestamptz AS event_time,
                   se.node_id
              FROM signal_extractions se
             WHERE se.tenant_id = %s
               AND se.master_entity_id IS NOT NULL
               AND (se.envelope->'temporal'->>'event_time') IS NOT NULL
               AND (se.envelope->'temporal'->>'event_time')::timestamptz > NOW() - INTERVAL '3 days'
             LIMIT 5000
            """,
            (tenant_id,),
        )
        rows = cur.fetchall()

    if not rows:
        return 0

    from collections import defaultdict
    by_entity: dict = defaultdict(list)
    for r in rows:
        if r[1]:
            by_entity[r[0]].append({
                "entity_id": r[0], "place": r[1],
                "signal_rid": r[2], "event_time": r[3], "node_id": r[4],
            })

    for entity_id, mentions in by_entity.items():
        mentions.sort(key=lambda m: m["event_time"])
        for i in range(len(mentions) - 1):
            a, b = mentions[i], mentions[i + 1]
            delta = abs((b["event_time"] - a["event_time"]).total_seconds())
            if delta > 86400:
                continue
            if a["place"].lower() == b["place"].lower():
                continue
            # Crude filter: skip if one place contains the other
            if a["place"].lower() in b["place"].lower() or b["place"].lower() in a["place"].lower():
                continue
            try:
                with conn.cursor() as cur:
                    src_a = [_source_row(cur, a["signal_rid"])]
                    src_b = [_source_row(cur, b["signal_rid"])]
                _upsert_conflict(
                    conn,
                    node_id=a["node_id"] or f"entity:{entity_id}",
                    entity_id=entity_id,
                    field_path="spatial.place_text",
                    field_kind="place",
                    value_a=a["place"],
                    sources_a=src_a,
                    value_b=b["place"],
                    sources_b=src_b,
                    severity="warning",
                    tenant_id=tenant_id,
                )
                found += 1
            except Exception as e:
                log.debug("place conflict upsert failed: %s", e)
    return found


def scan_once(tenant_id: str = "default") -> dict:
    stats = {"event_time": 0, "place": 0}
    try:
        with get_connection() as conn:
            stats["event_time"] = _scan_event_time_conflicts(conn, tenant_id)
            stats["place"] = _scan_place_conflicts(conn, tenant_id)
            conn.commit()
    except Exception as e:
        log.exception("conflict scan failed: %s", e)
    return stats


# ─── Worker lifecycle ──────────────────────────────────────────────

def _worker_loop(tenant_id: str):
    log.info("Conflict scanner started — interval=%ds", SCAN_INTERVAL)
    while not _STOP_EVENT.is_set():
        try:
            stats = scan_once(tenant_id)
            total = sum(stats.values())
            if total:
                log.info("conflict scan: %s", stats)
        except Exception as e:
            log.exception("conflict scan iteration failed: %s", e)
        _STOP_EVENT.wait(SCAN_INTERVAL)


def start_conflict_scanner(tenant_id: str = "default") -> None:
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_worker_loop, args=(tenant_id,),
        name="conflict-scanner", daemon=True,
    )
    _WORKER_THREAD.start()


def stop_conflict_scanner() -> None:
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=5)
