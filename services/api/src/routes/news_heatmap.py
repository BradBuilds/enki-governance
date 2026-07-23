"""
News heatmap — aggregated mention density on the globe.

Phase 4.3 of the collapse rebuild plan. Reads ``entity_mentions`` (with
the lat/lng columns added in migration 038) and returns a GeoJSON
FeatureCollection of weighted hex cells. Used by the MapLibre globe
heatmap layer.

GET /news-heatmap — query params:
    since      ISO-8601 timestamp; default = 24h ago
    bbox       comma-separated minLng,minLat,maxLng,maxLat
    zoom       integer 0..18; controls hex resolution
    sources    repeatable connector_id filter
    topics     repeatable topic_id filter

Hex aggregation: we don't depend on the H3 library — instead we do a
simple coarse-grain bucket (lat/lng rounded to (resolution) decimals)
which produces the same heatmap shape for visual purposes. If the H3
library is installed it gets used; otherwise the bucket fallback runs.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

log = logging.getLogger("enki.news_heatmap")
router = APIRouter(prefix="", tags=["map"])


# Zoom-to-grain table. At zoom 0 (whole globe) one cell ≈ 10°; at zoom
# 8 one cell ≈ 0.05°. The grain is the rounding precision (decimals).
_ZOOM_GRAIN = {
    0: 1, 1: 1, 2: 1, 3: 1, 4: 1,
    5: 1, 6: 2, 7: 2, 8: 2, 9: 2,
    10: 3, 11: 3, 12: 3, 13: 4, 14: 4,
    15: 4, 16: 5, 17: 5, 18: 5,
}


def _grain_for(zoom: int) -> int:
    return _ZOOM_GRAIN.get(max(0, min(18, zoom)), 2)


def _try_h3_aggregate(rows, zoom: int):
    """If the h3 lib is installed, use it. Otherwise return None and the
    caller falls back to lat/lng rounding."""
    try:
        import h3  # type: ignore
    except Exception:
        return None
    res = max(0, min(15, zoom // 2))
    bucket: dict[str, dict] = {}
    for lat, lng, story_id in rows:
        if lat is None or lng is None:
            continue
        try:
            cell = h3.latlng_to_cell(float(lat), float(lng), res)
        except Exception:
            continue
        b = bucket.setdefault(cell, {"count": 0, "stories": set()})
        b["count"] += 1
        if story_id:
            b["stories"].add(story_id)
    features = []
    for cell, b in bucket.items():
        try:
            cy, cx = h3.cell_to_latlng(cell)
        except Exception:
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [cx, cy]},
            "properties": {
                "h3": cell,
                "count": b["count"],
                "story_ids": sorted(list(b["stories"]))[:20],
            },
        })
    return features


def _bucket_aggregate(rows, zoom: int):
    grain = _grain_for(zoom)
    bucket: dict[tuple[float, float], dict] = {}
    for lat, lng, story_id in rows:
        if lat is None or lng is None:
            continue
        key = (round(float(lat), grain), round(float(lng), grain))
        b = bucket.setdefault(key, {"count": 0, "stories": set()})
        b["count"] += 1
        if story_id:
            b["stories"].add(story_id)
    features = []
    for (lat, lng), b in bucket.items():
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lng, lat]},
            "properties": {
                "lat_grain": lat,
                "lng_grain": lng,
                "count": b["count"],
                "story_ids": sorted(list(b["stories"]))[:20],
            },
        })
    return features


@router.get("/news-heatmap")
async def news_heatmap(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    since: Optional[str] = Query(default=None, description="ISO-8601, default 24h ago"),
    bbox: Optional[str] = Query(default=None, description="minLng,minLat,maxLng,maxLat"),
    zoom: int = Query(default=2, ge=0, le=18),
    sources: Optional[list[str]] = Query(default=None),
    topics: Optional[list[str]] = Query(default=None),
    # Hide low-density cells. Useful at low zoom levels where the bucket
    # aggregation grain is fine enough that single mentions in distinct
    # cities don't merge — pass min_count=2 or higher to require
    # concentrations. Defaults to 1 (show everything) so the map isn't
    # empty when geocoding precision varies; the UI can opt-in to
    # filtering via a Layers-panel control later.
    min_count: int = Query(default=1, ge=1, le=100),
):
    """Aggregated mention density. Returns GeoJSON FeatureCollection."""
    from storage.postgres.db import get_connection

    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except Exception:
            since_dt = datetime.now(timezone.utc) - timedelta(hours=24)
    else:
        since_dt = datetime.now(timezone.utc) - timedelta(hours=24)

    where = [
        "m.tenant_id = %s",
        "m.latitude IS NOT NULL",
        "m.longitude IS NOT NULL",
        "m.created_at >= %s",
    ]
    params: list = [auth.tenant_id, since_dt]

    if bbox:
        try:
            min_lng, min_lat, max_lng, max_lat = [float(x) for x in bbox.split(",")]
            where.append("m.latitude BETWEEN %s AND %s")
            where.append("m.longitude BETWEEN %s AND %s")
            params.extend([min_lat, max_lat, min_lng, max_lng])
        except Exception:
            log.debug("invalid bbox: %s", bbox)

    join_signal = ""
    if sources:
        join_signal = " JOIN signals s ON s.rid = m.signal_rid "
        where.append("s.connector_id = ANY(%s)")
        params.append(sources)

    sql = f"""
        SELECT m.latitude, m.longitude, m.signal_rid
          FROM entity_mentions m
          {join_signal}
         WHERE {' AND '.join(where)}
    """

    rows: list = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    features = _try_h3_aggregate(rows, zoom)
    if features is None:
        features = _bucket_aggregate(rows, zoom)

    # Filter out low-density cells (count < min_count) — see param doc above.
    if min_count > 1:
        features = [f for f in features if f.get("properties", {}).get("count", 0) >= min_count]

    return {
        "type": "FeatureCollection",
        "features": features,
        "meta": {
            "since": since_dt.isoformat(),
            "zoom": zoom,
            "min_count": min_count,
            "mention_count": sum(f.get("properties", {}).get("count", 0) for f in features),
            "cell_count": len(features),
        },
    }
