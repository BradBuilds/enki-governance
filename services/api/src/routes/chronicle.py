"""
Chronicle API — daily intelligence summaries produced by chronicle_worker.py.

Reads knowledge_nodes where node_type='chronicle' and subtype='day', returning
the envelope-packed summary for one or many days.

GET /chronicle                  — list chronicles (most recent first, paginated)
GET /chronicle/{date}           — full chronicle for YYYY-MM-DD
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date as date_cls

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

log = logging.getLogger("enki.chronicle")
router = APIRouter(prefix="/chronicle", tags=["chronicle"])

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _project_chronicle(row: dict, full: bool = False) -> dict:
    """Shape a knowledge_nodes row into a chronicle summary."""
    env = row.get("envelope") or {}
    if isinstance(env, str):
        try:
            env = json.loads(env)
        except Exception:
            env = {}
    if not isinstance(env, dict):
        env = {}

    summary = env.get("summary") or {}
    temporal = env.get("temporal") or {}
    stats = summary.get("structured_fields") or {}

    out = {
        "node_id": row.get("node_id"),
        "date": stats.get("date") or (temporal.get("event_time") or "")[:10],
        "title": row.get("title") or summary.get("headline") or "",
        "narrative": summary.get("narrative") or "",
        "key_facts": summary.get("key_facts") or [],
        "total_nodes": int(stats.get("total_nodes") or 0),
        "top_topics": stats.get("top_topics") or [],
        "top_places": stats.get("top_places") or [],
        "top_outlets": stats.get("top_outlets") or [],
        "tier_distribution": stats.get("tier_distribution") or {},
        "by_type": stats.get("by_type") or [],
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }
    if full:
        out["envelope"] = env
    return out


@router.get("")
async def list_chronicles(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    limit: int = Query(30, le=180),
    offset: int = Query(0, ge=0),
):
    """List chronicles, newest first. Default returns the last 30 days."""
    from storage.postgres.db import get_connection
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT node_id, title, envelope, updated_at
                      FROM knowledge_nodes
                     WHERE tenant_id = %s
                       AND node_type = 'chronicle'
                       AND subtype = 'day'
                     ORDER BY COALESCE((envelope->'temporal'->>'event_time')::timestamptz, updated_at) DESC
                     LIMIT %s OFFSET %s
                    """,
                    (auth.tenant_id, limit, offset),
                )
                cols = [d[0] for d in cur.description]
                chronicles = [_project_chronicle(dict(zip(cols, r))) for r in cur.fetchall()]
                return {"chronicles": chronicles, "count": len(chronicles), "offset": offset, "limit": limit}
    except Exception as e:
        log.exception("chronicle list failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{day}")
async def get_chronicle(
    day: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Return one chronicle by date (YYYY-MM-DD)."""
    if not _DATE_RE.match(day):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    from storage.postgres.db import get_connection

    node_id = f"chronicle:{day}"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT node_id, title, envelope, updated_at
                      FROM knowledge_nodes
                     WHERE tenant_id = %s AND node_id = %s
                    """,
                    (auth.tenant_id, node_id),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail=f"no chronicle for {day}")
                cols = [d[0] for d in cur.description]
                return _project_chronicle(dict(zip(cols, row)), full=True)
    except HTTPException:
        raise
    except Exception as e:
        log.exception("chronicle detail failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{day}/rich")
async def get_chronicle_rich(
    day: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Named-item daily summary derived from raw signals/entities/assessments
    on the requested day. Independent of the chronicle_worker's envelope —
    works even when the worker hasn't run, and surfaces actual headlines /
    entity names / contradiction targets instead of the aggregate-counts
    summary the worker produces.

    Returns:
      top_story            : most-cited signal on the day (title, source, link)
      first_seen_entities  : entities whose earliest mention was this day
      confirmed_today      : assessments first marked fact-tier on this day
      contested_today      : assessments first contradicted on this day
      activity             : small contextual numbers (docs/statements/etc.)
    """
    if not _DATE_RE.match(day):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    from datetime import datetime, time, timezone, timedelta
    from storage.postgres.db import get_connection

    start = datetime.combine(date_cls.fromisoformat(day), time(0, 0), tzinfo=timezone.utc)
    end = start + timedelta(days=1)

    out: dict = {
        "day": day,
        "top_story": None,
        "first_seen_entities": [],
        "confirmed_today": [],
        "contested_today": [],
        "activity": {
            "documents_ingested": 0,
            "statements_promoted": 0,
            "entities_first_seen": 0,
        },
    }

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Top story — signal with the most distinct entity mentions
                # within the day. Captures 'the story everyone is reporting'.
                cur.execute(
                    """
                    SELECT s.rid, s.signal_id,
                           s.payload->>'title'  AS title,
                           s.payload->>'source' AS source,
                           s.payload->>'link'   AS link,
                           COUNT(DISTINCT em.entity_id) AS entity_count,
                           COUNT(DISTINCT em.signal_rid) AS mention_count
                      FROM signals s
                      LEFT JOIN entity_mentions em ON em.signal_rid = s.rid
                     WHERE s.tenant_id = %s
                       AND s.timestamp >= %s
                       AND s.timestamp <  %s
                       AND s.signal_type = 'signal.content.rss_entry'
                     GROUP BY s.rid, s.signal_id, title, source, link
                     ORDER BY entity_count DESC NULLS LAST,
                              mention_count DESC NULLS LAST
                     LIMIT 1
                    """,
                    (auth.tenant_id, start, end),
                )
                r = cur.fetchone()
                if r and r[2]:
                    out["top_story"] = {
                        "signal_rid": r[0],
                        "signal_id":  r[1],
                        "title":      r[2],
                        "source":     r[3],
                        "link":       r[4],
                        "entity_count":  int(r[5] or 0),
                        "mention_count": int(r[6] or 0),
                    }

                # First-seen entities — those whose earliest mention is today.
                cur.execute(
                    """
                    SELECT e.entity_id, e.canonical_name, e.canonical_type,
                           first_seen.min_t,
                           COUNT(em.mention_id) AS day_mentions
                      FROM entities e
                      JOIN (
                          SELECT entity_id, MIN(created_at) AS min_t
                            FROM entity_mentions
                           WHERE tenant_id = %s
                           GROUP BY entity_id
                          HAVING MIN(created_at) >= %s
                             AND MIN(created_at) <  %s
                      ) first_seen ON first_seen.entity_id = e.entity_id
                      LEFT JOIN entity_mentions em
                             ON em.entity_id = e.entity_id
                            AND em.created_at >= %s
                            AND em.created_at <  %s
                     WHERE e.tenant_id = %s
                     GROUP BY e.entity_id, e.canonical_name, e.canonical_type, first_seen.min_t
                     ORDER BY day_mentions DESC NULLS LAST, e.entity_id ASC
                     LIMIT 12
                    """,
                    (auth.tenant_id, start, end, start, end, auth.tenant_id),
                )
                for row in cur.fetchall():
                    out["first_seen_entities"].append({
                        "entity_id": row[0],
                        "name":      row[1],
                        "type":      row[2],
                        "first_seen": row[3].isoformat() if row[3] else None,
                        "mentions_today": int(row[4] or 0),
                    })

                # Confirmed today — assessments first reaching fact tier today.
                cur.execute(
                    """
                    SELECT a.assessment_id,
                           a.subj_entity_id, subj.canonical_name AS subj,
                           a.predicate_edge_type,
                           a.obj_entity_id,  obj.canonical_name  AS obj,
                           a.corroborated_by_count
                      FROM assessments a
                      LEFT JOIN entities subj ON subj.entity_id = a.subj_entity_id
                      LEFT JOIN entities obj  ON obj.entity_id  = a.obj_entity_id
                     WHERE a.assessment_tier = 'fact'
                       AND a.first_seen >= %s
                       AND a.first_seen <  %s
                     ORDER BY a.corroborated_by_count DESC NULLS LAST
                     LIMIT 8
                    """,
                    (start, end),
                )
                for row in cur.fetchall():
                    out["confirmed_today"].append({
                        "assessment_id": row[0],
                        "subj_entity_id": row[1],
                        "subject":   row[2],
                        "predicate": row[3],
                        "obj_entity_id":  row[4],
                        "object":    row[5],
                        "sources":   int(row[6] or 0),
                    })

                # Contested today — assessments newly contradicted today.
                cur.execute(
                    """
                    SELECT a.assessment_id,
                           a.subj_entity_id, subj.canonical_name AS subj,
                           a.predicate_edge_type,
                           a.obj_entity_id,  obj.canonical_name  AS obj,
                           a.corroborated_by_count, a.contradicted_by_count
                      FROM assessments a
                      LEFT JOIN entities subj ON subj.entity_id = a.subj_entity_id
                      LEFT JOIN entities obj  ON obj.entity_id  = a.obj_entity_id
                     WHERE a.contradicted_by_count > 0
                       AND a.first_seen >= %s
                       AND a.first_seen <  %s
                     ORDER BY a.contradicted_by_count DESC
                     LIMIT 8
                    """,
                    (start, end),
                )
                for row in cur.fetchall():
                    out["contested_today"].append({
                        "assessment_id": row[0],
                        "subj_entity_id": row[1],
                        "subject":     row[2],
                        "predicate":   row[3],
                        "obj_entity_id":  row[4],
                        "object":      row[5],
                        "corroborated": int(row[6] or 0),
                        "contradicted": int(row[7] or 0),
                    })

                # Activity numbers — small + contextual, not vanity.
                cur.execute(
                    "SELECT COUNT(*) FROM documents WHERE created_at >= %s AND created_at < %s",
                    (start, end),
                )
                out["activity"]["documents_ingested"] = int(cur.fetchone()[0] or 0)
                cur.execute(
                    "SELECT COUNT(*) FROM statements WHERE created_at >= %s AND created_at < %s",
                    (start, end),
                )
                out["activity"]["statements_promoted"] = int(cur.fetchone()[0] or 0)
                out["activity"]["entities_first_seen"] = len(out["first_seen_entities"])

    except Exception as e:
        log.exception("chronicle rich-day failed")
        raise HTTPException(status_code=500, detail=str(e))

    return out


@router.post("/run")
async def trigger_chronicle_run(
    backfill_days: int = 7,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Trigger the chronicle_worker as a background process. Useful when
    the systemd timer hasn't fired yet (fresh install, just-restarted
    node) and the operator wants to populate /chronicle without waiting
    for the 01:30 UTC tick.

    backfill_days: how many days back to fill. Default 7. Capped at 60.
    Worker is invoked detached so the request returns immediately.
    """
    import subprocess
    from datetime import datetime, timedelta, timezone
    days = max(1, min(backfill_days or 7, 60))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    try:
        proc = subprocess.Popen(
            ["python3", "/app/scripts/chronicle_worker.py",
             "--backfill", "--since", since],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        return {
            "status": "started",
            "pid": proc.pid,
            "since": since,
            "backfill_days": days,
        }
    except Exception as e:
        log.exception("chronicle worker spawn failed")
        raise HTTPException(status_code=500, detail=str(e))
