"""
Feed Map — geographic view of intelligence signals.

GET /feed/map — Returns articles with lat/lon as GeoJSON for map rendering.
               Includes location-extracted articles from gazetteer when
               no coordinates are stored on the signal.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

log = logging.getLogger("enki.feed_map")
router = APIRouter(prefix="/feed", tags=["feed"])

# Signal types to exclude (noise)
_NOISE_TYPES = {
    "signal.geospatial.aircraft_position",
    "signal.geospatial.vessel_position",
    "signal.geospatial.position_report",
    "signal.device.sensor_reading",
    "signal.infrastructure.sensor_reading",
}


@router.get("/map")
async def get_feed_map(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    limit: int = Query(300, le=1000),
    after: Optional[str] = None,
):
    """Feed as GeoJSON — articles with geographic coordinates."""
    from storage.postgres.db import get_connection
    from core.extraction.gazetteer import extract_location

    now = datetime.now(timezone.utc)
    after_dt = datetime.fromisoformat(after) if after else now - timedelta(days=7)

    features = []
    regions: dict[str, dict] = defaultdict(lambda: {"count": 0, "articles": [], "lat": 0, "lon": 0})

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                noise_list = list(_NOISE_TYPES)
                placeholders = ",".join(["%s"] * len(noise_list))
                cur.execute(f"""
                    SELECT signal_id, signal_type, severity, device_id,
                           timestamp, payload, latitude, longitude
                    FROM signals
                    WHERE tenant_id = %s AND timestamp >= %s
                      AND signal_type NOT IN ({placeholders})
                    ORDER BY timestamp DESC
                    LIMIT %s
                """, (auth.tenant_id, after_dt, *noise_list, limit))

                for r in cur.fetchall():
                    sig_type = r[1]

                    payload = r[5] or {}
                    headline = payload.get("title") or payload.get("headline") or ""
                    if not headline or len(headline) < 15 or headline.startswith("Story #"):
                        continue

                    lat = r[6]
                    lon = r[7]
                    location_name = payload.get("location_name", "")
                    country = payload.get("country", "")

                    # If no coordinates, try gazetteer extraction
                    if not lat or not lon:
                        summary = payload.get("summary") or payload.get("og_description") or ""
                        loc = extract_location(headline, summary)
                        if loc:
                            lat = loc["latitude"]
                            lon = loc["longitude"]
                            location_name = loc["name"]
                            country = loc.get("type", "")

                    if not lat or not lon:
                        continue  # Skip articles with no geographic data

                    domain = sig_type.split(".")[1] if "." in sig_type else "unknown"

                    feature = {
                        "type": "Feature",
                        "geometry": {
                            "type": "Point",
                            "coordinates": [lon, lat],
                        },
                        "properties": {
                            "signal_id": r[0],
                            "headline": headline,
                            "source": payload.get("source", payload.get("feed_title", "")),
                            "source_url": payload.get("link") or payload.get("url") or "",
                            "thumbnail_url": payload.get("thumbnail_url", ""),
                            "timestamp": r[4].isoformat() if r[4] else None,
                            "severity": r[2],
                            "domain": domain,
                            "location_name": location_name,
                            "country": country,
                            "points": payload.get("points"),
                            "comments": payload.get("num_comments"),
                            "article_preview": {
                                "full_text": payload.get("full_text", ""),
                                "first_paragraph": payload.get("first_paragraph", ""),
                                "og_description": payload.get("og_description", ""),
                                "word_count": payload.get("word_count", 0),
                                "author": payload.get("author", ""),
                            },
                        },
                    }
                    features.append(feature)

                    # Aggregate into regions for clustered view
                    region_key = location_name or f"{round(lat, 1)},{round(lon, 1)}"
                    reg = regions[region_key]
                    reg["count"] += 1
                    if not reg["lat"]:
                        reg["lat"] = lat
                        reg["lon"] = lon
                        reg["name"] = location_name
                    reg["articles"].append({
                        "signal_id": r[0],
                        "headline": headline,
                        "source": payload.get("source", ""),
                        "timestamp": r[4].isoformat() if r[4] else None,
                        "domain": domain,
                    })

    except Exception as e:
        log.error(f"Feed map query failed: {e}")
        return {"type": "FeatureCollection", "features": [], "regions": [], "error": str(e)}

    # Build region list (for zoomed-out cluster view)
    region_list = []
    for key, reg in sorted(regions.items(), key=lambda x: -x[1]["count"]):
        region_list.append({
            "name": reg.get("name", key),
            "latitude": reg["lat"],
            "longitude": reg["lon"],
            "article_count": reg["count"],
            "articles": reg["articles"][:10],  # Top 10 for preview
            "domains": list(set(a["domain"] for a in reg["articles"])),
        })

    return {
        "type": "FeatureCollection",
        "features": features,
        "regions": region_list,
        "total_located": len(features),
    }
