"""
Reference data API — ports, airports, aircraft registry, sync management.

All reference data lives in local PostgreSQL for fast spatial queries.
Synced periodically from external sources.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse

from storage.postgres.db import get_connection
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

router = APIRouter(tags=["reference data"])


# ─── Ports ─────────────────────────────────────────────────────────────────────

@router.get("/ports")
async def query_ports(
    north: float = Query(...), south: float = Query(...),
    east: float = Query(...), west: float = Query(...),
    zoom: int = Query(10),
    limit: int = Query(2000),
):
    """Spatial query for ports within bounding box, filtered by zoom level.

    Zoom tiers:
      0-5  → large only (major shipping hubs)
      6-8  → + medium
      9-11 → + small
      12+  → everything (very small, open anchorages)
    """
    if zoom <= 5:
        size_filter = "AND LOWER(harbor_size) IN ('large', 'l')"
    elif zoom <= 8:
        size_filter = "AND LOWER(harbor_size) IN ('large', 'l', 'medium', 'm')"
    elif zoom <= 11:
        size_filter = "AND LOWER(harbor_size) IN ('large', 'l', 'medium', 'm', 'small', 's')"
    else:
        size_filter = ""  # show everything

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT port_id, name, country, latitude, longitude,
                       harbor_size, harbor_type, shelter
                FROM ports
                WHERE latitude BETWEEN %s AND %s AND longitude BETWEEN %s AND %s
                {size_filter}
                LIMIT %s
            """, (south, north, west, east, limit))
            ports = [{
                "port_id": r[0], "name": r[1], "country": r[2],
                "latitude": r[3], "longitude": r[4],
                "harbor_size": r[5], "harbor_type": r[6], "shelter": r[7],
            } for r in cur.fetchall()]
    return {"count": len(ports), "ports": ports}


@router.get("/ports/nearby")
async def nearby_ports(
    lat: float = Query(...), lon: float = Query(...),
    radius_km: float = Query(50), limit: int = Query(5),
):
    """Find ports near a point."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT port_id, name, country, latitude, longitude, harbor_size,
                       ST_Distance(location, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) / 1000 as dist_km
                FROM ports WHERE location IS NOT NULL
                  AND ST_DWithin(location, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)
                ORDER BY dist_km LIMIT %s
            """, (lon, lat, lon, lat, radius_km * 1000, limit))
            ports = [{
                "port_id": r[0], "name": r[1], "country": r[2],
                "latitude": r[3], "longitude": r[4], "harbor_size": r[5],
                "distance_km": round(r[6], 1),
            } for r in cur.fetchall()]
    return {"ports": ports, "count": len(ports)}


# ─── Airports ──────────────────────────────────────────────────────────────────

@router.get("/airports")
async def query_airports(
    north: float = Query(...), south: float = Query(...),
    east: float = Query(...), west: float = Query(...),
    zoom: int = Query(10),
    limit: int = Query(2000),
):
    """Spatial query for airports within bounding box, filtered by zoom level.

    Zoom tiers:
      0-4  → large_airport only (major international hubs)
      5-7  → + medium_airport
      8-10 → + small_airport with scheduled_service
      11-13→ + all small_airport
      14+  → everything (heliports, seaplane bases, closed)
    """
    # Build type filter based on zoom
    # Supports both OurAirports types (large_airport, medium_airport, etc.)
    # and FAA type codes (AD=aerodrome, HP=heliport, SP=seaplane, etc.)
    if zoom <= 4:
        type_filter = "AND type IN ('large_airport')"
    elif zoom <= 7:
        type_filter = "AND type IN ('large_airport', 'medium_airport')"
    elif zoom <= 10:
        type_filter = "AND (type IN ('large_airport', 'medium_airport') OR (type IN ('small_airport', 'AD') AND scheduled_service = TRUE))"
    elif zoom <= 13:
        type_filter = "AND type IN ('large_airport', 'medium_airport', 'small_airport', 'AD')"
    else:
        type_filter = ""  # show everything including heliports, seaplane bases, etc.

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT airport_id, icao, name, city, country, latitude, longitude,
                       elevation_ft, type, military, scheduled_service
                FROM airports
                WHERE latitude BETWEEN %s AND %s AND longitude BETWEEN %s AND %s
                {type_filter}
                LIMIT %s
            """, (south, north, west, east, limit))
            airports = [{
                "airport_id": r[0], "icao": r[1], "name": r[2],
                "city": r[3], "country": r[4],
                "latitude": r[5], "longitude": r[6],
                "elevation_ft": r[7], "type": r[8], "military": r[9],
                "scheduled_service": r[10],
            } for r in cur.fetchall()]
    return {"count": len(airports), "airports": airports}


@router.get("/airports/nearby")
async def nearby_airports(
    lat: float = Query(...), lon: float = Query(...),
    radius_km: float = Query(50), limit: int = Query(5),
):
    """Find airports near a point."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT airport_id, icao, name, city, latitude, longitude, type, elevation_ft,
                       ST_Distance(location, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) / 1000 as dist_km
                FROM airports WHERE location IS NOT NULL
                  AND ST_DWithin(location, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)
                ORDER BY dist_km LIMIT %s
            """, (lon, lat, lon, lat, radius_km * 1000, limit))
            airports = [{
                "airport_id": r[0], "icao": r[1], "name": r[2], "city": r[3],
                "latitude": r[4], "longitude": r[5], "type": r[6],
                "elevation_ft": r[7], "distance_km": round(r[8], 1),
            } for r in cur.fetchall()]
    return {"airports": airports, "count": len(airports)}


# ─── Aircraft Registry ─────────────────────────────────────────────────────────

@router.get("/aircraft/{n_number}")
async def lookup_aircraft(n_number: str):
    """Look up aircraft registration by N-number."""
    # Normalize: strip leading N, uppercase
    clean = n_number.upper().strip().lstrip("N")
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Try with and without N prefix
            cur.execute("""
                SELECT n_number, serial_number, mfr_name, model, type_aircraft,
                       engine_type, registrant_name, registrant_city, registrant_state,
                       registrant_country, status
                FROM aircraft_registry
                WHERE n_number = %s OR n_number = %s
                LIMIT 1
            """, (clean, f"N{clean}"))
            r = cur.fetchone()
            if not r:
                return JSONResponse(status_code=404, content={"error": "AIRCRAFT.NOT_FOUND"})
            return {
                "n_number": r[0], "serial_number": r[1], "manufacturer": r[2],
                "model": r[3], "type": r[4], "engine_type": r[5],
                "registrant": r[6], "city": r[7], "state": r[8],
                "country": r[9], "status": r[10],
            }


# ─── Sync Management ──────────────────────────────────────────────────────────

@router.get("/reference/sync-status")
async def sync_status():
    """Get sync status for all reference datasets."""
    from core.reference.sync import get_sync_status
    return {"datasets": get_sync_status()}


@router.post("/reference/sync/{dataset}")
async def sync_dataset(dataset: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Trigger sync for a specific dataset. Requires authentication."""
    from core.reference.sync import sync_ports, sync_airports, _update_sync_status

    if dataset == "cameras":
        from services.api.src.routes.cameras import sync_cameras
        result = await sync_cameras()
        return result
    elif dataset == "ports":
        result = await sync_ports()
        return result
    elif dataset == "airports":
        result = await sync_airports()
        return result
    elif dataset == "aircraft_registry":
        from core.reference.sync import sync_aircraft_registry
        result = await sync_aircraft_registry()
        return result
    else:
        return JSONResponse(status_code=400, content={"error": f"Unknown dataset: {dataset}"})


@router.post("/reference/sync-all")
async def sync_all_datasets(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Trigger sync for all reference datasets. Requires authentication."""
    results = {}
    from core.reference.sync import sync_ports, sync_airports
    from services.api.src.routes.cameras import sync_cameras

    for name, fn in [("cameras", sync_cameras), ("ports", sync_ports), ("airports", sync_airports)]:
        try:
            results[name] = await fn()
        except Exception as e:
            results[name] = {"error": str(e)}

    return {"results": results}
