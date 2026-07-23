from __future__ import annotations

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response
from fastapi import HTTPException

from services.api.src.deps import authenticate_request, raise_enki_error, sha256_hex
from services.api.src.models import ArtifactMetadata, ErrorEnvelope
from storage.artifacts.store import save_artifact, read_artifact_metadata, read_artifact_content
from services.api.src.config import get_api_max_artifact_bytes

router = APIRouter(prefix="/artifacts", tags=["artifacts"])


@router.post(
    "",
    response_model=ArtifactMetadata,
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
async def upload_artifact(
    request: Request,
    x_device_id: str = Header(..., alias="X-Device-ID"),
    x_challenge_id: str = Header(..., alias="X-Challenge-ID"),
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_body_sha256: str = Header(..., alias="X-Body-SHA256"),
    x_signature: str = Header(..., alias="X-Signature"),
):
    """
    Upload raw bytes as artifact; returns CID + metadata.

    Accepts raw body bytes; Content-Type header used for metadata.
    """
    max_bytes = get_api_max_artifact_bytes()
    # Enforce via Content-Length if present
    cl = request.headers.get("Content-Length")
    if cl is not None:
        try:
            cl_int = int(cl)
            if cl_int > max_bytes:
                raise_enki_error("API.PAYLOAD_TOO_LARGE", f"Content length exceeds max ({max_bytes} bytes)")
        except ValueError:
            raise_enki_error("API.BAD_REQUEST", "Invalid Content-Length header")

    body = await request.body()
    if len(body) > max_bytes:
        raise_enki_error("API.PAYLOAD_TOO_LARGE", f"Body exceeds max ({max_bytes} bytes)")
    computed = sha256_hex(body)
    if computed != x_body_sha256:
        raise_enki_error("INGEST.BODY_HASH_MISMATCH", "Body hash mismatch (POST body must be signed)")

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

    content_type = request.headers.get("Content-Type")
    meta = save_artifact(x_tenant_id, body, content_type)
    return JSONResponse(content=meta)


@router.get(
    "/{cid}",
    response_model=ArtifactMetadata,
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
        404: {
            "model": ErrorEnvelope,
            "description": "Not Found",
            "content": {
                "application/json": {
                    "example": {"error_code": "QUERY.ARTIFACT_NOT_FOUND", "detail": "Artifact not found"}
                }
            },
        },
    },
)
async def get_artifact_metadata(
    request: Request,
    cid: str,
    x_device_id: str = Header(..., alias="X-Device-ID"),
    x_challenge_id: str = Header(..., alias="X-Challenge-ID"),
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_body_sha256: str = Header(..., alias="X-Body-SHA256"),
    x_signature: str = Header(..., alias="X-Signature"),
):
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

    meta = read_artifact_metadata(x_tenant_id, cid)
    if meta is None:
        raise HTTPException(status_code=404, detail={"error_code": "QUERY.ARTIFACT_NOT_FOUND", "detail": "Artifact not found"})

    return JSONResponse(content=meta)


@router.get(
    "/{cid}/content",
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
        404: {
            "model": ErrorEnvelope,
            "description": "Not Found",
            "content": {
                "application/json": {
                    "example": {"error_code": "QUERY.ARTIFACT_NOT_FOUND", "detail": "Artifact not found"}
                }
            },
        },
    },
)
async def get_artifact_content(
    request: Request,
    cid: str,
    x_device_id: str = Header(..., alias="X-Device-ID"),
    x_challenge_id: str = Header(..., alias="X-Challenge-ID"),
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_body_sha256: str = Header(..., alias="X-Body-SHA256"),
    x_signature: str = Header(..., alias="X-Signature"),
):
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

    meta = read_artifact_metadata(x_tenant_id, cid)
    content = read_artifact_content(x_tenant_id, cid)
    if meta is None or content is None:
        raise HTTPException(status_code=404, detail={"error_code": "QUERY.ARTIFACT_NOT_FOUND", "detail": "Artifact not found"})

    content_type = meta.get("content_type") or "application/octet-stream"
    return Response(content=content, media_type=content_type)
