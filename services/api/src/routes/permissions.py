"""
Access Permission API — manage who can see whose personal data.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

router = APIRouter(prefix="/permissions", tags=["permissions"])


@router.get("")
async def list_my_permissions(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """List all permissions for the current user's data (who can see my data?)."""
    from core.access.permissions import list_permissions
    perms = list_permissions(target_id=auth.identity_id, tenant_id=auth.tenant_id)
    return {"permissions": perms, "count": len(perms)}


@router.post("")
async def set_permission(request: Request, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Set or update a permission rule.

    Body: {
        "identity_id": "sarah",      -- who gets access
        "data_type": "location",     -- what type (location, contacts, financial, communications, photos, calendar, all)
        "allowed": true,             -- grant or deny
        "alert_on_access": true      -- notify me when they access
    }
    """
    from core.access.permissions import set_permission as _set

    body = await request.json()
    identity_id = body.get("identity_id", "")
    data_type = body.get("data_type", "")
    allowed = body.get("allowed", False)
    alert_on_access = body.get("alert_on_access", True)

    if not identity_id or not data_type:
        return JSONResponse(status_code=400, content={"error": "identity_id and data_type required"})

    ok = _set(
        identity_id=identity_id,
        target_id=auth.identity_id,
        data_type=data_type,
        allowed=allowed,
        alert_on_access=alert_on_access,
        granted_by=auth.identity_id,
        tenant_id=auth.tenant_id,
    )

    return {"updated": ok, "identity_id": identity_id, "data_type": data_type, "allowed": allowed}


@router.get("/log")
async def my_access_log(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Who has accessed my data recently?"""
    from core.access.permissions import get_access_log
    entries = get_access_log(target_id=auth.identity_id, tenant_id=auth.tenant_id, limit=50)
    return {"entries": entries, "count": len(entries)}


@router.get("/log/{target_id}")
async def access_log_for(target_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Access log for a specific target (admin view)."""
    from core.access.permissions import get_access_log
    entries = get_access_log(target_id=target_id, tenant_id=auth.tenant_id, limit=50)
    return {"entries": entries, "count": len(entries)}
