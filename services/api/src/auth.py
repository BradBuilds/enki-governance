"""
Logto JWT validation for the Enki API.

Validates access tokens issued by Logto using JWKS (RS256).
Extracts user identity and organization (tenant) from claims.

Usage:
    from services.api.src.auth import require_auth, AuthInfo

    @app.get("/protected")
    async def endpoint(auth: AuthInfo = Depends(require_auth)):
        print(auth.sub, auth.tenant_id)
"""

from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field
from typing import Optional

import jwt
from jwt import PyJWKClient
from fastapi import HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

log = logging.getLogger("enki.auth")

LOGTO_ENDPOINT = os.getenv("LOGTO_ENDPOINT", "http://localhost:3001")
JWKS_URI = f"{LOGTO_ENDPOINT}/oidc/jwks"
ISSUER = f"{LOGTO_ENDPOINT}/oidc"
LOGTO_APP_ID = os.getenv("LOGTO_APP_ID", "")

# Auth bypass is not supported. The active profile (ENKI_PROFILE) decides
# whether this Logto path is even attempted — see unified_auth.py.
# This module always validates JWTs when called.

if not LOGTO_APP_ID:
    log.warning(
        "LOGTO_APP_ID not set — JWT audience verification DISABLED. "
        "Set LOGTO_APP_ID to the Logto application ID to enforce audience checks."
    )

_jwks_client: Optional[PyJWKClient] = None
_bearer = HTTPBearer(auto_error=False)


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(JWKS_URI)
    return _jwks_client


@dataclass
class AuthInfo:
    """Decoded identity from a validated JWT."""
    sub: str
    tenant_id: str
    email: Optional[str] = None
    name: Optional[str] = None
    organization_id: Optional[str] = None
    scopes: list[str] = field(default_factory=list)


def _validate_token(token: str) -> dict:
    try:
        client = _get_jwks_client()
        signing_key = client.get_signing_key_from_jwt(token)

        decode_kwargs: dict = {
            "algorithms": ["RS256"],
            "issuer": ISSUER,
        }

        if LOGTO_APP_ID:
            decode_kwargs["audience"] = LOGTO_APP_ID
            decode_kwargs["options"] = {"verify_aud": True}
        else:
            decode_kwargs["options"] = {"verify_aud": False}

        payload = jwt.decode(
            token,
            signing_key.key,
            **decode_kwargs,
        )
        return payload
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")


def _extract_auth_info(payload: dict, fallback_tenant: str) -> AuthInfo:
    scopes = payload.get("scope", "").split() if payload.get("scope") else []
    org_id = payload.get("organization_id")
    tenant = org_id or fallback_tenant

    return AuthInfo(
        sub=payload.get("sub", ""),
        tenant_id=tenant,
        email=payload.get("email"),
        name=payload.get("name"),
        organization_id=org_id,
        scopes=scopes,
    )


async def require_auth(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> AuthInfo:
    """FastAPI dependency: validate JWT and return AuthInfo.

    Always enforces authentication. Dev mode bypass is handled by
    unified_auth.py (ENKI_PROFILE), not here.
    """
    fallback_tenant = request.headers.get("x-tenant-id", "default")

    if not credentials:
        raise HTTPException(status_code=401, detail="Missing authorization token")

    payload = _validate_token(credentials.credentials)
    return _extract_auth_info(payload, fallback_tenant)


async def optional_auth(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> Optional[AuthInfo]:
    """Like require_auth but returns None instead of 401 for public endpoints."""
    if not credentials:
        return None

    try:
        payload = _validate_token(credentials.credentials)
        fallback_tenant = request.headers.get("x-tenant-id", "default")
        return _extract_auth_info(payload, fallback_tenant)
    except HTTPException:
        return None
