"""
Webhook receivers for external systems.

POST /webhooks/traccar          — Receive Traccar position events
POST /webhooks/generic/{name}   — Generic receiver for any device (any format)
"""

from __future__ import annotations

import json
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from storage.ingest.signal_ingestion import ingest_signal

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/generic/{device_name}")
async def generic_webhook(
    device_name: str,
    request: Request,
    x_tenant_id: str = Header("default", alias="X-Tenant-ID"),
):
    """
    Generic webhook receiver — accepts ANY format from ANY device.

    Configure your device to POST to:
        https://example.com/webhooks/generic/my-device-name

    The pipeline auto-detects format (JSON, XML, images, binary)
    and classifies the signal type automatically.
    """
    body = await request.body()
    if not body:
        return JSONResponse(status_code=400, content={
            "error": "WEBHOOK.EMPTY_BODY", "detail": "No payload received"
        })

    content_type = request.headers.get("content-type", "application/octet-stream")
    device_id = f"webhook_{device_name}"

    result = ingest_signal(
        raw_bytes=body,
        device_id=device_id,
        tenant_id=x_tenant_id,
        content_type=content_type,
        source_type="webhook",
    )

    return JSONResponse(
        status_code=201,
        content={
            "status": "accepted" if result.path in ("ldu_w", "ldu_w_fast", "duplicate") else
                      "provisioned" if result.path == "ldu_p" else "stored",
            "path": result.path,
            "signal_id": result.signal_id,
            "signal_type": result.signal_type,
            "device_id": device_id,
        },
    )


@router.post("/traccar")
async def traccar_webhook(
    request: Request,
    x_tenant_id: str = Header("default", alias="X-Tenant-ID"),
):
    """
    Receive Traccar position/event webhooks.

    Traccar sends JSON with structure:
      {event: {...}, device: {...}, position: {id, deviceId, latitude, longitude,
       speed, course, altitude, fixTime, serverTime, attributes: {battery, ...}}}

    Or just a position object directly (depending on Traccar config).
    """
    body = await request.body()

    if not body:
        return JSONResponse(status_code=400, content={
            "error": "WEBHOOK.EMPTY_BODY", "detail": "No payload received"
        })

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={
            "error": "WEBHOOK.INVALID_JSON", "detail": "Body is not valid JSON"
        })

    # Extract position from Traccar's wrapper or use raw object
    position = data.get("position", data)

    if not isinstance(position, dict):
        return JSONResponse(status_code=400, content={
            "error": "WEBHOOK.INVALID_FORMAT", "detail": "Expected position object"
        })

    # Build device_id from Traccar's deviceId
    traccar_device_id = position.get("deviceId", data.get("device", {}).get("id", "unknown"))
    device_id = f"traccar_{traccar_device_id}"

    # Map Traccar fields to Enki signal format
    mapped = {
        "latitude": position.get("latitude"),
        "longitude": position.get("longitude"),
        "altitude": position.get("altitude"),
        "speed": position.get("speed"),
        "course": position.get("course"),
        "timestamp": position.get("fixTime") or position.get("serverTime"),
        "accuracy": position.get("accuracy"),
    }

    # Merge attributes (battery, ignition, motion, etc.)
    attributes = position.get("attributes", {})
    if isinstance(attributes, dict):
        mapped["attributes"] = attributes
        # Promote common attributes to top level
        for key in ("battery", "batteryLevel", "ignition", "motion", "alarm"):
            if key in attributes:
                mapped[key] = attributes[key]

    # Include device info if present
    device_info = data.get("device", {})
    if isinstance(device_info, dict) and device_info.get("name"):
        mapped["device_name"] = device_info["name"]

    result = ingest_signal(
        raw_bytes=json.dumps(mapped, default=str).encode("utf-8"),
        device_id=device_id,
        tenant_id=x_tenant_id,
        content_type="application/json",
        source_type="traccar",
    )

    return JSONResponse(
        status_code=201,
        content={
            "status": "accepted" if result.path in ("ldu_w", "ldu_w_fast", "duplicate") else
                      "provisioned" if result.path == "ldu_p" else "stored",
            "path": result.path,
            "signal_id": result.signal_id,
            "signal_type": result.signal_type,
            "device_id": device_id,
        },
    )
