"""
Camera infrastructure API.

GET  /cameras          — Fast spatial query from local DB
POST /cameras/sync     — Bulk sync camera positions from all providers
GET  /cameras/stats    — Camera count by provider
GET  /cameras/{id}     — Single camera detail with live image URL
"""

from __future__ import annotations

import re
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

from storage.postgres.db import get_connection

logger = logging.getLogger("enki.cameras")
router = APIRouter(prefix="/cameras", tags=["cameras"])


@router.get("")
async def query_cameras(
    north: float = Query(...),
    south: float = Query(...),
    east: float = Query(...),
    west: float = Query(...),
    zoom: int = Query(12),
    provider: Optional[str] = Query(None),
    limit: int = Query(5000),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Fast spatial query — returns cameras within bounding box from local DB.

    Progressive density by zoom:
      0-5   → limit 50  (sparse dots on continent view)
      6-8   → limit 200 (state-level overview)
      9-11  → limit 500 (regional)
      12+   → full density within viewport
    """
    if zoom <= 5:
        effective_limit = min(limit, 50)
    elif zoom <= 8:
        effective_limit = min(limit, 200)
    elif zoom <= 11:
        effective_limit = min(limit, 500)
    else:
        effective_limit = limit

    with get_connection() as conn:
        with conn.cursor() as cur:
            if provider:
                cur.execute("""
                    SELECT camera_id, provider, name, latitude, longitude,
                           image_url, video_url, road, direction, region
                    FROM cameras
                    WHERE latitude BETWEEN %s AND %s
                      AND longitude BETWEEN %s AND %s
                      AND provider = %s AND active = TRUE
                    LIMIT %s
                """, (south, north, west, east, provider, effective_limit))
            else:
                cur.execute("""
                    SELECT camera_id, provider, name, latitude, longitude,
                           image_url, video_url, road, direction, region
                    FROM cameras
                    WHERE latitude BETWEEN %s AND %s
                      AND longitude BETWEEN %s AND %s
                      AND active = TRUE
                    LIMIT %s
                """, (south, north, west, east, effective_limit))

            cameras = [{
                "camera_id": r[0], "provider": r[1], "name": r[2],
                "latitude": r[3], "longitude": r[4],
                "image_url": r[5], "video_url": r[6],
                "road": r[7], "direction": r[8], "region": r[9],
            } for r in cur.fetchall()]

    return {"count": len(cameras), "cameras": cameras}


@router.get("/stats")
async def camera_stats():
    """Camera count by provider."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT provider, count(*), max(last_synced)
                FROM cameras WHERE active = TRUE
                GROUP BY provider ORDER BY count DESC
            """)
            stats = [{
                "provider": r[0], "count": r[1],
                "last_synced": r[2].isoformat() if r[2] else None,
            } for r in cur.fetchall()]
            cur.execute("SELECT count(*) FROM cameras WHERE active = TRUE")
            total = cur.fetchone()[0]

    return {"total": total, "providers": stats}


@router.get("/nearby")
async def nearby_cameras(
    lat: float = Query(...),
    lon: float = Query(...),
    radius_m: float = Query(2000),
    limit: int = Query(10),
):
    """Find cameras near a point (for enrichment / location cards)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT camera_id, name, provider, latitude, longitude, image_url,
                       ST_Distance(location, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) as dist_m
                FROM cameras
                WHERE active = TRUE AND location IS NOT NULL
                  AND ST_DWithin(location, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)
                ORDER BY dist_m LIMIT %s
            """, (lon, lat, lon, lat, radius_m, limit))
            cameras = [{
                "camera_id": r[0], "name": r[1], "provider": r[2],
                "latitude": r[3], "longitude": r[4], "image_url": r[5],
                "distance_m": round(r[6], 1),
            } for r in cur.fetchall()]
    return {"cameras": cameras, "count": len(cameras)}


@router.get("/{camera_id}")
async def get_camera(camera_id: str):
    """Get single camera detail."""
    import json as _json
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT camera_id, provider, name, latitude, longitude,
                       image_url, video_url, road, direction, region, metadata
                FROM cameras WHERE camera_id = %s
            """, (camera_id,))
            r = cur.fetchone()
            if not r:
                return JSONResponse(status_code=404, content={"error": "CAMERA.NOT_FOUND"})
            meta = r[10]
            if isinstance(meta, memoryview):
                meta = _json.loads(bytes(meta))
            elif isinstance(meta, str):
                meta = _json.loads(meta)
            return {
                "camera_id": r[0], "provider": r[1], "name": r[2],
                "latitude": r[3], "longitude": r[4],
                "image_url": r[5], "video_url": r[6],
                "road": r[7], "direction": r[8], "region": r[9],
                "metadata": meta,
            }


@router.post("/sync")
async def sync_cameras():
    """Bulk sync camera positions from all providers into local DB.

    This fetches camera indexes from NYC TMC, TfL London, TransCore 511 states,
    and Caltrans, then upserts them all into the cameras table.
    Cameras don't move — this only needs to run periodically (daily/weekly).
    """
    import httpx

    results = {}
    all_cameras = []

    async with httpx.AsyncClient(timeout=15) as client:
        # ── NYC TMC ──
        try:
            resp = await client.get("https://webcams.nyctmc.org/api/cameras")
            if resp.status_code == 200:
                cams = resp.json()
                for cam in cams:
                    lat, lon = cam.get("latitude"), cam.get("longitude")
                    if lat is None or lon is None:
                        continue
                    all_cameras.append({
                        "camera_id": f"nyc_{cam.get('id', '')}",
                        "provider": "nyc_tmc",
                        "name": cam.get("name", "NYC Camera"),
                        "latitude": lat, "longitude": lon,
                        "image_url": cam.get("imageUrl", ""),
                        "video_url": cam.get("videoUrl", ""),
                        "region": "New York City",
                    })
                results["nyc_tmc"] = len([c for c in all_cameras if c["provider"] == "nyc_tmc"])
        except Exception as e:
            results["nyc_tmc"] = f"error: {e}"

        # ── TfL London ──
        try:
            resp = await client.get("https://api.tfl.gov.uk/Place/Type/JamCam")
            if resp.status_code == 200:
                cams = resp.json()
                for cam in cams:
                    lat, lon = cam.get("lat"), cam.get("lon")
                    if lat is None or lon is None:
                        continue
                    img_url = ""
                    vid_url = ""
                    for prop in cam.get("additionalProperties", []):
                        if prop.get("key") == "imageUrl":
                            img_url = prop.get("value", "")
                        elif prop.get("key") == "videoUrl":
                            vid_url = prop.get("value", "")
                    all_cameras.append({
                        "camera_id": f"tfl_{cam.get('id', '')}",
                        "provider": "tfl_jamcam",
                        "name": cam.get("commonName", "London Camera"),
                        "latitude": lat, "longitude": lon,
                        "image_url": img_url, "video_url": vid_url,
                        "region": "London",
                    })
                results["tfl_jamcam"] = len([c for c in all_cameras if c["provider"] == "tfl_jamcam"])
        except Exception as e:
            results["tfl_jamcam"] = f"error: {e}"

        # ── TransCore 511 States ──
        state_configs = [
            ("FL", "https://fl511.com"),
            ("GA", "https://511ga.org"),
            ("PA", "https://511pa.com"),
            ("NV", "https://www.nvroads.com"),
            ("AZ", "https://az511.gov"),
            ("WI", "https://511wi.gov"),
            ("LA", "https://511la.org"),
            ("ID", "https://511.idaho.gov"),
        ]
        for state, url in state_configs:
            provider = f"511_{state}"
            try:
                resp = await client.get(f"{url}/map/mapIcons/Cameras", params={"lang": "en"})
                if resp.status_code == 200:
                    data = resp.json()
                    cam_list = data.get("item2", [])
                    if not isinstance(cam_list, list):
                        results[provider] = "unexpected format"
                        continue
                    count = 0
                    for cam in cam_list:
                        loc = cam.get("location", [])
                        if not loc or len(loc) < 2:
                            continue
                        cam_id = cam.get("itemId", "")
                        all_cameras.append({
                            "camera_id": f"511_{state}_{cam_id}",
                            "provider": provider,
                            "name": cam.get("title", "") or f"{state} Camera {cam_id}",
                            "latitude": float(loc[0]), "longitude": float(loc[1]),
                            "image_url": f"{url}/map/Cctv/{cam_id}",
                            "region": state,
                        })
                        count += 1
                    results[provider] = count
            except Exception as e:
                results[provider] = f"error: {e}"

        # ── Caltrans (12 districts) ──
        cal_total = 0
        for dist in range(1, 13):
            try:
                dn = f"{dist:02d}"
                resp = await client.get(
                    f"https://cwwp2.dot.ca.gov/data/d{dist}/cctv/cctvStatusD{dn}.json"
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
                        img_data = cctv.get("imageData", {})
                        static = img_data.get("static", {})
                        stream = img_data.get("streamingVideoURL", "")
                        all_cameras.append({
                            "camera_id": f"caltrans_{dist}_{cctv.get('index', '')}",
                            "provider": "caltrans",
                            "name": loc.get("locationName", f"Caltrans D{dist}"),
                            "latitude": float(lat_str), "longitude": float(lon_str),
                            "image_url": static.get("currentImageURL", ""),
                            "video_url": stream if isinstance(stream, str) else "",
                            "road": loc.get("route", ""),
                            "direction": loc.get("direction", ""),
                            "region": f"California D{dist}",
                        })
                        cal_total += 1
            except Exception:
                pass
        results["caltrans"] = cal_total

    # ── Bulk upsert into database ──
    inserted = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            for cam in all_cameras:
                try:
                    cur.execute("""
                        INSERT INTO cameras (camera_id, provider, name, latitude, longitude,
                            image_url, video_url, road, direction, region, last_synced)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (camera_id) DO UPDATE SET
                            name = EXCLUDED.name,
                            latitude = EXCLUDED.latitude,
                            longitude = EXCLUDED.longitude,
                            image_url = EXCLUDED.image_url,
                            video_url = EXCLUDED.video_url,
                            road = EXCLUDED.road,
                            direction = EXCLUDED.direction,
                            region = EXCLUDED.region,
                            last_synced = NOW()
                    """, (
                        cam["camera_id"], cam["provider"], cam["name"],
                        cam["latitude"], cam["longitude"],
                        cam.get("image_url", ""), cam.get("video_url", ""),
                        cam.get("road", ""), cam.get("direction", ""),
                        cam.get("region", ""),
                    ))
                    inserted += 1
                except Exception:
                    pass
        conn.commit()

    return {
        "synced": inserted,
        "total_fetched": len(all_cameras),
        "by_provider": results,
    }
