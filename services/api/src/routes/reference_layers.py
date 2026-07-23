"""
Generic reference layer API.

Serves ANY reference data layer from the unified reference_points table.
One endpoint handles cell towers, telecom masts, power plants, military bases,
WiFi networks, border crossings, submarine cables, etc.

Layer configs stored in reference_layers table — no code changes to add new sources.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse

from storage.postgres.db import get_connection

logger = logging.getLogger("enki.reference_layers")
router = APIRouter(prefix="/ref", tags=["reference layers"])


@router.get("/layers")
async def list_layers():
    """List all available reference layers with their configs."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT layer_id, display_name, category, icon, color, zoom_min,
                       detail_fields, observable_types, enabled, item_count, last_synced,
                       source_type
                FROM reference_layers
                WHERE enabled = TRUE
                ORDER BY category, display_name
            """)
            layers = [{
                "layer_id": r[0], "display_name": r[1], "category": r[2],
                "icon": r[3], "color": r[4], "zoom_min": r[5],
                "detail_fields": r[6] if isinstance(r[6], list) else json.loads(r[6]) if r[6] else [],
                "observable_types": r[7] if isinstance(r[7], list) else json.loads(r[7]) if r[7] else [],
                "enabled": r[8], "item_count": r[9],
                "last_synced": r[10].isoformat() if r[10] else None,
                "source_type": r[11],
            } for r in cur.fetchall()]

    return {"count": len(layers), "layers": layers}


@router.get("/layers/{layer_id}/points")
async def query_layer_points(
    layer_id: str,
    north: float = Query(...), south: float = Query(...),
    east: float = Query(...), west: float = Query(...),
    zoom: int = Query(10),
    limit: int = Query(2000),
):
    """Spatial query for any reference layer with zoom-tier filtering.

    The zoom_tiers config in reference_layers defines which importance levels
    are visible at each zoom level. Higher importance = visible at lower zoom.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Get layer config
            cur.execute("""
                SELECT zoom_min, zoom_tiers, color, display_name
                FROM reference_layers WHERE layer_id = %s AND enabled = TRUE
            """, (layer_id,))
            layer = cur.fetchone()
            if not layer:
                return JSONResponse(status_code=404, content={"error": "LAYER.NOT_FOUND"})

            zoom_min = layer[0] or 0
            zoom_tiers = layer[1] if isinstance(layer[1], dict) else json.loads(layer[1]) if layer[1] else {}

            # Zoom gate: don't return data below minimum zoom
            if zoom < zoom_min:
                return {"count": 0, "points": [], "layer_id": layer_id}

            # Build importance filter from zoom tiers
            # zoom_tiers: {"4": "importance >= 2", "7": "importance >= 1", "10": "true"}
            # Find the highest tier threshold that's <= current zoom
            importance_filter = ""  # default: show everything if no tiers configured
            if zoom_tiers:
                importance_filter = "AND FALSE"  # if tiers exist but none match, show nothing
                for tier_zoom_str in sorted(zoom_tiers.keys(), key=lambda x: int(x)):
                    tier_zoom = int(tier_zoom_str)
                    if zoom >= tier_zoom:
                        condition = zoom_tiers[tier_zoom_str]
                        if condition == "true":
                            importance_filter = ""  # show everything
                        else:
                            importance_filter = f"AND {condition}"

            cur.execute(f"""
                SELECT external_id, name, latitude, longitude, category,
                       importance, properties
                FROM reference_points
                WHERE layer_id = %s
                  AND latitude BETWEEN %s AND %s
                  AND longitude BETWEEN %s AND %s
                  {importance_filter}
                LIMIT %s
            """, (layer_id, south, north, west, east, limit))

            points = [{
                "id": r[0], "name": r[1],
                "latitude": r[2], "longitude": r[3],
                "category": r[4], "importance": r[5],
                "properties": r[6] if isinstance(r[6], dict) else json.loads(r[6]) if r[6] else {},
            } for r in cur.fetchall()]

    return {"count": len(points), "points": points, "layer_id": layer_id}


@router.post("/layers/{layer_id}/sync")
async def sync_layer(layer_id: str):
    """Trigger sync for a reference layer. Handles Overpass, CSV, GeoJSON, and API sources."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT source_type, overpass_query, source_url
                FROM reference_layers WHERE layer_id = %s
            """, (layer_id,))
            layer = cur.fetchone()
            if not layer:
                return JSONResponse(status_code=404, content={"error": "LAYER.NOT_FOUND"})

    source_type, overpass_query, source_url = layer

    if source_type == 'overpass' and overpass_query:
        return await _sync_overpass(layer_id, overpass_query)
    elif layer_id == 'power_plants':
        return await _sync_power_plants(layer_id)
    elif layer_id == 'internet_exchanges':
        return await _sync_peeringdb(layer_id)
    elif layer_id == 'military_bases':
        return await _sync_military_bases(layer_id)
    elif layer_id == 'submarine_cables':
        return await _sync_submarine_cables(layer_id)
    elif layer_id == 'fuel_stations':
        return await _sync_overpass(layer_id, 'node["amenity"="fuel"]({bbox});')
    else:
        return {"status": "manual_sync_required", "layer_id": layer_id,
                "source_type": source_type, "source_url": source_url}


@router.post("/layers/sync-all")
async def sync_all_layers():
    """Sync all reference layers that can be auto-synced."""
    results = {}
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT layer_id, source_type, overpass_query FROM reference_layers WHERE enabled = TRUE")
            layers = cur.fetchall()

    for layer_id, source_type, overpass_query in layers:
        try:
            if source_type == 'overpass' and overpass_query:
                results[layer_id] = await _sync_overpass(layer_id, overpass_query)
            elif layer_id == 'power_plants':
                results[layer_id] = await _sync_power_plants(layer_id)
            elif layer_id == 'military_bases':
                results[layer_id] = await _sync_military_bases(layer_id)
            elif layer_id == 'submarine_cables':
                results[layer_id] = await _sync_submarine_cables(layer_id)
            elif source_type == 'api' and layer_id == 'internet_exchanges':
                results[layer_id] = await _sync_peeringdb(layer_id)
            else:
                results[layer_id] = {"status": "skipped", "source_type": source_type}
        except Exception as e:
            results[layer_id] = {"error": str(e)}
            _update_layer_status(layer_id, 0, str(e))

    return {"results": results}


# ─── Region-batched Overpass sync ─────────────────────────────────────────

# Split world into manageable bboxes to avoid Overpass timeouts
REGIONS = [
    ("North America", "15,-170,72,-50"),
    ("Central America", "-5,-120,15,-60"),
    ("South America", "-56,-82,13,-34"),
    ("Europe", "35,-25,72,45"),
    ("Africa", "-35,-20,37,52"),
    ("Middle East", "12,25,42,63"),
    ("South Asia", "5,60,40,98"),
    ("East Asia", "18,73,55,150"),
    ("Southeast Asia", "-12,95,28,155"),
    ("Oceania", "-50,110,0,180"),
    ("Russia", "40,25,82,180"),
]


async def _sync_overpass(layer_id: str, query_template: str) -> dict:
    """Sync a layer from OSM Overpass, batched by region to avoid timeouts."""
    import httpx

    total = 0
    region_results = {}

    # Overpass rejects requests without a User-Agent (HTTP 406), which silently
    # broke every Overpass-backed layer (cell towers, fuel stations, cables).
    ua = {"User-Agent": "Enki Governance (https://example.com)"}
    async with httpx.AsyncClient(timeout=180, headers=ua) as client:
        for region_name, bbox in REGIONS:
            try:
                query = query_template.replace("{bbox}", bbox)
                full_query = f"[out:json][timeout:120];({query});out center 50000;"

                resp = await client.post(
                    "https://overpass-api.de/api/interpreter",
                    data={"data": full_query},
                )
                if resp.status_code == 429:
                    # Rate limited — wait and skip
                    region_results[region_name] = "rate_limited"
                    import asyncio
                    await asyncio.sleep(5)
                    continue
                if resp.status_code != 200:
                    region_results[region_name] = f"HTTP {resp.status_code}"
                    continue

                data = resp.json()
                elements = data.get("elements", [])
                count = _upsert_elements(layer_id, elements)
                total += count
                region_results[region_name] = count

                # Brief pause between regions to avoid rate limiting
                import asyncio
                await asyncio.sleep(2)

            except Exception as e:
                region_results[region_name] = f"error: {str(e)[:80]}"

    _update_layer_status(layer_id, total)
    logger.info(f"Synced {total} points for layer {layer_id}")
    return {"synced": total, "layer_id": layer_id, "regions": region_results}


def _upsert_elements(layer_id: str, elements: list) -> int:
    """Bulk upsert OSM elements into reference_points."""
    count = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            for el in elements:
                lat = el.get("lat") or el.get("center", {}).get("lat")
                lon = el.get("lon") or el.get("center", {}).get("lon")
                if not lat or not lon:
                    continue

                ext_id = str(el.get("id", ""))
                tags = el.get("tags", {})
                name = tags.get("name", tags.get("operator", ""))

                importance = 0
                if tags.get("importance") == "international":
                    importance = 2
                elif tags.get("importance") == "national":
                    importance = 1

                try:
                    cur.execute("""
                        INSERT INTO reference_points (layer_id, external_id, name, latitude, longitude,
                            category, importance, properties, last_synced)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (layer_id, external_id) DO UPDATE SET
                            name = COALESCE(NULLIF(EXCLUDED.name, ''), reference_points.name),
                            latitude = EXCLUDED.latitude,
                            longitude = EXCLUDED.longitude,
                            properties = EXCLUDED.properties,
                            last_synced = NOW()
                    """, (
                        layer_id, ext_id, name, float(lat), float(lon),
                        tags.get("type", tags.get("tower:type", "")),
                        importance,
                        json.dumps({k: v for k, v in tags.items() if k != "name"}),
                    ))
                    count += 1
                except Exception:
                    pass

        conn.commit()
    return count


# ─── Non-Overpass sync functions ──────────────────────────────────────────

async def _sync_power_plants(layer_id: str) -> dict:
    """Sync Global Power Plant Database (WRI) — 35,000+ plants in 167 countries."""
    import httpx
    import csv
    import io

    url = "https://raw.githubusercontent.com/wri/global-power-plant-database/master/output_database/global_power_plant_database.csv"

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}", "synced": 0}

    reader = csv.DictReader(io.StringIO(resp.text))
    total = 0

    with get_connection() as conn:
        with conn.cursor() as cur:
            for row in reader:
                lat = row.get("latitude", "")
                lon = row.get("longitude", "")
                if not lat or not lon:
                    continue
                try:
                    lat_f, lon_f = float(lat), float(lon)
                except ValueError:
                    continue

                cap = float(row.get("capacity_mw", 0) or 0)
                importance = 2 if cap > 1000 else 1 if cap > 100 else 0

                try:
                    cur.execute("""
                        INSERT INTO reference_points (layer_id, external_id, name, latitude, longitude,
                            category, importance, properties, last_synced)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (layer_id, external_id) DO UPDATE SET
                            name = EXCLUDED.name, properties = EXCLUDED.properties, last_synced = NOW()
                    """, (
                        layer_id,
                        row.get("gppd_idnr", ""),
                        row.get("name", "Unknown Plant"),
                        lat_f, lon_f,
                        row.get("primary_fuel", ""),
                        importance,
                        json.dumps({
                            "fuel": row.get("primary_fuel", ""),
                            "capacity_mw": cap,
                            "country": row.get("country_long", ""),
                            "owner": row.get("owner", ""),
                            "commissioning_year": row.get("commissioning_year", ""),
                        }),
                    ))
                    total += 1
                except Exception:
                    pass

        conn.commit()

    _update_layer_status(layer_id, total)
    return {"synced": total, "layer_id": layer_id}


async def _sync_peeringdb(layer_id: str) -> dict:
    """Sync Internet Exchange Points from PeeringDB (900+ IXPs)."""
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get("https://www.peeringdb.com/api/ix?depth=1")
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}", "synced": 0}

        data = resp.json()

    ixps = data.get("data", [])
    total = 0

    with get_connection() as conn:
        with conn.cursor() as cur:
            for ix in ixps:
                lat = ix.get("latitude")
                lon = ix.get("longitude")
                if not lat or not lon:
                    continue

                try:
                    cur.execute("""
                        INSERT INTO reference_points (layer_id, external_id, name, latitude, longitude,
                            category, importance, properties, last_synced)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (layer_id, external_id) DO UPDATE SET
                            name = EXCLUDED.name, properties = EXCLUDED.properties, last_synced = NOW()
                    """, (
                        layer_id,
                        str(ix.get("id", "")),
                        ix.get("name", ""),
                        float(lat), float(lon),
                        "",
                        1,  # all IXPs are medium+ importance
                        json.dumps({
                            "name_long": ix.get("name_long", ""),
                            "city": ix.get("city", ""),
                            "country": ix.get("country", ""),
                            "website": ix.get("website", ""),
                            "proto_unicast": ix.get("proto_unicast", False),
                            "proto_ipv6": ix.get("proto_ipv6", False),
                        }),
                    ))
                    total += 1
                except Exception:
                    pass

        conn.commit()

    _update_layer_status(layer_id, total)
    return {"synced": total, "layer_id": layer_id}


async def _sync_military_bases(layer_id: str) -> dict:
    """Sync US military bases from USDOT/BTS open data (ArcGIS FeatureServer)."""
    import httpx

    url = "https://services.arcgis.com/xOi1kZaI0eWDREZv/ArcGIS/rest/services/Military_Bases/FeatureServer/0/query"
    total = 0

    async with httpx.AsyncClient(timeout=60) as client:
        offset = 0
        while True:
            resp = await client.get(url, params={
                "where": "1=1", "outFields": "SITE_NAME,OPER_STAT,JOINT_BASE,STATE_TERR,COUNTRY,LATITUDE,LONGITUDE",
                "f": "json", "resultOffset": offset, "resultRecordCount": 2000,
            })
            if resp.status_code != 200:
                break

            data = resp.json()
            features = data.get("features", [])
            if not features:
                break

            with get_connection() as conn:
                with conn.cursor() as cur:
                    for f in features:
                        a = f.get("attributes", {})
                        lat = a.get("LATITUDE")
                        lon = a.get("LONGITUDE")
                        if not lat or not lon:
                            # Try geometry
                            g = f.get("geometry", {})
                            lon = g.get("x")
                            lat = g.get("y")
                        if not lat or not lon:
                            continue

                        name = a.get("SITE_NAME", "Unknown Base")
                        importance = 2 if a.get("JOINT_BASE") == "Y" else 1

                        try:
                            cur.execute("""
                                INSERT INTO reference_points (layer_id, external_id, name, latitude, longitude,
                                    category, importance, properties, last_synced)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                                ON CONFLICT (layer_id, external_id) DO UPDATE SET
                                    name = EXCLUDED.name, properties = EXCLUDED.properties, last_synced = NOW()
                            """, (
                                layer_id, f"{name}_{lat}_{lon}", name, float(lat), float(lon),
                                a.get("OPER_STAT", ""),
                                importance,
                                json.dumps({
                                    "site_name": name,
                                    "oper_stat": a.get("OPER_STAT", ""),
                                    "joint_base": a.get("JOINT_BASE", ""),
                                    "state_terr": a.get("STATE_TERR", ""),
                                    "country": a.get("COUNTRY", ""),
                                }),
                            ))
                            total += 1
                        except Exception:
                            pass
                conn.commit()

            offset += len(features)
            if not data.get("exceededTransferLimit", False):
                break

    _update_layer_status(layer_id, total)
    return {"synced": total, "layer_id": layer_id}


async def _sync_submarine_cables(layer_id: str) -> dict:
    """Sync submarine cable landing points from the infrapedia/OSM community dataset."""
    import httpx

    # Use OSM Overpass for submarine cable landing stations
    query = '[out:json][timeout:120];(node["submarine"="yes"](-90,-180,90,180);node["man_made"="submarine_cable_landing_point"](-90,-180,90,180);node["telecom"="landing_point"](-90,-180,90,180););out 5000;'

    ua = {"User-Agent": "Enki Governance (https://example.com)"}
    async with httpx.AsyncClient(timeout=130, headers=ua) as client:
        resp = await client.post(
            "https://overpass-api.de/api/interpreter",
            data={"data": query},
        )
        if resp.status_code != 200:
            return {"error": f"Overpass {resp.status_code}", "synced": 0}

        data = resp.json()
        elements = data.get("elements", [])

    total = _upsert_elements(layer_id, elements)
    _update_layer_status(layer_id, total)
    return {"synced": total, "layer_id": layer_id}


def _update_layer_status(layer_id: str, count: int, error: str = None):
    """Update reference_layers item count and sync status."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE reference_layers SET
                    item_count = %s, last_synced = NOW(),
                    last_error = %s
                WHERE layer_id = %s
            """, (count, error, layer_id))
        conn.commit()
