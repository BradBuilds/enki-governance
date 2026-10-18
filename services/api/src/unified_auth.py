"""
Unified authentication for ENKI Command Center.

Driven by a single env var: ENKI_PROFILE (one of dev / cloud / hardware /
hardened / hub).

The profile decides which auth paths are allowed at request time:

  Profile     Device   Logto JWT   Local JWT   Dev fallback   Hub admin
  ─────────   ──────   ─────────   ─────────   ────────────   ─────────
  dev          ✓ (any)   ✗            ✗           ✓               ✗
  cloud        ✓          ✓           ✗           ✗               ✗
  hardware     ✓          ✗           ✓           ✗               ✗
  hardened     ✓          ✓           ✗           ✗               ✗
  hub          ✓ (peer)   ✗            ✗           ✗               ✓

Resolution order on every request:
  1. X-Device-* headers present  → device auth (DeviceGrant chain verified)
  2. Authorization: Bearer ...   → profile-appropriate token validator
  3. Cookie session              → local JWT (hardware profile only)
  4. Dev profile only            → header-based fallback identity
  5. Otherwise                   → 401

There are exactly 3 paths: device, bearer, dev-fallback. Which path is
even attempted is decided by the profile spec — no per-mode `if` chains.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from fastapi import Request, HTTPException

from core.profile import Profile, resolve_profile, get_spec

log = logging.getLogger("enki.auth.unified")


class SubjectType(str, Enum):
    DEVICE = "device"
    USER = "user"
    DEV = "dev"
    HUB_ADMIN = "hub_admin"
    PEER = "peer"                            # federation member (Ed25519-authenticated node)


@dataclass
class UnifiedAuthInfo:
    """Identity context produced by unified auth. Every protected route
    receives this regardless of which auth path was taken."""

    subject_id: str                          # device_id, user sub, "dev-user", or peer_node_id
    subject_type: SubjectType                # device | user | dev | hub_admin | peer
    tenant_id: str                           # resolved tenant

    # Device-specific (None for user/dev/hub)
    device_tier: Optional[str] = None        # K, P, W (R never seen at request time)
    device_scopes: Optional[list[str]] = None

    # User-specific (None for device/dev/hub)
    email: Optional[str] = None
    name: Optional[str] = None
    user_scopes: list[str] = field(default_factory=list)

    # Role within tenant (admin, analyst, viewer, member)
    role: str = "member"

    # Profile that authorized this request — useful for downstream policy
    profile: Optional[str] = None

    # Federation-peer specific (None for non-peer subjects)
    peer_roles: list[str] = field(default_factory=list)   # peer | library_owner | gdu_steward | federation_admin
    peer_trust_score: Optional[float] = None              # 0.0–1.0


# ── Path detectors ────────────────────────────────────────────────────────

def _has_device_headers(request: Request) -> bool:
    h = request.headers
    return bool(
        h.get("x-device-id")
        and h.get("x-challenge-id")
        and h.get("x-signature")
    )


def _has_bearer_token(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    return auth.lower().startswith("bearer ")


def _bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return auth.split(" ", 1)[1] if " " in auth else ""


# ── Path implementations ──────────────────────────────────────────────────

async def _authenticate_device(request: Request, profile: Profile) -> UnifiedAuthInfo:
    """Verify Ed25519 challenge-response against the configured grant store
    and trusted_keys. The grant chain is walked back to a Tier R pubkey."""
    from services.api.src.deps import authenticate_request, sha256_hex, raise_enki_error

    h = request.headers
    x_device_id = h.get("x-device-id", "")
    x_challenge_id = h.get("x-challenge-id", "")
    x_tenant_id = h.get("x-tenant-id", "")
    x_body_sha256 = h.get("x-body-sha256", "")
    x_signature = h.get("x-signature", "")

    body = await request.body()

    computed = sha256_hex(body)
    if computed != x_body_sha256:
        raise_enki_error("INGEST.BODY_HASH_MISMATCH", "Body hash mismatch")

    grant_store = request.app.state.grant_store
    challenge_store = request.app.state.challenge_store
    root_public_key_raw = request.app.state.root_public_key_raw

    auth = authenticate_request(
        request=request,
        body=body,
        x_device_id=x_device_id,
        x_challenge_id=x_challenge_id,
        x_tenant_id=x_tenant_id,
        x_body_sha256=x_body_sha256,
        x_signature=x_signature,
        grant_store=grant_store,
        challenge_store=challenge_store,
        root_public_key_raw=root_public_key_raw,
    )

    if not auth.success:
        raise_enki_error(auth.reason or "AUTH.VERIFICATION_ERROR", "Authentication failed")

    # Profile-level tier check: reject device tiers not allowed for this profile
    spec = get_spec(profile)
    if auth.device_tier and auth.device_tier not in spec.allowed_device_tiers:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Tier {auth.device_tier} not allowed in profile {profile.value}. "
                f"Allowed: {sorted(spec.allowed_device_tiers)}"
            ),
        )

    return UnifiedAuthInfo(
        subject_id=auth.device_id or x_device_id,
        subject_type=SubjectType.DEVICE,
        tenant_id=x_tenant_id,
        device_tier=auth.device_tier,
        device_scopes=auth.tenant_scopes,
        profile=profile.value,
    )


async def _authenticate_logto(request: Request, profile: Profile) -> UnifiedAuthInfo:
    """Logto OIDC JWT validation. Used by cloud and hardened profiles."""
    from services.api.src.auth import _validate_token, _extract_auth_info

    token = _bearer_token(request)
    payload = _validate_token(token)
    fallback_tenant = request.headers.get("x-tenant-id", "default")
    info = _extract_auth_info(payload, fallback_tenant)
    role = _resolve_user_role(info.sub, info.tenant_id)

    return UnifiedAuthInfo(
        subject_id=info.sub,
        subject_type=SubjectType.USER,
        tenant_id=info.tenant_id,
        email=info.email,
        name=info.name,
        user_scopes=info.scopes,
        role=role,
        profile=profile.value,
    )


def _authenticate_local_jwt(request: Request, profile: Profile) -> Optional[UnifiedAuthInfo]:
    """Local JWT validation (cookie or Bearer). Used by hardware profile.

    Returns None if no token; caller handles the 401."""
    from services.api.src.auth_local import validate_local_jwt

    token = request.cookies.get("enki_session")
    if not token and _has_bearer_token(request):
        token = _bearer_token(request)

    if not token:
        return None

    claims = validate_local_jwt(token)
    if not claims:
        return None

    return UnifiedAuthInfo(
        subject_id=claims["sub"],
        subject_type=SubjectType.USER,
        tenant_id=claims.get("tenant_id", "default"),
        role=claims.get("role", "member"),
        profile=profile.value,
    )


def _authenticate_peer(request: Request, profile: Profile) -> Optional[UnifiedAuthInfo]:
    """Verify a federation peer JWT (Ed25519-rooted, hub-issued).
    Returns None when no peer JWT present or verification fails;
    that lets the caller fall back to other auth paths in dual-mode."""
    from services.api.src.peer_auth import verify_jwt

    token = _bearer_token(request)
    if not token:
        return None

    claims = verify_jwt(token)
    if claims is None:
        return None

    # tenant: peers operate against the hub's logical tenant; cross-tenant
    # routing on peer-published content uses the row-level tenant_id, not the
    # request-level one.
    tenant = request.headers.get("x-tenant-id", "default")

    return UnifiedAuthInfo(
        subject_id=claims.peer_node_id,
        subject_type=SubjectType.PEER,
        tenant_id=tenant,
        role="admin" if "federation_admin" in claims.roles else "member",
        profile=profile.value,
        peer_roles=claims.roles,
        peer_trust_score=claims.trust_score,
    )


def _authenticate_hub_admin(request: Request, profile: Profile) -> Optional[UnifiedAuthInfo]:
    """Hub admin token validation (out-of-band issued). Used only by hub profile.

    The hub admin token is a static API key kept in /etc/enki/hub.conf,
    sent as Authorization: Bearer <token>. Hub has no Logto, no users —
    just one admin token for control-plane writes."""
    import os

    expected = os.environ.get("ENKI_HUB_ADMIN_TOKEN", "").strip()
    if not expected:
        return None

    presented = _bearer_token(request)
    if presented and presented == expected:
        return UnifiedAuthInfo(
            subject_id="hub-admin",
            subject_type=SubjectType.HUB_ADMIN,
            tenant_id="hub",
            role="admin",
            profile=profile.value,
        )
    return None


def _dev_fallback(request: Request, profile: Profile) -> UnifiedAuthInfo:
    """Header-based identity for DEV only. Never used in production profiles."""
    return UnifiedAuthInfo(
        subject_id=request.headers.get("x-identity-id", "dev-user"),
        subject_type=SubjectType.DEV,
        tenant_id=request.headers.get("x-tenant-id", "default"),
        profile=profile.value,
    )


# ── Main dispatcher ───────────────────────────────────────────────────────

async def unified_auth(request: Request) -> UnifiedAuthInfo:
    """FastAPI dependency: resolve identity per the active profile.

    Per-profile path matrix (which paths are even attempted):

       device   logto   local_jwt   hub_admin   dev_fallback
       ──────   ─────   ─────────   ─────────   ────────────
    dev    ✓     -          -           -            ✓
    cloud  ✓     ✓          -           -            -
    hw     ✓     -          ✓           -            -
    hard   ✓     ✓          -           -            -
    hub    ✓     -          -           ✓            -
    """
    profile, spec = resolve_profile()

    # Path 1: device auth — always allowed when device headers are present
    # AND the grant_store has been initialized on app.state.
    if _has_device_headers(request):
        if not hasattr(request.app.state, "grant_store"):
            raise HTTPException(
                status_code=503,
                detail="Device auth not initialized on this server",
            )
        return await _authenticate_device(request, profile)

    # Path 2: profile-specific bearer token
    if _has_bearer_token(request):
        # Try federation peer JWT first — works across all profiles, since
        # a peer might call a node running any profile. Only succeeds if the
        # token is a hub-issued peer JWT; otherwise falls through to the
        # profile-specific bearer handlers below.
        peer = _authenticate_peer(request, profile)
        if peer is not None:
            return peer

        if profile == Profile.CLOUD or profile == Profile.HARDENED:
            return await _authenticate_logto(request, profile)
        if profile == Profile.HARDWARE or profile == Profile.NODE_LOCAL:
            local = _authenticate_local_jwt(request, profile)
            if local is not None:
                return local
            raise HTTPException(status_code=401, detail="Invalid local session")
        if profile == Profile.HUB:
            admin = _authenticate_hub_admin(request, profile)
            if admin is not None:
                return admin
            raise HTTPException(status_code=401, detail="Invalid hub admin token")
        # DEV with bearer token: ignore the token, fall through to dev fallback
        # (don't honor random bearer tokens in dev mode)

    # Path 3: cookie-based local JWT (HARDWARE + NODE_LOCAL)
    if profile == Profile.HARDWARE or profile == Profile.NODE_LOCAL:
        local = _authenticate_local_jwt(request, profile)
        if local is not None:
            return local

    # Path 4: dev fallback — only when profile is DEV
    if profile == Profile.DEV:
        return _dev_fallback(request, profile)

    # No path matched
    raise HTTPException(
        status_code=401,
        detail=(
            f"Authentication required (profile={profile.value}). "
            f"Provide X-Device-* headers or a valid bearer token."
        ),
    )


async def optional_unified_auth(request: Request) -> Optional[UnifiedAuthInfo]:
    """Like unified_auth but returns None instead of 401."""
    try:
        return await unified_auth(request)
    except HTTPException:
        return None


# ── Role enforcement ──────────────────────────────────────────────────────

ROLE_HIERARCHY = {"admin": 3, "analyst": 2, "viewer": 1, "member": 0}


def require_role(*allowed_roles: str):
    """FastAPI dependency factory: require user has one of the specified roles.

    Tier K device auth is always treated as admin (the operator IS the admin).
    Hub admin token is always admin.
    DEV fallback is always 'member' — never admin, even if wished.
    """
    async def _check(request: Request) -> UnifiedAuthInfo:
        auth = await unified_auth(request)

        # Tier K device = admin
        if auth.subject_type == SubjectType.DEVICE and auth.device_tier == "K":
            auth.role = "admin"
            return auth

        # Hub admin = admin
        if auth.subject_type == SubjectType.HUB_ADMIN:
            auth.role = "admin"
            return auth

        # DEV fallback never escalates beyond member
        if auth.subject_type == SubjectType.DEV and auth.role not in allowed_roles:
            auth.role = "member"

        if auth.role not in allowed_roles:
            raise HTTPException(
                status_code=403,
                detail=f"Requires role: {', '.join(allowed_roles)}. You have: {auth.role}",
            )
        return auth

    return _check


def require_peer_role(*allowed_roles: str):
    """FastAPI dependency: require the caller is a federation peer with
    one of the specified roles.

    Allowed roles: 'peer', 'library_owner', 'gdu_steward', 'federation_admin'.
    Use 'peer' to mean 'any authenticated federation member'.
    """
    async def _check(request: Request) -> UnifiedAuthInfo:
        auth = await unified_auth(request)
        if auth.subject_type != SubjectType.PEER:
            raise HTTPException(
                status_code=403,
                detail=f"Federation peer JWT required (got {auth.subject_type.value})",
            )
        if "peer" in allowed_roles and auth.peer_roles:
            return auth  # any authenticated peer satisfies 'peer'
        if not any(r in auth.peer_roles for r in allowed_roles):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Federation role required: {', '.join(allowed_roles)}. "
                    f"You have: {auth.peer_roles}"
                ),
            )
        return auth

    return _check


# ── Helpers ───────────────────────────────────────────────────────────────

def _resolve_user_role(subject_id: str, tenant_id: str) -> str:
    """Resolve a Logto/local user's role from the identities table.

    Fail-closed: if the DB is unreachable, raise 503 rather than guess.
    """
    try:
        from storage.postgres.db import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT role FROM identities
                    WHERE tenant_id = %s
                      AND (identity_id = %s OR metadata->>'logto_sub' = %s)
                    LIMIT 1
                """, (tenant_id, subject_id, subject_id))
                r = cur.fetchone()
                return r[0] if r else "member"
    except Exception as e:
        log.error("Role resolution failed (DB unreachable): %s — denying access", e)
        raise HTTPException(
            status_code=503,
            detail="Unable to verify user role — database unavailable",
        )
