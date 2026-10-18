"""
Intelligence Report endpoints.
POST /reports/generate    — generate a brief NOW
GET  /reports             — list generated reports
GET  /reports/{id}        — get a specific report
POST /reports/schedules   — create a report schedule
GET  /reports/schedules   — list schedules
DELETE /reports/schedules/{id} — delete a schedule
"""

from fastapi import APIRouter, Depends, HTTPException, Header
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from pydantic import BaseModel
from typing import Optional
import logging
import uuid
from datetime import datetime, timezone

log = logging.getLogger("enki.reports")
router = APIRouter(prefix="/reports", tags=["reports"])


class GenerateRequest(BaseModel):
    topics: list[str] = []
    include_alerts: bool = True
    include_cases: bool = True
    include_signals: bool = True


class ScheduleRequest(BaseModel):
    name: str
    cron_expression: str = "0 7 * * *"  # default: daily at 7am
    report_type: str = "intelligence_brief"
    topics: list[str] = []
    delivery: str = "in_app"  # in_app | email


@router.post("/generate")
async def generate_report(body: GenerateRequest, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Generate an intelligence brief immediately."""
    from core.reports.generator import generate_intelligence_brief
    try:
        report = generate_intelligence_brief(
            tenant_id=auth.tenant_id,
            topics=body.topics,
            include_alerts=body.include_alerts,
            include_cases=body.include_cases,
            include_signals=body.include_signals,
        )
        return report
    except Exception as e:
        log.error(f"Report generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("")
async def list_reports(auth: UnifiedAuthInfo = Depends(unified_auth), limit: int = 20):
    """List generated reports."""
    from storage.postgres.db import get_connection
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT report_id, title, report_type, generated_at, section_count
                    FROM reports WHERE tenant_id = %s
                    ORDER BY generated_at DESC LIMIT %s
                """, (auth.tenant_id, limit))
                reports = [{"report_id": r[0], "title": r[1], "report_type": r[2],
                           "generated_at": r[3].isoformat() if r[3] else None,
                           "section_count": r[4]} for r in cur.fetchall()]
        return {"reports": reports}
    except Exception:
        return {"reports": []}


# ── Static /reports/* routes MUST come before /{report_id} ────────────────────

@router.post("/schedules")
async def create_schedule(body: ScheduleRequest, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Create a report schedule."""
    from storage.postgres.db import get_connection
    schedule_id = f"sched_{uuid.uuid4().hex[:12]}"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO report_schedules (schedule_id, tenant_id, name, cron_expression,
                    report_type, topics, delivery, enabled, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (schedule_id, auth.tenant_id, body.name, body.cron_expression,
                  body.report_type, body.topics, body.delivery, True,
                  datetime.now(timezone.utc)))
            conn.commit()
    return {"schedule_id": schedule_id, "name": body.name, "cron": body.cron_expression, "enabled": True}


@router.get("/schedules")
async def list_schedules(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """List report schedules."""
    from storage.postgres.db import get_connection
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT schedule_id, name, cron_expression, report_type, topics, delivery, enabled, created_at
                    FROM report_schedules WHERE tenant_id = %s ORDER BY created_at DESC
                """, (auth.tenant_id,))
                schedules = [{"schedule_id": r[0], "name": r[1], "cron": r[2], "report_type": r[3],
                             "topics": r[4], "delivery": r[5], "enabled": r[6],
                             "created_at": r[7].isoformat() if r[7] else None} for r in cur.fetchall()]
        return {"schedules": schedules}
    except Exception:
        return {"schedules": []}


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(schedule_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Delete a report schedule."""
    from storage.postgres.db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM report_schedules WHERE schedule_id = %s AND tenant_id = %s",
                       (schedule_id, auth.tenant_id))
            conn.commit()
    return {"deleted": True}


# ── Dynamic path param route MUST be last ─────────────────────────────────────

@router.get("/{report_id}")
async def get_report(report_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Get a specific report."""
    from storage.postgres.db import get_connection
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT report_id, title, report_type, generated_at, sections, summary
                    FROM reports WHERE report_id = %s AND tenant_id = %s
                """, (report_id, auth.tenant_id))
                r = cur.fetchone()
                if not r:
                    raise HTTPException(status_code=404, detail="Report not found")
                return {"report_id": r[0], "title": r[1], "report_type": r[2],
                        "generated_at": r[3].isoformat() if r[3] else None,
                        "sections": r[4], "summary": r[5]}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to load report")
