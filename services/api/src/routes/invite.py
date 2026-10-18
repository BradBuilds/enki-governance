"""
Invite code API endpoints.

Public endpoints (needed by signup flow):
  POST /invite/validate   — check if a code is valid
  POST /invite/redeem      — redeem code after Logto signup

Admin endpoints (require admin role):
  GET  /invite/codes       — list all invite codes
  POST /invite/generate    — generate a new invite code
  POST /invite/{code}/revoke — revoke an invite code
"""

from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from storage.signals.invite_codes import (
    validate_invite_code,
    redeem_invite_code,
    generate_invite_code,
    list_invite_codes,
    revoke_invite_code,
)
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo, require_role

router = APIRouter(prefix="/invite", tags=["invite codes"])


class ValidateRequest(BaseModel):
    code: str
    email: Optional[str] = None

class RedeemRequest(BaseModel):
    code: str
    logto_sub: str
    display_name: str
    email: Optional[str] = None


@router.post("/validate")
async def validate_code(body: ValidateRequest):
    """Check if an invite code is valid. Public — needed by signup page."""
    result = validate_invite_code(body.code, body.email)
    return result


@router.post("/redeem")
async def redeem_code(body: RedeemRequest):
    """Redeem an invite code. Called by SvelteKit callback after Logto signup."""
    result = redeem_invite_code(
        code=body.code,
        logto_sub=body.logto_sub,
        display_name=body.display_name,
        email=body.email,
    )
    if "error" in result:
        return JSONResponse(status_code=400, content=result)
    return JSONResponse(status_code=201, content=result)


# ── Admin endpoints (require admin role) ──


class GenerateRequest(BaseModel):
    tier: str = "pro"
    max_users: int = 10
    email: Optional[str] = None
    expires_days: Optional[int] = 30
    note: Optional[str] = None


@router.get("/codes")
async def list_codes(auth: UnifiedAuthInfo = Depends(require_role("admin"))):
    """List all invite codes. Admin only."""
    codes = list_invite_codes()
    return {"codes": codes}


@router.post("/generate")
async def generate_code(
    body: GenerateRequest,
    auth: UnifiedAuthInfo = Depends(require_role("admin")),
):
    """Generate a new invite code. Admin only."""
    result = generate_invite_code(
        tier=body.tier,
        max_users=body.max_users,
        email=body.email,
        note=body.note,
        created_by=auth.subject_id,
        expires_in_days=body.expires_days,
    )
    return JSONResponse(status_code=201, content=result)


@router.post("/{code}/revoke")
async def revoke_code(
    code: str,
    auth: UnifiedAuthInfo = Depends(require_role("admin")),
):
    """Revoke an unused invite code. Admin only."""
    success = revoke_invite_code(code)
    if success:
        return {"status": "revoked", "code": code}
    return JSONResponse(status_code=400, content={"error": "Could not revoke code"})
