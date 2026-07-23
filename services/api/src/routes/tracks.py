"""
Track management endpoints.

GET  /tracks                     — List assembled tracks
GET  /tracks/{entity_id}/history — Time-ordered positions for an entity
POST /tracks/assemble            — Assemble track from position signals
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

from storage.signals.geo import list_tracks, get_entity_positions, assemble_track
from storage.signals.analysis import analyze_device_track

router = APIRouter(prefix="/tracks", tags=["tracks"])


@router.get("")
async def get_tracks(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    limit: int = Query(100, le=500),
):
    """List all assembled tracks for this tenant."""
    tracks = list_tracks(tenant_id=auth.tenant_id, limit=limit)
    return {"tracks": tracks, "count": len(tracks)}


@router.get("/{entity_id}/history")
async def get_entity_history(
    entity_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
    after: Optional[str] = Query(None),
    before: Optional[str] = Query(None),
):
    """Get time-ordered positions for a device/entity."""
    after_dt = None
    before_dt = None
    if after:
        try:
            after_dt = datetime.fromisoformat(after)
        except ValueError:
            return JSONResponse(status_code=400, content={
                "error": "TRACK.INVALID_DATE", "detail": f"Invalid 'after': {after}"
            })
    if before:
        try:
            before_dt = datetime.fromisoformat(before)
        except ValueError:
            return JSONResponse(status_code=400, content={
                "error": "TRACK.INVALID_DATE", "detail": f"Invalid 'before': {before}"
            })

    positions = get_entity_positions(
        tenant_id=auth.tenant_id, entity_id=entity_id,
        after=after_dt, before=before_dt,
    )
    return {"entity_id": entity_id, "positions": positions, "count": len(positions)}


@router.post("/assemble")
async def assemble_entity_track(
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """
    Assemble a track from position signals for a device.

    Body: {
        "entity_id": "traccar_1",
        "start_time": "2026-03-24T00:00:00Z",
        "end_time": "2026-03-24T23:59:59Z",
        "entity_type": "device",
        "display_name": "My Tracker"
    }
    """
    body = await request.json()

    entity_id = body.get("entity_id")
    if not entity_id:
        return JSONResponse(status_code=400, content={
            "error": "TRACK.MISSING_ENTITY", "detail": "entity_id is required"
        })

    try:
        start_time = datetime.fromisoformat(body["start_time"])
        end_time = datetime.fromisoformat(body["end_time"])
    except (KeyError, ValueError) as e:
        return JSONResponse(status_code=400, content={
            "error": "TRACK.INVALID_TIME", "detail": f"start_time and end_time required: {e}"
        })

    result = assemble_track(
        tenant_id=auth.tenant_id,
        entity_id=entity_id,
        start_time=start_time,
        end_time=end_time,
        entity_type=body.get("entity_type", "device"),
        display_name=body.get("display_name"),
    )

    if not result:
        return JSONResponse(status_code=404, content={
            "error": "TRACK.NO_POSITIONS",
            "detail": f"No positions found for {entity_id} in time range (need at least 2)",
        })

    return JSONResponse(status_code=201, content=result)


@router.get("/{device_id}/analyze")
async def analyze_track(
    device_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
    after: Optional[str] = Query(None),
    before: Optional[str] = Query(None),
    stop_threshold: float = Query(5.0, description="Minutes to count as a stop"),
    stop_radius: float = Query(50.0, description="Meters radius for stop detection"),
):
    """
    Analyze a device's movement pattern — route, stops, speed, stats.

    Returns structured data ready for map visualization:
    route polyline, stop markers, segment analysis, summary stats.
    """
    after_dt = None
    before_dt = None
    if after:
        try: after_dt = datetime.fromisoformat(after)
        except ValueError: pass
    if before:
        try: before_dt = datetime.fromisoformat(before)
        except ValueError: pass

    result = analyze_device_track(
        tenant_id=auth.tenant_id,
        device_id=device_id,
        after=after_dt,
        before=before_dt,
        stop_threshold_minutes=stop_threshold,
        stop_radius_meters=stop_radius,
    )

    return result
