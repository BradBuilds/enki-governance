"""
Live cache API — browse high-volume data without permanent ingestion.

GET /live/{source}     — Query hot cache for a source type (adsb, ais, etc.)
GET /live/{source}/geo — GeoJSON FeatureCollection from hot cache
DELETE /live/expired   — Purge expired cache entries
"""

from __future__ import annotations

import json
from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

from storage.postgres.db import get_connection

router = APIRouter(prefix="/live", tags=["live cache"])


async def _get_with_retry(client, url, attempts: int = 3):
    """GET with brief backoff. The free live feeds (adsb.lol, digitraffic) are
    often momentarily rate-limited/busy; one shot with a 10s timeout gives up
    too easily. Returns the 200 response, or None after `attempts` tries."""
    import asyncio
    for i in range(attempts):
        try:
            resp = await client.get(url)
            if resp.status_code == 200:
                return resp
        except Exception:
            pass
        await asyncio.sleep(0.4 * (i + 1))
    return None


@router.get("/{source}/viewport")
async def viewport_fetch(
    source: str,
    north: float = Query(...),
    south: float = Query(...),
    east: float = Query(...),
    west: float = Query(...),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Fetch live data for a map viewport by proxying external APIs.

    This is the viewport-driven endpoint: the map sends its bounds,
    the backend queries the appropriate external API for that area,
    writes results to hot cache + history, and returns them.
    """
    import httpx
    import math

    center_lat = (north + south) / 2
    center_lon = (east + west) / 2
    # Rough radius in nautical miles from bounds
    lat_span = north - south
    lon_span = east - west
    radius_nm = max(5, min(250, int(math.sqrt(lat_span**2 + lon_span**2) * 30)))

    items = []

    if source == "adsb":
        try:
            url = f"https://api.adsb.lol/v2/lat/{center_lat}/lon/{center_lon}/dist/{radius_nm}"
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await _get_with_retry(client, url)
                if resp is not None:
                    data = resp.json()
                    aircraft = data.get("ac", [])

                    # Write to hot cache + history
                    _write_viewport_cache(auth.tenant_id, "adsb", aircraft, center_lat, center_lon)

                    # Return as items
                    for ac in aircraft:
                        lat = ac.get("lat")
                        lon = ac.get("lon")
                        if lat is None or lon is None:
                            continue
                        # Viewport filter: only return items in the requested bounds
                        if lat < south or lat > north or lon < west or lon > east:
                            continue
                        items.append({
                            "key": ac.get("hex", ""),
                            "label": (ac.get("flight") or ac.get("r") or ac.get("hex") or "").strip(),
                            "payload": ac,
                            "latitude": lat,
                            "longitude": lon,
                            "altitude": _safe_float(ac.get("alt_baro")),
                            "heading": _safe_float(ac.get("track")),
                            "speed": _safe_float(ac.get("gs")),
                            "cached_at": None,
                        })
        except Exception as e:
            return {"source": source, "count": 0, "items": [], "error": str(e)}

    elif source == "ais":
        # Free AIS via the Finnish Transport Agency (Digitraffic) — no key, but
        # coverage is the Baltic/Finland region only. Returns all tracked
        # vessels; we filter to the requested viewport. Global coverage needs a
        # keyed provider (aisstream.io / MarineTraffic) wired the same way.
        try:
            url = "https://meri.digitraffic.fi/api/ais/v1/locations"
            async with httpx.AsyncClient(
                timeout=10, headers={"User-Agent": "Enki Governance"}
            ) as client:
                resp = await _get_with_retry(client, url)
                if resp is not None:
                    vessels = resp.json().get("features", [])
                    kept = []
                    for v in vessels:
                        coords = (v.get("geometry") or {}).get("coordinates") or []
                        if len(coords) < 2:
                            continue
                        lon, lat = coords[0], coords[1]
                        if lat < south or lat > north or lon < west or lon > east:
                            continue
                        props = v.get("properties") or {}
                        items.append({
                            "key": str(props.get("mmsi", "")),
                            "label": str(props.get("mmsi") or "vessel"),
                            "payload": props,
                            "latitude": lat, "longitude": lon,
                            "heading": _safe_float(props.get("heading") or props.get("cog")),
                            "speed": _safe_float(props.get("sog")),
                            "cached_at": None,
                        })
                        kept.append(props)
                    _write_viewport_cache(auth.tenant_id, "ais", kept, center_lat, center_lon)
        except Exception as e:
            return {"source": source, "count": 0, "items": [], "error": str(e)}

    elif source == "cameras":
        # Traffic cameras — query multiple providers based on viewport
        try:
            camera_items = await _fetch_cameras_for_viewport(
                client=httpx.AsyncClient(timeout=10),
                north=north, south=south, east=east, west=west,
                tenant_id=auth.tenant_id,
            )
            items = camera_items
        except Exception as e:
            return {"source": source, "count": 0, "items": [], "error": str(e)}

    return {"source": source, "count": len(items), "items": items}


def _safe_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _write_viewport_cache(tenant_id: str, source: str, items: list, center_lat: float, center_lon: float):
    """Write viewport-fetched items to hot cache + history (background, best-effort)."""
    import json as _json
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                for item in items:
                    lat = item.get("lat")
                    lon = item.get("lon")
                    key = item.get("hex") or item.get("mmsi") or ""
                    if not key or lat is None or lon is None:
                        continue

                    label = (item.get("flight") or item.get("r") or item.get("shipname") or key).strip()
                    payload = _json.dumps(item, default=str)
                    alt = _safe_float(item.get("alt_baro") or item.get("altitude"))
                    hdg = _safe_float(item.get("track") or item.get("heading"))
                    spd = _safe_float(item.get("gs") or item.get("speed"))

                    # Upsert cache
                    cur.execute("""
                        INSERT INTO live_cache (tenant_id, source, key, display_label, payload,
                            latitude, longitude, altitude, heading, speed, cached_at, expires_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW() + interval '4 hours')
                        ON CONFLICT (tenant_id, source, key) DO UPDATE SET
                            payload = EXCLUDED.payload, latitude = EXCLUDED.latitude,
                            longitude = EXCLUDED.longitude, altitude = EXCLUDED.altitude,
                            heading = EXCLUDED.heading, speed = EXCLUDED.speed,
                            display_label = EXCLUDED.display_label,
                            cached_at = NOW(), expires_at = NOW() + interval '4 hours'
                    """, (tenant_id, source, key, label[:500], payload,
                          float(lat), float(lon), alt, hdg, spd))

                    # Append history
                    summary = {}
                    for sk in ('flight', 'r', 'squawk', 'shipname', 'mmsi', 't'):
                        if sk in item:
                            summary[sk] = item[sk]

                    cur.execute("""
                        INSERT INTO live_history (tenant_id, source, key, latitude, longitude,
                            altitude, heading, speed, payload_summary)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (tenant_id, source, key, float(lat), float(lon), alt, hdg, spd,
                          _json.dumps(summary, default=str)))

                # Purge old
                cur.execute("DELETE FROM live_cache WHERE expires_at < NOW()")
                cur.execute("DELETE FROM live_history WHERE recorded_at < NOW() - interval '24 hours'")
            conn.commit()
    except Exception:
        pass  # best-effort


async def _fetch_cameras_for_viewport(
    client: httpx.AsyncClient,
    north: float, south: float, east: float, west: float,
    tenant_id: str = "default",
) -> list[dict]:
    """Fetch traffic camera positions for the viewport from multiple providers.

    Checks which camera providers cover the viewport area and queries them.
    Cameras are cached in live_cache for subsequent requests.
    """
    import re

    items = []
    center_lat = (north + south) / 2
    center_lon = (east + west) / 2

    # ── NYC TMC (covers NYC area: lat 40.4-41.0, lon -74.3 to -73.5) ──
    if south < 41.0 and north > 40.4 and west < -73.5 and east > -74.3:
        try:
            resp = await client.get("https://webcams.nyctmc.org/api/cameras", timeout=8)
            if resp.status_code == 200:
                cameras = resp.json()
                for cam in cameras:
                    lat = cam.get("latitude")
                    lon = cam.get("longitude")
                    if lat is None or lon is None:
                        continue
                    if lat < south or lat > north or lon < west or lon > east:
                        continue
                    items.append({
                        "key": cam.get("id", ""),
                        "label": cam.get("name", "NYC Camera"),
                        "payload": cam,
                        "latitude": lat,
                        "longitude": lon,
                        "altitude": None,
                        "heading": None,
                        "speed": None,
                        "image_url": cam.get("imageUrl", ""),
                        "source_provider": "nyc_tmc",
                    })
        except Exception:
            pass

    # ── TfL London JamCam (covers London: lat 51.2-51.7, lon -0.6 to 0.4) ──
    if south < 51.7 and north > 51.2 and west < 0.4 and east > -0.6:
        try:
            resp = await client.get("https://api.tfl.gov.uk/Place/Type/JamCam", timeout=8)
            if resp.status_code == 200:
                cameras = resp.json()
                for cam in cameras:
                    lat = cam.get("lat")
                    lon = cam.get("lon")
                    if lat is None or lon is None:
                        continue
                    if lat < south or lat > north or lon < west or lon > east:
                        continue
                    # Extract image URL from additionalProperties
                    img_url = ""
                    for prop in cam.get("additionalProperties", []):
                        if prop.get("key") == "imageUrl":
                            img_url = prop.get("value", "")
                            break
                    items.append({
                        "key": cam.get("id", ""),
                        "label": cam.get("commonName", "London Camera"),
                        "payload": {"provider": "tfl", "id": cam.get("id")},
                        "latitude": lat,
                        "longitude": lon,
                        "altitude": None,
                        "heading": None,
                        "speed": None,
                        "image_url": img_url,
                        "source_provider": "tfl_jamcam",
                    })
        except Exception:
            pass

    # ── TransCore 511 States (mapIcons endpoint — returns lat/lon directly) ──
    state_511_configs = [
        {"name": "FL", "url": "https://fl511.com", "bbox": (24, -87, 31, -80)},
        {"name": "GA", "url": "https://511ga.org", "bbox": (30.5, -85.5, 35, -81)},
        {"name": "PA", "url": "https://511pa.com", "bbox": (39.5, -80.5, 42.5, -75)},
        {"name": "NV", "url": "https://www.nvroads.com", "bbox": (35, -120, 42, -114)},
        {"name": "AZ", "url": "https://az511.gov", "bbox": (31.5, -114.5, 37, -109)},
        {"name": "WI", "url": "https://511wi.gov", "bbox": (42.4, -92.9, 47.1, -86.8)},
        {"name": "LA", "url": "https://511la.org", "bbox": (28.9, -94.1, 33.1, -88.8)},
        {"name": "ID", "url": "https://511.idaho.gov", "bbox": (42.0, -117.2, 49.0, -111.0)},
    ]

    for state_cfg in state_511_configs:
        s_south, s_west, s_north, s_east = state_cfg["bbox"]
        if south > s_north or north < s_south or west > s_east or east < s_west:
            continue
        try:
            resp = await client.get(
                f"{state_cfg['url']}/map/mapIcons/Cameras",
                params={"lang": "en"},
                timeout=8,
            )
            if resp.status_code == 200:
                data = resp.json()
                cam_list = data.get("item2", [])
                if not isinstance(cam_list, list):
                    continue
                for cam in cam_list:
                    loc = cam.get("location", [])
                    if not loc or len(loc) < 2:
                        continue
                    lat_c, lon_c = float(loc[0]), float(loc[1])
                    if lat_c < south or lat_c > north or lon_c < west or lon_c > east:
                        continue
                    cam_id = cam.get("itemId", "")
                    items.append({
                        "key": f"511_{state_cfg['name']}_{cam_id}",
                        "label": cam.get("title", "") or f"{state_cfg['name']} Camera {cam_id}",
                        "payload": {"provider": f"511_{state_cfg['name']}", "id": cam_id},
                        "latitude": lat_c,
                        "longitude": lon_c,
                        "altitude": None,
                        "heading": None,
                        "speed": None,
                        "image_url": f"{state_cfg['url']}/map/Cctv/{cam_id}",
                        "source_provider": f"511_{state_cfg['name']}",
                    })
        except Exception:
            pass

    # ── Caltrans (12 districts, different API format) ──
    # Only fetch if viewport overlaps California (lat 32-42, lon -124 to -114)
    if south < 42 and north > 32 and west < -114 and east > -124:
        # Determine which districts overlap the viewport
        cal_districts = [
            (1, 38.5, -124, 42, -122),  # D1 - Eureka
            (2, 40, -123, 42, -120),     # D2 - Redding
            (3, 38, -122, 40, -119.5),   # D3 - Sacramento
            (4, 37, -123, 38.5, -121),   # D4 - Oakland/SF
            (5, 34.5, -121.5, 37.5, -119), # D5 - SLO/Santa Barbara
            (6, 35, -120.5, 37.5, -118),  # D6 - Fresno
            (7, 33.5, -119, 35, -117),    # D7 - LA
            (8, 33.5, -118, 35, -114.5),  # D8 - San Bernardino
            (9, 37, -120, 38.5, -118),    # D9 - Bishop
            (10, 37, -122, 38.5, -120),   # D10 - Stockton
            (11, 32.5, -117.5, 33.5, -116), # D11 - San Diego
            (12, 33.5, -118.5, 34, -117),  # D12 - Orange County
        ]
        for dist_num, d_south, d_west, d_north, d_east in cal_districts:
            if south > d_north or north < d_south or west > d_east or east < d_west:
                continue
            try:
                dn = f"{dist_num:02d}"
                resp = await client.get(
                    f"https://cwwp2.dot.ca.gov/data/d{dist_num}/cctv/cctvStatusD{dn}.json",
                    timeout=8,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for cam in data.get("data", []):
                        cctv = cam.get("cctv", {})
                        loc = cctv.get("location", {})
                        lat_str = loc.get("latitude")
                        lon_str = loc.get("longitude")
                        if not lat_str or not lon_str:
                            continue
                        lat_c = float(lat_str)
                        lon_c = float(lon_str)
                        if lat_c < south or lat_c > north or lon_c < west or lon_c > east:
                            continue
                        img_data = cctv.get("imageData", {})
                        static = img_data.get("static", {})
                        img_url = static.get("currentImageURL", "")
                        items.append({
                            "key": f"caltrans_{dist_num}_{cctv.get('index', '')}",
                            "label": loc.get("locationName", f"Caltrans D{dist_num}"),
                            "payload": {"provider": "caltrans", "district": dist_num},
                            "latitude": lat_c,
                            "longitude": lon_c,
                            "altitude": None,
                            "heading": None,
                            "speed": None,
                            "image_url": img_url,
                            "source_provider": "caltrans",
                        })
            except Exception:
                pass

    # Write all to hot cache
    _write_viewport_cache(tenant_id, "cameras", [
        {"hex": item["key"], "lat": item["latitude"], "lon": item["longitude"],
         "flight": item["label"], "image_url": item.get("image_url", ""),
         "source_provider": item.get("source_provider", "")}
        for item in items
    ], center_lat, center_lon)

    return items


@router.get("/{source}")
async def query_live_cache(
    source: str,
    lat: float = Query(None),
    lon: float = Query(None),
    radius_km: float = Query(50),
    limit: int = Query(200),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Query the hot cache for a source type.

    If lat/lon provided, filters by radius. Otherwise returns all non-expired entries.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            if lat is not None and lon is not None:
                # PostGIS distance filter
                cur.execute("""
                    SELECT key, display_label, payload, latitude, longitude,
                           altitude, heading, speed, cached_at
                    FROM live_cache
                    WHERE tenant_id = %s AND source = %s AND expires_at > NOW()
                      AND latitude IS NOT NULL AND longitude IS NOT NULL
                      AND ST_DWithin(
                          ST_MakePoint(longitude, latitude)::geography,
                          ST_MakePoint(%s, %s)::geography,
                          %s
                      )
                    ORDER BY cached_at DESC
                    LIMIT %s
                """, (auth.tenant_id, source, lon, lat, radius_km * 1000, limit))
            else:
                cur.execute("""
                    SELECT key, display_label, payload, latitude, longitude,
                           altitude, heading, speed, cached_at
                    FROM live_cache
                    WHERE tenant_id = %s AND source = %s AND expires_at > NOW()
                    ORDER BY cached_at DESC
                    LIMIT %s
                """, (auth.tenant_id, source, limit))

            items = []
            for r in cur.fetchall():
                payload = r[2]
                if isinstance(payload, memoryview):
                    payload = bytes(payload)
                if isinstance(payload, (bytes, str)):
                    payload = json.loads(payload)

                items.append({
                    "key": r[0],
                    "label": r[1],
                    "payload": payload,
                    "latitude": r[3],
                    "longitude": r[4],
                    "altitude": r[5],
                    "heading": r[6],
                    "speed": r[7],
                    "cached_at": r[8].isoformat() if r[8] else None,
                })

    return {"source": source, "count": len(items), "items": items}


@router.get("/{source}/geo")
async def live_geojson(
    source: str,
    lat: float = Query(None),
    lon: float = Query(None),
    radius_km: float = Query(50),
    limit: int = Query(500),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """GeoJSON FeatureCollection from hot cache — for map display."""
    result = await query_live_cache(source, lat, lon, radius_km, limit, auth.tenant_id)

    features = []
    for item in result["items"]:
        if item["latitude"] is not None and item["longitude"] is not None:
            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [item["longitude"], item["latitude"]],
                },
                "properties": {
                    "key": item["key"],
                    "label": item["label"],
                    "altitude": item["altitude"],
                    "heading": item["heading"],
                    "speed": item["speed"],
                    "cached_at": item["cached_at"],
                    "source": source,
                    "_live": True,  # flag for map to style differently
                },
            })

    return {
        "type": "FeatureCollection",
        "features": features,
    }


@router.get("/{source}/trail/{key}")
async def get_trail(
    source: str,
    key: str,
    hours: float = Query(4),
    limit: int = Query(200),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Get position history trail for a specific entity (aircraft, vessel, etc.).

    Returns ordered positions for drawing a polyline on the map.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT latitude, longitude, altitude, heading, speed,
                       payload_summary, recorded_at
                FROM live_history
                WHERE tenant_id = %s AND source = %s AND key = %s
                  AND recorded_at > NOW() - interval '%s hours'
                ORDER BY recorded_at ASC
                LIMIT %s
            """, (auth.tenant_id, source, key, hours, limit))

            positions = []
            for r in cur.fetchall():
                summary = r[5]
                if isinstance(summary, memoryview):
                    summary = json.loads(bytes(summary))
                elif isinstance(summary, str):
                    summary = json.loads(summary)

                positions.append({
                    "lat": r[0],
                    "lon": r[1],
                    "altitude": r[2],
                    "heading": r[3],
                    "speed": r[4],
                    "summary": summary,
                    "time": r[6].isoformat() if r[6] else None,
                })

    # Build GeoJSON LineString for map rendering
    coords = [[p["lon"], p["lat"]] for p in positions if p["lat"] and p["lon"]]
    geojson = None
    if len(coords) >= 2:
        geojson = {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {
                "source": source,
                "key": key,
                "point_count": len(coords),
            },
        }

    return {
        "source": source,
        "key": key,
        "positions": positions,
        "count": len(positions),
        "track": geojson,
    }


@router.get("/{source}/trails")
async def get_active_trails(
    source: str,
    min_points: int = Query(3),
    hours: float = Query(1),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Get all entities that have trail history (for rendering multiple trails at once)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT key, count(*) as points,
                       min(recorded_at) as first_seen,
                       max(recorded_at) as last_seen
                FROM live_history
                WHERE tenant_id = %s AND source = %s
                  AND recorded_at > NOW() - interval '%s hours'
                GROUP BY key
                HAVING count(*) >= %s
                ORDER BY max(recorded_at) DESC
                LIMIT 100
            """, (auth.tenant_id, source, hours, min_points))

            entities = [{
                "key": r[0],
                "points": r[1],
                "first_seen": r[2].isoformat() if r[2] else None,
                "last_seen": r[3].isoformat() if r[3] else None,
            } for r in cur.fetchall()]

    return {"source": source, "entities": entities, "count": len(entities)}


@router.delete("/expired")
async def purge_expired(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Purge expired cache entries. Requires authentication."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM live_cache WHERE expires_at < NOW()")
            count = cur.rowcount
        conn.commit()
    return {"purged": count}
