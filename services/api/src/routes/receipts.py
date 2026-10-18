from __future__ import annotations

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from services.api.src.deps import authenticate_request, raise_enki_error, sha256_hex
from storage.postgres.queries import get_receipt
from storage.postgres.db import get_connection
from services.api.src.models import Receipt, ErrorEnvelope

router = APIRouter(prefix="/receipts", tags=["read"])


@router.get(
    "/{txid}",
    response_model=Receipt,
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
        404: {
            "model": ErrorEnvelope,
            "description": "Not Found",
            "content": {
                "application/json": {
                    "example": {"error_code": "QUERY.RECEIPT_NOT_FOUND", "detail": "Receipt not found"}
                }
            },
        },
    },
)
async def get_receipt_by_txid(
    request: Request,
    txid: str,
    x_device_id: str = Header(..., alias="X-Device-ID"),
    x_challenge_id: str = Header(..., alias="X-Challenge-ID"),
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_body_sha256: str = Header(..., alias="X-Body-SHA256"),
    x_signature: str = Header(..., alias="X-Signature"),
):
    """
    Resolve a transaction receipt by txid.

    Governance: txid := receipt.rid (Base-60 encoded).
    GET requests must sign an empty body (sha256("")).
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

    dsn = request.app.state.db_dsn if hasattr(request.app.state, "db_dsn") else None  # type: ignore[attr-defined]
    if dsn is None:
        # Fallback to environment-based connection inside storage functions
        try:
            from os import getenv
            dsn_env = getenv("ENKI_DB_DSN")
            if not dsn_env:
                raise_enki_error("API.BAD_REQUEST", "Database DSN not configured")
            dsn = dsn_env
        except Exception:
            raise_enki_error("API.BAD_REQUEST", "Database DSN not configured")

    receipt = get_receipt(x_tenant_id, txid, dsn)
    if receipt is None:
        raise_enki_error("QUERY.RECEIPT_NOT_FOUND", "Receipt not found")

    return JSONResponse(content=receipt)
