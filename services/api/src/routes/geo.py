"""
Geospatial signal query + device ping endpoints.

GET  /signals/geo          — GeoJSON FeatureCollection (bbox or radius)
POST /signals/geo/ping     — Simple GPS ping from any device
GET  /signals/geo/clusters — Aggregated clusters for map zoom levels
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Body, Depends, Header, Query
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

from storage.signals.geo import (
    query_signals_bbox, query_signals_radius, signals_to_geojson,
)
from storage.ingest.signal_ingestion import ingest_signal
from storage.signals.detection import run_detection

router = APIRouter(prefix="/signals/geo", tags=["geo"])


@router.get("")
async def query_geo_signals(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    west: Optional[float] = Query(None, description="Bounding box west longitude"),
    south: Optional[float] = Query(None, description="Bounding box south latitude"),
    east: Optional[float] = Query(None, description="Bounding box east longitude"),
    north: Optional[float] = Query(None, description="Bounding box north latitude"),
    lat: Optional[float] = Query(None, description="Center latitude for radius query"),
    lon: Optional[float] = Query(None, description="Center longitude for radius query"),
    radius_km: Optional[float] = Query(None, description="Radius in kilometers"),
    signal_type: Optional[str] = Query(None),
    after: Optional[str] = Query(None),
    before: Optional[str] = Query(None),
    limit: int = Query(500, le=2000),
):
    """
    Query geo-tagged signals as GeoJSON.

    Use bbox params (west, south, east, north) OR radius params (lat, lon, radius_km).
    If neither provided, returns all geo-tagged signals up to limit.
    """
    after_dt = _parse_dt(after)
    before_dt = _parse_dt(before)

    if lat is not None and lon is not None and radius_km is not None:
        signals = query_signals_radius(
            tenant_id=auth.tenant_id, lat=lat, lon=lon, radius_km=radius_km,
            signal_type=signal_type, after=after_dt, before=before_dt, limit=limit,
        )
    elif west is not None and south is not None and east is not None and north is not None:
        signals = query_signals_bbox(
            tenant_id=auth.tenant_id, west=west, south=south, east=east, north=north,
            signal_type=signal_type, after=after_dt, before=before_dt, limit=limit,
        )
    else:
        # No spatial filter — return all geo-tagged signals (bbox = world)
        signals = query_signals_bbox(
            tenant_id=auth.tenant_id, west=-180, south=-90, east=180, north=90,
            signal_type=signal_type, after=after_dt, before=before_dt, limit=limit,
        )

    geojson = signals_to_geojson(signals)
    return geojson


@router.post("/ping")
async def gps_ping(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    x_device_id: str = Header("unknown", alias="X-Device-ID"),
    body: dict = Body(...),
):
    """
    Simple GPS ping endpoint. Any device can POST its location.

    Body: { "lat": 38.89, "lon": -77.03 }
    Optional: altitude, speed, bearing, accuracy, label

    Creates a signal.geospatial.position_report and runs it through
    the full ingestion pipeline (chain-hashed, detection-checked).
    """
    lat = body.get("lat") or body.get("latitude")
    lon = body.get("lon") or body.get("longitude") or body.get("lng")
    if lat is None or lon is None:
        return JSONResponse({"error": "lat and lon required"}, status_code=400)

    payload = {
        "latitude": float(lat),
        "longitude": float(lon),
        "timestamp": body.get("timestamp") or datetime.now(timezone.utc).isoformat(),
        "signal_type": "signal.geospatial.position_report",
    }
    for field in ("altitude", "speed", "bearing", "accuracy", "label"):
        if body.get(field) is not None:
            payload[field] = body[field]

    raw_bytes = json.dumps(payload).encode("utf-8")
    result = ingest_signal(
        raw_bytes=raw_bytes,
        device_id=x_device_id,
        tenant_id=auth.tenant_id,
        content_type="application/json",
    )

    # Run detection engine (geofence checks, rule matching)
    if result.signal_id:
        try:
            run_detection(
                signal={
                    "signal_id": result.signal_id,
                    "signal_type": payload.get("signal_type", ""),
                    "device_id": x_device_id,
                    "latitude": payload["latitude"],
                    "longitude": payload["longitude"],
                },
                tenant_id=auth.tenant_id,
            )
        except Exception as e:
            # Detection failure should not block ingestion
            pass

    return {
        "status": "ok",
        "signal_id": result.signal_id,
        "path": result.path,
        "lat": payload["latitude"],
        "lon": payload["longitude"],
    }


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None
