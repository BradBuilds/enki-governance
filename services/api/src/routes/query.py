"""
Query endpoint: GET /query/assertions with unified auth.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from services.api.src.deps import raise_enki_error
from storage.postgres.queries import list_audit_events
from services.api.src.models import AssertionsResponse, ErrorEnvelope
from services.api.src.config import get_api_default_limit, get_api_max_limit

router = APIRouter(prefix="/query", tags=["query"])


@router.get(
    "/assertions",
    response_model=AssertionsResponse,
    responses={
        400: {
            "model": ErrorEnvelope,
            "description": "Bad Request",
        },
        401: {
            "model": ErrorEnvelope,
            "description": "Unauthorized",
        },
        403: {
            "model": ErrorEnvelope,
            "description": "Forbidden",
        },
    },
)
async def query_assertions(
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
    q: str = Query("", description="Query string (placeholder)"),
    limit: int = Query(100, ge=1),
    cursor: str | None = Query(None),
):
    """Auth-wired query endpoint for assertion events."""
    default_limit = get_api_default_limit()
    max_limit = get_api_max_limit()
    if limit is None:
        limit = default_limit
    if limit > max_limit:
        raise_enki_error("API.BAD_REQUEST", f"limit exceeds max ({max_limit})")

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

    result = list_audit_events(auth.tenant_id, dsn, limit=limit, cursor=cursor_int)
    assertion_events = [
        e for e in result.get("events", [])
        if str(e.get("event_type", "")).startswith("assertion.")
    ]

    return JSONResponse(content={"events": assertion_events})
