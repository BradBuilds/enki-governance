"""
Space & astronomy API — satellites, near-Earth objects, fireballs, space weather.

All data served from local PostgreSQL (synced periodically from free APIs).
Satellite positions are computed client-side from TLE data using satellite.js.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from storage.postgres.db import get_connection
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

router = APIRouter(tags=["space"])


# ─── Satellites ──────────────────────────────────────────────────────────────

@router.get("/satellites")
async def query_satellites(
    north: float = Query(...), south: float = Query(...),
    east: float = Query(...), west: float = Query(...),
    zoom: int = Query(5),
    constellation: str = Query(None),
    include_debris: bool = Query(False),
    limit: int = Query(2000),
):
    """Spatial query for satellites with zoom-tier filtering.

    Zoom tiers (by constellation importance):
      0-3  → ISS only
      4-6  → + GPS, Galileo, GLONASS, WEATHER
      7-9  → + GEO, EARTH_RESOURCE, IRIDIUM, ONEWEB
      10+  → everything including Starlink (~6000 sats)

    Debris (object_type DEBRIS / ROCKET_BODY, ~7k objects from Cosmos 1408,
    Fengyun 1C, Iridium-Cosmos collision, Briz-M) is excluded by default
    to keep the active-satellite picture readable. Pass include_debris=true
    to get the full catalog. Constellation=DEBRIS works regardless of the
    flag (caller wants debris explicitly).

    Returns TLE data for client-side orbital propagation via satellite.js.
    """
    # Build constellation filter based on zoom (TLEs are propagated client-side)
    if constellation:
        const_filter = "AND constellation = %s"
        params: list = [constellation, limit]
    elif zoom <= 3:
        # Whole-globe view: the recognizable navigation + geostationary rings
        # (~800 sats, MEO/GEO) — iconic on a globe and cheap to render. Starlink
        # (~10k LEO) is held back until the user zooms in.
        const_filter = "AND constellation IN ('ISS', 'GPS', 'GALILEO', 'GLONASS', 'GEO')"
        params = [limit]
    elif zoom <= 6:
        const_filter = "AND constellation IN ('ISS', 'GPS', 'GALILEO', 'GLONASS', 'GEO', 'WEATHER', 'EARTH_RESOURCE', 'IRIDIUM')"
        params = [limit]
    elif zoom <= 9:
        const_filter = "AND constellation IN ('ISS', 'GPS', 'GALILEO', 'GLONASS', 'GEO', 'WEATHER', 'EARTH_RESOURCE', 'IRIDIUM', 'ONEWEB')"
        params = [limit]
    else:
        const_filter = ""
        params = [limit]

    # Suppress debris unless the caller asked for it AND wasn't already
    # narrowing by an explicit constellation.
    debris_filter = ""
    if not include_debris and constellation != "DEBRIS":
        debris_filter = "AND (object_type IS NULL OR object_type NOT IN ('DEBRIS', 'ROCKET_BODY'))"

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Return TLEs for client-side propagation — no bbox filter needed
            # (satellite positions change every second, stored positions are stale)
            cur.execute(f"""
                SELECT satellite_id, name, constellation, object_type, orbit_type,
                       country_code, tle_line1, tle_line2, latitude, longitude, altitude_km
                FROM satellites
                WHERE tle_line1 IS NOT NULL AND tle_line1 != ''
                {const_filter}
                {debris_filter}
                LIMIT %s
            """, params)

            sats = [{
                "satellite_id": r[0], "name": r[1], "constellation": r[2],
                "object_type": r[3], "orbit_type": r[4], "country_code": r[5],
                "tle_line1": r[6], "tle_line2": r[7],
                "latitude": r[8], "longitude": r[9], "altitude_km": r[10],
            } for r in cur.fetchall()]

    return {"count": len(sats), "satellites": sats}


@router.get("/satellites/search")
async def search_satellites(q: str = Query(..., min_length=2), limit: int = Query(50)):
    """Search satellites by name."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT satellite_id, name, constellation, orbit_type, country_code
                FROM satellites
                WHERE LOWER(name) LIKE LOWER(%s)
                ORDER BY
                    CASE WHEN constellation = 'ISS' THEN 0
                         WHEN constellation IN ('GPS', 'GALILEO', 'GLONASS') THEN 1
                         ELSE 2 END,
                    name
                LIMIT %s
            """, (f"%{q}%", limit))
            results = [{
                "satellite_id": r[0], "name": r[1], "constellation": r[2],
                "orbit_type": r[3], "country_code": r[4],
            } for r in cur.fetchall()]
    return {"results": results, "count": len(results)}


@router.get("/satellites/{norad_id}")
async def get_satellite(norad_id: int):
    """Get satellite detail with full TLE."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT satellite_id, name, intl_designator, constellation, object_type,
                       orbit_type, country_code, tle_line1, tle_line2,
                       latitude, longitude, altitude_km, metadata
                FROM satellites WHERE satellite_id = %s
            """, (norad_id,))
            r = cur.fetchone()
            if not r:
                return JSONResponse(status_code=404, content={"error": "SATELLITE.NOT_FOUND"})
            return {
                "satellite_id": r[0], "name": r[1], "intl_designator": r[2],
                "constellation": r[3], "object_type": r[4], "orbit_type": r[5],
                "country_code": r[6], "tle_line1": r[7], "tle_line2": r[8],
                "latitude": r[9], "longitude": r[10], "altitude_km": r[11],
                "metadata": r[12],
            }


# ─── Fireballs ───────────────────────────────────────────────────────────────

@router.get("/fireballs")
async def query_fireballs(
    north: float = Query(90), south: float = Query(-90),
    east: float = Query(180), west: float = Query(-180),
    days: int = Query(365),
    limit: int = Query(500),
):
    """Spatial query for fireball events."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT fireball_id, date, latitude, longitude, altitude_km,
                       velocity_kms, total_radiated_energy_j, impact_energy_kt
                FROM fireballs
                WHERE latitude BETWEEN %s AND %s AND longitude BETWEEN %s AND %s
                  AND date > NOW() - make_interval(days => %s)
                ORDER BY date DESC
                LIMIT %s
            """, (south, north, west, east, days, limit))
            fireballs = [{
                "fireball_id": r[0], "date": r[1].isoformat() if r[1] else None,
                "latitude": r[2], "longitude": r[3],
                "altitude_km": r[4], "velocity_kms": r[5],
                "total_radiated_energy_j": r[6], "impact_energy_kt": r[7],
            } for r in cur.fetchall()]

    return {"count": len(fireballs), "fireballs": fireballs}


# ─── Near-Earth Objects ──────────────────────────────────────────────────────

@router.get("/neo/upcoming")
async def upcoming_neos(days: int = Query(60), limit: int = Query(50)):
    """Upcoming NEO close approaches sorted by date."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT n.neo_id, n.name, n.designation, n.is_potentially_hazardous,
                       n.estimated_diameter_min_km, n.estimated_diameter_max_km,
                       n.absolute_magnitude_h,
                       c.close_approach_date, c.relative_velocity_kms,
                       c.miss_distance_km, c.miss_distance_lunar
                FROM neo_close_approaches c
                JOIN near_earth_objects n ON n.neo_id = c.neo_id
                WHERE c.close_approach_date BETWEEN CURRENT_DATE AND CURRENT_DATE + make_interval(days => %s)
                ORDER BY c.close_approach_date
                LIMIT %s
            """, (days, limit))
            neos = [{
                "neo_id": r[0], "name": r[1], "designation": r[2],
                "is_potentially_hazardous": r[3],
                "estimated_diameter_min_km": r[4], "estimated_diameter_max_km": r[5],
                "absolute_magnitude_h": r[6],
                "close_approach_date": r[7].isoformat() if r[7] else None,
                "relative_velocity_kms": r[8],
                "miss_distance_km": r[9], "miss_distance_lunar": r[10],
            } for r in cur.fetchall()]

    return {"count": len(neos), "approaches": neos}


@router.get("/neo/hazardous")
async def hazardous_neos(limit: int = Query(20)):
    """Potentially hazardous asteroids with upcoming close approaches."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT n.neo_id, n.name, n.designation,
                       n.estimated_diameter_min_km, n.estimated_diameter_max_km,
                       c.close_approach_date, c.miss_distance_km, c.miss_distance_lunar,
                       c.relative_velocity_kms
                FROM neo_close_approaches c
                JOIN near_earth_objects n ON n.neo_id = c.neo_id
                WHERE n.is_potentially_hazardous = TRUE
                  AND c.close_approach_date >= CURRENT_DATE
                ORDER BY c.close_approach_date
                LIMIT %s
            """, (limit,))
            neos = [{
                "neo_id": r[0], "name": r[1], "designation": r[2],
                "estimated_diameter_min_km": r[3], "estimated_diameter_max_km": r[4],
                "close_approach_date": r[5].isoformat() if r[5] else None,
                "miss_distance_km": r[6], "miss_distance_lunar": r[7],
                "relative_velocity_kms": r[8],
            } for r in cur.fetchall()]

    return {"count": len(neos), "hazardous": neos}


# ─── Space Weather ───────────────────────────────────────────────────────────

@router.get("/space-weather/current")
async def current_space_weather():
    """Active / recent space weather events (last 7 days)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT event_id, event_type, class_type, intensity,
                       start_time, peak_time, end_time, source_location, link
                FROM space_weather_events
                WHERE start_time > NOW() - interval '7 days'
                ORDER BY start_time DESC
                LIMIT 50
            """)
            events = [{
                "event_id": r[0], "event_type": r[1], "class_type": r[2],
                "intensity": r[3],
                "start_time": r[4].isoformat() if r[4] else None,
                "peak_time": r[5].isoformat() if r[5] else None,
                "end_time": r[6].isoformat() if r[6] else None,
                "source_location": r[7], "link": r[8],
            } for r in cur.fetchall()]

    return {"count": len(events), "events": events}


@router.get("/space-weather/summary")
async def space_weather_summary():
    """Dashboard summary: current conditions."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Recent solar flares (last 24h)
            cur.execute("""
                SELECT count(*), max(class_type) FROM space_weather_events
                WHERE event_type = 'solar_flare' AND start_time > NOW() - interval '24 hours'
            """)
            flare_row = cur.fetchone()
            # Recent CMEs (last 48h)
            cur.execute("""
                SELECT count(*) FROM space_weather_events
                WHERE event_type = 'cme' AND start_time > NOW() - interval '48 hours'
            """)
            cme_count = cur.fetchone()[0]
            # Latest geomagnetic storm
            cur.execute("""
                SELECT class_type, intensity, start_time FROM space_weather_events
                WHERE event_type = 'geomagnetic_storm'
                ORDER BY start_time DESC LIMIT 1
            """)
            storm = cur.fetchone()

    return {
        "solar_flares_24h": flare_row[0] if flare_row else 0,
        "max_flare_class": flare_row[1] if flare_row else None,
        "cme_count_48h": cme_count,
        "latest_storm": {
            "kp_class": storm[0], "intensity": storm[1],
            "start_time": storm[2].isoformat() if storm[2] else None,
        } if storm else None,
    }


# ─── Sync triggers ──────────────────────────────────────────────────────────

@router.post("/space/sync/{dataset}")
async def sync_space_dataset(dataset: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Trigger sync for a space dataset. Requires authentication."""
    from core.reference.space_sync import (
        sync_celestrak_tle, sync_fireballs, sync_neows, sync_donki
    )

    fns = {
        "satellites": sync_celestrak_tle,
        "fireballs": sync_fireballs,
        "near_earth_objects": sync_neows,
        "space_weather": sync_donki,
    }
    fn = fns.get(dataset)
    if not fn:
        return JSONResponse(status_code=400, content={"error": f"Unknown dataset: {dataset}"})
    return await fn()


@router.post("/space/sync-all")
async def sync_all_space(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Trigger sync for all space datasets. Requires authentication."""
    from core.reference.space_sync import sync_all_space
    return await sync_all_space()
