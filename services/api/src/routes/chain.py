from __future__ import annotations

from fastapi import APIRouter, Header, Request, Query
from fastapi.responses import JSONResponse

from services.api.src.deps import authenticate_request, raise_enki_error, sha256_hex
from storage.postgres.queries import verify_chain
from services.api.src.models import ChainVerifyResponse, ErrorEnvelope

router = APIRouter(prefix="/chain", tags=["read"])


@router.get(
    "/verify",
    response_model=ChainVerifyResponse,
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
async def verify_chain_endpoint(
    request: Request,
    start_rid: str | None = Query(None, description="Optional start RID (Base-60)"),
    end_rid: str | None = Query(None, description="Optional end RID (Base-60)"),
    x_device_id: str = Header(..., alias="X-Device-ID"),
    x_challenge_id: str = Header(..., alias="X-Challenge-ID"),
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_body_sha256: str = Header(..., alias="X-Body-SHA256"),
    x_signature: str = Header(..., alias="X-Signature"),
):
    """
    Verify receipt chain integrity for a tenant.
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

    from os import getenv
    dsn = getenv("ENKI_DB_DSN")
    if not dsn:
        raise_enki_error("API.BAD_REQUEST", "Database DSN not configured")

    result = verify_chain(x_tenant_id, dsn, start_rid=start_rid, end_rid=end_rid)
    return JSONResponse(content=result)
