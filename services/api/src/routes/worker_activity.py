"""
Worker Activity Feed — shows recent discoveries across all workers.

Queries gdu_admission_log, entity_events, and investigation_findings
to build a unified activity feed showing what the system has found.
"""

from fastapi import APIRouter, Depends, Query
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from storage.postgres.db import get_connection

router = APIRouter(prefix="/workers", tags=["workers"])


@router.get("/activity")
async def worker_activity(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    limit: int = Query(50, le=200),
    since: str = Query(None),
):
    """Recent worker discoveries: entities admitted, nodes created, edges formed."""
    events = []

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                since_clause = ""
                since_params: list = [auth.tenant_id, limit]
                if since:
                    since_clause = "AND created_at > %s"
                    since_params = [auth.tenant_id, since, limit]

                # GDU admissions (most valuable — these are actual graph mutations)
                cur.execute(f"""
                    SELECT operation, category, subtype, base60_address,
                           input_type, reason, created_at, node_id
                    FROM gdu_admission_log
                    WHERE tenant_id = %s {since_clause}
                    ORDER BY created_at DESC
                    LIMIT %s
                """, tuple(since_params))

                for r in cur.fetchall():
                    events.append({
                        "worker": "gdu_gateway",
                        "action": r[0],  # admit, collapse, reject
                        "category": r[1],
                        "subtype": r[2],
                        "base60_address": r[3],
                        "entity_type": r[4],
                        "detail": r[5],
                        "timestamp": r[6].isoformat() if r[6] else None,
                        "node_id": r[7],
                    })

                # Investigation findings (high-value discoveries from public sources)
                cur.execute(f"""
                    SELECT f.source, f.finding_type, f.title,
                           f.created_at, i.seed_name, i.seed_type
                    FROM investigation_findings f
                    JOIN entity_investigations i ON f.investigation_id = i.investigation_id
                    WHERE i.tenant_id = %s {since_clause}
                    ORDER BY f.created_at DESC
                    LIMIT %s
                """, tuple(since_params))

                for r in cur.fetchall():
                    events.append({
                        "worker": "investigation",
                        "action": "finding",
                        "category": r[1],  # finding_type
                        "subtype": r[0],   # source (SEC, FEC, etc.)
                        "detail": r[2],    # title
                        "timestamp": r[3].isoformat() if r[3] else None,
                        "entity_name": r[4],
                        "entity_type": r[5],
                    })

    except Exception as e:
        return {"events": [], "error": str(e)}

    # Sort all events by timestamp descending
    events.sort(key=lambda e: e.get("timestamp") or "", reverse=True)
    events = events[:limit]

    return {"events": events, "count": len(events)}
