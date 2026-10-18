from __future__ import annotations

from fastapi import APIRouter, Header, Request, Query
from fastapi.responses import JSONResponse

from services.api.src.deps import authenticate_request, raise_enki_error, sha256_hex
from storage.postgres.queries import list_audit_events
from services.api.src.models import AuditEventsResponse, ErrorEnvelope
from services.api.src.config import get_api_default_limit, get_api_max_limit

router = APIRouter(prefix="/audit-events", tags=["read"])


@router.get(
    "",
    response_model=AuditEventsResponse,
    responses={
        400: {
            "model": ErrorEnvelope,
            "description": "Bad Request",
            "content": {
                "application/json": {
                    "example": {"error_code": "API.BAD_REQUEST", "detail": "Invalid tenant_id format"}
                }
            },
        },
        401: {
            "model": ErrorEnvelope,
            "description": "Unauthorized",
            "content": {
                "application/json": {
                    "example": {"error_code": "AUTH.SIGNATURE_INVALID", "detail": "Authentication failed"}
                }
            },
        },
        403: {
            "model": ErrorEnvelope,
            "description": "Forbidden",
            "content": {
                "application/json": {
                    "example": {"error_code": "POLICY.DENIED", "detail": "Access denied"}
                }
            },
        },
    },
)
async def get_audit_events(
    request: Request,
    limit: int = Query(100, ge=1),
    cursor: str | None = Query(None),
    x_device_id: str = Header(..., alias="X-Device-ID"),
    x_challenge_id: str = Header(..., alias="X-Challenge-ID"),
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_body_sha256: str = Header(..., alias="X-Body-SHA256"),
    x_signature: str = Header(..., alias="X-Signature"),
):
    """
    List audit events for a tenant with pagination.

    Uses integer cursor pagination backed by audit_events.rid.
    """
    body = b""
    computed = sha256_hex(body)
    if computed != x_body_sha256:
        raise_enki_error("INGEST.BODY_HASH_MISMATCH", "Body hash mismatch (GET body must be empty)")

    grant_store = request.app.state.grant_store  # type: ignore[attr-defined]
    challenge_store = request.app.state.challenge_store  # type: ignore[attr-defined]
    root_public_key_raw = request.app.state.root_public_key_raw  # type: ignore[attr-defined]

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

    # Guardrails: enforce pagination caps
    default_limit = get_api_default_limit()
    max_limit = get_api_max_limit()
    if limit is None:
        limit = default_limit
    if limit > max_limit:
        raise_enki_error("API.BAD_REQUEST", f"limit exceeds max ({max_limit})")

    # Validate cursor format deterministically
    cursor_int = None
    if cursor is not None:
        try:
            cursor_int = int(cursor)
            if cursor_int < 0:
                raise ValueError()
        except Exception:
            raise_enki_error("API.BAD_REQUEST", "Invalid cursor format")

    from os import getenv
    dsn = getenv("ENKI_DB_DSN")
    if not dsn:
        raise_enki_error("API.BAD_REQUEST", "Database DSN not configured")

    result = list_audit_events(x_tenant_id, dsn, limit=limit, cursor=cursor_int)
    return JSONResponse(content=result)
