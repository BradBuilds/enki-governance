"""
Admin endpoints: challenge issuance, device enrollment.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from storage.auth.config import CHALLENGE_TTL_SECONDS
from storage.auth.grant import issue_device_grant, DeviceTier, Algorithm

from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from services.api.src.deps import raise_enki_error

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/challenges/issue")
def issue_challenge(
    request: Request,
    device_id: str = Query(...),
):
    """Issue an authentication challenge for a device (public endpoint)."""
    challenge_store = request.app.state.challenge_store

    try:
        c = challenge_store.issue(device_id=device_id, ttl_seconds=CHALLENGE_TTL_SECONDS)
    except RuntimeError:
        return JSONResponse(
            status_code=429,
            content={"error_code": "API.RATE_LIMITED", "detail": "Too many active challenges"},
            headers={"Retry-After": "60"},
        )
    return JSONResponse(
        {
            "challenge_id": c.challenge_id,
            "nonce": c.nonce,
            "issued_at": c.issued_at.isoformat(),
            "expires_at": c.expires_at.isoformat(),
        }
    )


class DeviceEnrollmentRequest(BaseModel):
    """
    EMULATED mode enrollment request.

    NOTE: explicit_tier is REQUIRED in EMULATED mode per governance corrections.
    PRODUCTION mode may add attestation inputs when VERIFY_ATTESTATION=True.
    """
    device_id: str
    public_key: str  # base64
    public_key_algorithm: Algorithm
    tenant_scopes: list[str]
    explicit_tier: DeviceTier = Field(..., description="Required in EMULATED mode")
    expires_days: int = Field(365, ge=1, le=3650)
    metadata: Optional[dict[str, Any]] = None


@router.post("/enroll_device")
async def enroll_device(
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """
    Enroll a device (Tier K required).

    The unified_auth dependency handles challenge-response verification.
    """
    # Tier-K gate
    if auth.device_tier != "K":
        raise_enki_error("AUTH.ADMIN_TIER_REQUIRED", "Admin enrollment requires Tier K")

    body = await request.body()

    # Parse request body
    try:
        payload = DeviceEnrollmentRequest.model_validate_json(body)
    except Exception as e:
        raise_enki_error("API.BAD_REQUEST", f"Invalid enrollment payload: {e}")

    # Issue grant for new device
    try:
        public_key_bytes = base64.b64decode(payload.public_key)
    except Exception:
        raise_enki_error("API.BAD_REQUEST", "public_key must be valid base64")

    expires_at = (datetime.now(timezone.utc) + timedelta(days=payload.expires_days)).isoformat()

    root_key = request.app.state.root_key
    grant_store = request.app.state.grant_store

    grant = issue_device_grant(
        device_id=payload.device_id,
        public_key_bytes=public_key_bytes,
        public_key_algorithm=payload.public_key_algorithm,
        device_tier=payload.explicit_tier,
        tenant_scopes=payload.tenant_scopes,
        root_key=root_key,
        expires_at=expires_at,
    )

    grant_store.save(grant)

    return JSONResponse(grant.to_dict())
