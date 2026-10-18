"""
Secrets API — manage named secrets for connectors and AI config.

POST   /secrets         — create or update a secret
GET    /secrets         — list secrets (values never returned)
DELETE /secrets/{name}  — delete a secret
"""

from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from storage.secrets import create_secret, list_secrets, delete_secret
from core.research.credentials import notify_credential_change
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

router = APIRouter(prefix="/secrets", tags=["secrets"])


class CreateSecretRequest(BaseModel):
    name: str
    value: str
    category: str = "api_key"
    description: str = ""


@router.post("")
async def create_or_update(
    body: CreateSecretRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Create or update a named secret. If the name already exists, the value is replaced."""
    try:
        result = create_secret(
            tenant_id=auth.tenant_id,
            name=body.name,
            value=body.value,
            category=body.category,
            description=body.description,
            created_by=auth.subject_id,
        )
        # Invalidate the research credentials cache + reset tier flags so the
        # next investigation sees the new key without restart. Also enqueue
        # a backfill of prior no_api_key investigations for the sources this
        # credential unlocks.
        if body.category == "api_key":
            notify_credential_change(body.name)
            try:
                from core.investigation.sources import reset_tier_applied
                reset_tier_applied()
            except Exception:
                pass
            try:
                from core.research.credential_backfill import requeue_for_credential
                requeue_for_credential(auth.tenant_id, body.name)
            except Exception:
                pass
        return result
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": "INVALID_NAME", "detail": str(e)})


@router.get("")
async def list_all(
    category: Optional[str] = None,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """List all secrets for this tenant. Values are NEVER returned."""
    secrets = list_secrets(tenant_id=auth.tenant_id, category=category)
    return {"secrets": secrets, "total": len(secrets)}


@router.delete("/{name}")
async def remove(
    name: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Delete a secret by name."""
    ok = delete_secret(tenant_id=auth.tenant_id, name=name)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "SECRET.NOT_FOUND"})
    # Drop cached credential value so the next call falls through to env.
    notify_credential_change(name)
    try:
        from core.investigation.sources import reset_tier_applied
        reset_tier_applied()
    except Exception:
        pass
    return {"deleted": True, "name": name}
