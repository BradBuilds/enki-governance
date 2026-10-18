"""
Profile, identity, tenant, and preferences API endpoints.

Tenants:      POST /tenants, GET /tenants, GET /tenants/{id}
Identities:   POST /identities, GET /identities, GET /identities/{id}
Preferences:  GET /preferences, PATCH /preferences
Profile:      GET /profile (unified view of everything)
"""

from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo, require_role
from pydantic import BaseModel

from storage.signals.profiles import (
    create_tenant, get_tenant, list_tenants,
    create_identity, get_identity, list_identities,
    get_preferences, update_preferences,
    get_full_profile,
    find_or_create_by_logto_sub,
)

router = APIRouter(tags=["profiles & identity"])


# ─── Models ───────────────────────────────────────────────────────────────────

class CreateTenantRequest(BaseModel):
    tenant_id: str
    display_name: str

class CreateIdentityRequest(BaseModel):
    display_name: str
    role: str = "member"

class UpdatePreferencesRequest(BaseModel):
    default_view: Optional[str] = None
    theme: Optional[str] = None
    dashboard_profiles: Optional[list] = None
    active_dashboard_profile: Optional[str] = None
    timezone: Optional[str] = None


# ─── Tenants ──────────────────────────────────────────────────────────────────

@router.post("/tenants")
async def create_tenant_endpoint(body: CreateTenantRequest, auth: UnifiedAuthInfo = Depends(require_role("admin"))):
    from storage.node.identity import load_node_id
    try:
        node_id = load_node_id()
    except EnvironmentError:
        node_id = "local"
    result = create_tenant(body.tenant_id, body.display_name, node_id)
    return JSONResponse(status_code=201, content=result)

@router.get("/tenants")
async def list_tenants_endpoint(auth: UnifiedAuthInfo = Depends(require_role("admin"))):
    return {"tenants": list_tenants()}

@router.get("/tenants/{tenant_id}")
async def get_tenant_endpoint(tenant_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    tenant = get_tenant(tenant_id)
    if not tenant:
        return JSONResponse(status_code=404, content={"error": "TENANT.NOT_FOUND"})
    return tenant


# ─── Identities ───────────────────────────────────────────────────────────────

@router.post("/identities")
async def create_identity_endpoint(
    body: CreateIdentityRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    result = create_identity(auth.tenant_id, body.display_name, body.role)
    return JSONResponse(status_code=201, content=result)

@router.get("/identities")
async def list_identities_endpoint(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    return {"identities": list_identities(auth.tenant_id)}

@router.get("/identities/{identity_id}")
async def get_identity_endpoint(
    identity_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    identity = get_identity(identity_id, auth.tenant_id)
    if not identity:
        return JSONResponse(status_code=404, content={"error": "IDENTITY.NOT_FOUND"})
    return identity


class UpdateRoleRequest(BaseModel):
    role: str


@router.patch("/identities/{identity_id}/role")
async def update_identity_role(
    identity_id: str,
    body: UpdateRoleRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Change a team member's role. Admin only."""
    if body.role not in ("admin", "analyst", "viewer", "member"):
        return JSONResponse(status_code=400, content={"error": "INVALID_ROLE"})
    from storage.postgres.db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE identities SET role = %s, updated_at = NOW() WHERE identity_id = %s AND tenant_id = %s",
                (body.role, identity_id, auth.tenant_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                return JSONResponse(status_code=404, content={"error": "IDENTITY.NOT_FOUND"})
    return {"identity_id": identity_id, "role": body.role}


@router.delete("/identities/{identity_id}")
async def remove_identity(
    identity_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Remove a team member. Admin only. Cannot remove self (enforced by frontend)."""
    from storage.postgres.db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM identities WHERE identity_id = %s AND tenant_id = %s",
                (identity_id, auth.tenant_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                return JSONResponse(status_code=404, content={"error": "IDENTITY.NOT_FOUND"})
    return {"status": "removed", "identity_id": identity_id}


# ─── Preferences ──────────────────────────────────────────────────────────────

@router.get("/preferences")
async def get_preferences_endpoint(
):
    prefs = get_preferences(auth.subject_id)
    if not prefs:
        return JSONResponse(status_code=404, content={"error": "PREFERENCES.NOT_FOUND"})
    return prefs

@router.patch("/preferences")
async def update_preferences_endpoint(
    body: UpdatePreferencesRequest,
):
    ok = update_preferences(
        auth.subject_id, body.default_view, body.theme,
        body.dashboard_profiles, body.active_dashboard_profile, body.timezone,
    )
    if not ok:
        return JSONResponse(status_code=404, content={"error": "PREFERENCES.NOT_FOUND"})
    return {"updated": True}


# ─── Auth Identity Resolution ────────────────────────────────────────────────

class ResolveIdentityRequest(BaseModel):
    logto_sub: str
    display_name: str = "New User"
    email: Optional[str] = None

@router.post("/auth/me")
async def resolve_identity(
    body: ResolveIdentityRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """
    Resolve a Logto sub to an Enki identity.
    Called by the SvelteKit server after login.
    Creates identity on first login.
    """
    result = find_or_create_by_logto_sub(
        logto_sub=body.logto_sub,
        tenant_id=auth.tenant_id,
        display_name=body.display_name,
        email=body.email,
    )
    identity_id = result["identity_id"]
    profile = get_full_profile(identity_id, auth.tenant_id)
    if not profile:
        return result
    return profile


# ─── Unified Profile ──────────────────────────────────────────────────────────

@router.get("/profile")
async def get_profile_endpoint(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """
    THE unified profile endpoint.

    Returns everything about this user: identity, preferences,
    projects, collections, active cases, signal stats, unseen count.

    This is what the frontend loads on login to know WHO the user is
    and WHAT they should see.
    """
    profile = get_full_profile(auth.subject_id, auth.tenant_id)
    if not profile:
        return JSONResponse(status_code=404, content={"error": "PROFILE.NOT_FOUND"})
    return profile
