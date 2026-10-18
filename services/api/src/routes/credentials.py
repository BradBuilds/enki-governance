"""
Research credentials API — read-only view onto the credentials catalog +
configured-state. The wizard step 6 UI reads this to know what to ask for and
what's already filled.

Values are NEVER returned. Use POST /secrets to set a value, this endpoint
only reports presence.

GET /credentials/catalog         — full catalog (sources, signup URLs, unlocks)
GET /credentials/status          — catalog + configured: bool per entry
GET /credentials/rate-limits     — current rate-limit state per source
POST /credentials/refresh        — flush credential cache (post-secret-save hook)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from core.research.credentials import (
    load_catalog,
    credential_status,
    notify_credential_change,
)
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

router = APIRouter(prefix="/credentials", tags=["credentials"])


@router.get("/catalog")
async def get_catalog(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Return the credentials catalog — every credential the system knows
    how to use, what data each unlocks, signup URLs, free/keyed rate limits.
    No tenant-specific info; same for all callers."""
    catalog = load_catalog()
    return {
        "version": catalog.get("version"),
        "description": catalog.get("description"),
        "credentials": catalog.get("credentials", []),
        "principles": catalog.get("_principles", []),
    }


@router.get("/status")
async def get_status(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Per-credential status for this tenant: configured (bool) for each entry
    in the catalog. Values are never returned. Drives wizard step 6 UI."""
    items = credential_status(tenant_id=auth.tenant_id)
    configured_count = sum(1 for c in items if c.get("configured"))
    return {
        "credentials": items,
        "configured": configured_count,
        "total": len(items),
    }


@router.get("/rate-limits")
async def get_rate_limits(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Current rate-limit state per source. Shows which sources are at their
    keyed (higher) tier vs free tier. Useful for the dossier "research
    provenance" panel."""
    from core.investigation.sources import get_rate_limiter_stats
    return {"limiters": get_rate_limiter_stats()}


class RefreshRequest(BaseModel):
    name: str | None = None


@router.post("/refresh")
async def refresh(
    body: RefreshRequest | None = None,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Flush the credential cache so a freshly-saved secret takes effect on
    the next investigation. The secrets POST handler calls this; admins can
    call it directly to force a re-read."""
    name = body.name if body else None
    notify_credential_change(name)
    # Also reset the per-source tier-applied flags so the next acquire()
    # re-evaluates against the new credential state.
    from core.investigation.sources import reset_tier_applied
    reset_tier_applied()
    return {"refreshed": True, "name": name}
