"""
Case and alert API endpoints.

Cases: POST /cases, GET /cases, GET /cases/{id}, PATCH /cases/{id},
       POST /cases/{id}/observables, GET /cases/{id}/timeline
Alerts: GET /alerts, GET /alerts/{id}, PATCH /alerts/{id},
        POST /alerts/{id}/escalate
"""

from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

from storage.signals.cases import (
    create_case, get_case, list_cases, update_case_status,
    add_case_observable, list_case_observables, get_case_timeline,
    get_alert, list_alerts, update_alert_status, escalate_alert_to_case,
)

router = APIRouter(tags=["cases & alerts"])


# ─── Request Models ───────────────────────────────────────────────────────────

class CreateCaseRequest(BaseModel):
    title: str
    priority: str = "medium"
    description: Optional[str] = None
    owner: Optional[str] = None
    tags: Optional[list[str]] = None


class AddObservableRequest(BaseModel):
    observable_type: str
    observable_value: str
    notes: Optional[str] = None


class UpdateStatusRequest(BaseModel):
    status: str
    resolution_type: Optional[str] = None
    resolution_summary: Optional[str] = None


class UpdateAlertRequest(BaseModel):
    status: str
    suppression_reason: Optional[str] = None
    assigned_to: Optional[str] = None


# ─── CASES ─────────────────────────────────────────────────────────────────────

@router.post("/cases")
async def create_case_endpoint(
    body: CreateCaseRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    case = create_case(
        tenant_id=auth.tenant_id, title=body.title, priority=body.priority,
        description=body.description, owner=body.owner, tags=body.tags,
        created_by=auth.subject_id,
    )
    return JSONResponse(status_code=201, content=case)


@router.get("/cases")
async def list_cases_endpoint(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    status: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    owner: Optional[str] = Query(None),
    limit: int = Query(100, le=1000),
):
    cases = list_cases(auth.tenant_id, status=status, priority=priority, owner=owner, limit=limit)
    return {"cases": cases, "count": len(cases)}


@router.get("/cases/{case_id}")
async def get_case_endpoint(
    case_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    case = get_case(case_id, auth.tenant_id)
    if not case:
        return JSONResponse(status_code=404, content={"error": "CASE.NOT_FOUND"})
    return case


@router.patch("/cases/{case_id}")
async def update_case_endpoint(
    case_id: str,
    body: UpdateStatusRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    ok = update_case_status(
        case_id, auth.tenant_id, body.status, auth.subject_id,
        body.resolution_type, body.resolution_summary,
    )
    if not ok:
        return JSONResponse(status_code=404, content={"error": "CASE.NOT_FOUND"})
    return {"case_id": case_id, "status": body.status}


@router.post("/cases/{case_id}/observables")
async def add_observable_endpoint(
    case_id: str,
    body: AddObservableRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    result = add_case_observable(
        case_id, auth.tenant_id, body.observable_type, body.observable_value,
        auth.subject_id, body.notes,
    )
    return JSONResponse(status_code=201, content=result)


@router.get("/cases/{case_id}/observables")
async def list_observables_endpoint(
    case_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    return {"observables": list_case_observables(case_id, auth.tenant_id)}


@router.get("/cases/{case_id}/timeline")
async def case_timeline_endpoint(
    case_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
    limit: int = Query(100, le=1000),
):
    return {"events": get_case_timeline(case_id, auth.tenant_id, limit)}


# ─── ALERTS ────────────────────────────────────────────────────────────────────

@router.get("/alerts")
async def list_alerts_endpoint(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    status: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    limit: int = Query(100, le=1000),
):
    alerts = list_alerts(auth.tenant_id, status=status, severity=severity, category=category, limit=limit)
    return {"alerts": alerts, "count": len(alerts)}


@router.get("/alerts/{alert_id}")
async def get_alert_endpoint(
    alert_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    alert = get_alert(alert_id, auth.tenant_id)
    if not alert:
        return JSONResponse(status_code=404, content={"error": "ALERT.NOT_FOUND"})
    return alert


@router.patch("/alerts/{alert_id}")
async def update_alert_endpoint(
    alert_id: str,
    body: UpdateAlertRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    ok = update_alert_status(
        alert_id, auth.tenant_id, body.status, auth.subject_id,
        body.suppression_reason, body.assigned_to,
    )
    if not ok:
        return JSONResponse(status_code=404, content={"error": "ALERT.NOT_FOUND"})
    return {"alert_id": alert_id, "status": body.status}


@router.post("/alerts/{alert_id}/escalate")
async def escalate_alert_endpoint(
    alert_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    case = escalate_alert_to_case(alert_id, auth.tenant_id, auth.subject_id)
    if not case:
        return JSONResponse(status_code=404, content={"error": "ALERT.NOT_FOUND"})
    return JSONResponse(status_code=201, content=case)
