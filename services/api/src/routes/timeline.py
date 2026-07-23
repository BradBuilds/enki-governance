"""
Unified Timeline — merges signals, alerts, cases, and audit events into one feed.
GET /timeline — chronological activity feed
"""

from fastapi import APIRouter, Depends, Header, Query
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from typing import Optional
from datetime import datetime, timezone, timedelta
import logging

log = logging.getLogger("enki.timeline")
router = APIRouter(prefix="/timeline", tags=["timeline"])


@router.get("")
async def get_timeline(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    after: Optional[str] = None,
    before: Optional[str] = None,
    types: Optional[str] = Query(None, description="Comma-separated: signal,alert,case,audit"),
    limit: int = 50,
):
    """Unified activity feed merging across tables."""
    from storage.postgres.db import get_connection

    now = datetime.now(timezone.utc)
    after_dt = datetime.fromisoformat(after) if after else now - timedelta(hours=24)
    before_dt = datetime.fromisoformat(before) if before else now
    type_filter = set(types.split(",")) if types else {"signal", "alert", "case", "audit"}

    events = []

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Recent signals
                if "signal" in type_filter:
                    cur.execute("""
                        SELECT signal_id, signal_type, severity, device_id, timestamp
                        FROM signals WHERE tenant_id = %s AND timestamp BETWEEN %s AND %s
                        ORDER BY timestamp DESC LIMIT %s
                    """, (auth.tenant_id, after_dt, before_dt, limit))
                    for r in cur.fetchall():
                        events.append({
                            "event_id": r[0], "event_type": "signal_ingest", "category": "signal",
                            "title": f"{r[1]}", "detail": f"from {r[3] or 'unknown'} · {r[2] or 'info'}",
                            "timestamp": r[4].isoformat() if r[4] else None,
                            "entity_id": r[0], "entity_type": "signal",
                        })

                # Recent alerts
                if "alert" in type_filter:
                    cur.execute("""
                        SELECT alert_id, title, severity, status, created_at
                        FROM alerts WHERE tenant_id = %s AND created_at BETWEEN %s AND %s
                        ORDER BY created_at DESC LIMIT %s
                    """, (auth.tenant_id, after_dt, before_dt, limit))
                    for r in cur.fetchall():
                        events.append({
                            "event_id": r[0], "event_type": "alert_created", "category": "alert",
                            "title": r[1], "detail": f"{r[2]} · {r[3]}",
                            "timestamp": r[4].isoformat() if r[4] else None,
                            "entity_id": r[0], "entity_type": "alert",
                        })

                # Recent case updates
                if "case" in type_filter:
                    cur.execute("""
                        SELECT case_id, title, status, priority, updated_at
                        FROM cases WHERE tenant_id = %s AND updated_at BETWEEN %s AND %s
                        ORDER BY updated_at DESC LIMIT %s
                    """, (auth.tenant_id, after_dt, before_dt, limit))
                    for r in cur.fetchall():
                        events.append({
                            "event_id": r[0], "event_type": "case_updated", "category": "case",
                            "title": r[1], "detail": f"{r[2]} · {r[3]}",
                            "timestamp": r[4].isoformat() if r[4] else None,
                            "entity_id": r[0], "entity_type": "case",
                        })

                # Audit events
                if "audit" in type_filter:
                    try:
                        cur.execute("""
                            SELECT event_id, event_type, actor_id, commit_time, new_value
                            FROM audit_events WHERE tenant_id = %s AND commit_time BETWEEN %s AND %s
                            ORDER BY commit_time DESC LIMIT %s
                        """, (auth.tenant_id, after_dt, before_dt, limit))
                        for r in cur.fetchall():
                            events.append({
                                "event_id": str(r[0]), "event_type": r[1], "category": "audit",
                                "title": r[1], "detail": f"by {r[2] or 'system'}",
                                "timestamp": r[3].isoformat() if r[3] else None,
                                "entity_id": str(r[0]), "entity_type": "audit",
                            })
                    except Exception:
                        pass  # audit_events table may not exist yet

    except Exception as e:
        log.error(f"Timeline query failed: {e}")
        return {"events": [], "error": str(e)}

    # Sort by timestamp descending
    events.sort(key=lambda e: e.get("timestamp") or "", reverse=True)
    return {"events": events[:limit]}
