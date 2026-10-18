"""
FastAPI dependency injection for Phase 4 API.

Provides auth singletons and request authentication helpers.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from typing import Any, Optional

from fastapi import Depends, Header, HTTPException, Request
import re

from storage.auth.authenticate import authenticate_device, AuthResult
from storage.auth.grant import DeviceGrant
from storage.auth.grant_store import GrantStore
from storage.auth.challenge_store import InMemoryChallengeStore
from services.api.src.error_codes import get_http_status, UnknownApiErrorCode


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _error_http_status(code: str) -> int:
    """
    Map ENKI error codes to HTTP status codes.
    
    Uses canonical registry for API.* codes.
    Special handling for AUTH.* codes (storage.auth module).
    Falls back to 403 for policy/denial codes.
    
    Raises UnknownApiErrorCode if an API.* code is not registered.
    This is always an error condition in production.
    """
    if code.startswith("AUTH."):
        return 401
    
    # Try to get status from canonical API registry
    try:
        return get_http_status(code)
    except UnknownApiErrorCode:
        # API.* codes must be registered - if not, it's a developer error
        if code.startswith("API."):
            # Re-raise: let it fail hard with 500 to catch bugs
            raise
        # Other code namespaces (POLICY.*, TENANT.*, INGEST.*) fall back to 403
        return 403


def raise_enki_error(code: str, detail: str = "Request rejected") -> None:
    """Raise HTTP exception with ENKI error envelope.
    
    Converts UnknownApiErrorCode to deterministic 500 envelope
    for operational consistency.
    """
    try:
        status_code = _error_http_status(code)
    except UnknownApiErrorCode as e:
        # Developer error: API code not registered
        # Return deterministic 500 envelope for ops visibility
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "SERVER.MISCONFIGURATION",
                "detail": f"Unregistered API error code: {e.code}. Contact engineering.",
            },
        ) from e
    
    raise HTTPException(
        status_code=status_code,
        detail={"error_code": code, "detail": detail},
    )


def get_grant_store(request: Request) -> GrantStore:
    return request.app.state.grant_store  # type: ignore[attr-defined]


def get_challenge_store(request: Request) -> InMemoryChallengeStore:
    return request.app.state.challenge_store  # type: ignore[attr-defined]


def get_root_public_key_raw(request: Request) -> bytes:
    return request.app.state.root_public_key_raw  # type: ignore[attr-defined]


def get_root_key(request: Request):
    return request.app.state.root_key  # type: ignore[attr-defined]


def load_grant(
    *,
    grant_store: GrantStore,
    device_id: str,
) -> Optional[DeviceGrant]:
    return grant_store.load(device_id)


def authenticate_request(
    *,
    request: Request,
    body: bytes,
    x_device_id: str,
    x_challenge_id: str,
    x_tenant_id: str,
    x_body_sha256: str,
    x_signature: str,
    grant_store: GrantStore,
    challenge_store: InMemoryChallengeStore,
    root_public_key_raw: bytes,
) -> AuthResult:
    # Strict tenant_id boundary validation (namespaced, lowercase, digits, hyphens)
    TENANT_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:\.[a-z0-9]+(?:-[a-z0-9]+)*)*$")
    if not TENANT_ID_PATTERN.fullmatch(x_tenant_id):
        raise_enki_error("API.BAD_REQUEST", "Invalid tenant_id format")

    # Load grant from persistent store (Phase 3.6)
    grant = load_grant(grant_store=grant_store, device_id=x_device_id)

    # Authenticate via locked Phase 3.6 interface
    result = authenticate_device(
        device_id=x_device_id,
        challenge_id=x_challenge_id,
        tenant_id=x_tenant_id,
        method=request.method,
        path=(request.url.path if not request.url.query else f"{request.url.path}?{request.url.query}"),
        body=body,
        body_sha256_header=x_body_sha256,
        signature_b64=x_signature,
        grant=grant,
        root_public_key_raw=root_public_key_raw,
        challenge_store=challenge_store,
    )

    return result
