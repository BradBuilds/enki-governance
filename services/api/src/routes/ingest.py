"""
Ingestion endpoint: POST /ingest/events with unified auth + tier enforcement.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from storage.ingest.ingestion import ingest_jsonl

from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from services.api.src.deps import raise_enki_error

router = APIRouter(prefix="/ingest", tags=["ingestion"])


def _enforce_tier_w_connector_policy(*, device_id: str, events: list[dict[str, Any]]) -> None:
    """
    Tier W policy enforcement is route-level.

    This skeleton tries to use storage.connectors.registry.get_connector_config()
    if present. If your repo doesn't include that module yet, we fail loudly with
    an explicit error code rather than silently skipping enforcement.
    """
    try:
        from storage.connectors.registry import get_connector_config  # type: ignore
    except Exception:
        raise_enki_error(
            "CONNECTOR.MODULE_MISSING",
            "Tier W enforcement requires storage.connectors.registry.get_connector_config()",
        )

    connector = get_connector_config(device_id)

    if getattr(connector, "state", None) not in ["VERIFIED_RAW", "ACTIVE_PROJECTION"]:
        raise_enki_error(
            "CONNECTOR.STATE_FORBIDS_WRITE",
            f"Connector state {getattr(connector, 'state', None)} does not allow writes",
        )

    allowed = set(getattr(connector, "allowed_event_types", []) or [])

    for ev in events:
        event_type = ev.get("event_type")
        if not event_type:
            continue

        if event_type.startswith("assertion."):
            raise_enki_error(
                "CONNECTOR.EVENT_NOT_ALLOWED",
                "Tier W devices cannot submit assertions",
            )

        if allowed and event_type not in allowed:
            raise_enki_error(
                "CONNECTOR.EVENT_NOT_ALLOWED",
                f"Event type {event_type} not in connector allowlist",
            )


@router.post("/events")
async def ingest_events(
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    body = await request.body()

    # Parse JSONL
    jsonl_lines = body.decode("utf-8").splitlines()

    # Tier W enforcement (device-only, route-level policy)
    if auth.device_tier == "W":
        events = [json.loads(line) for line in jsonl_lines if line.strip()]
        _enforce_tier_w_connector_policy(device_id=auth.subject_id, events=events)

    # Ingestion
    receipt = ingest_jsonl(jsonl_lines, auth.tenant_id)
    return JSONResponse(receipt)
