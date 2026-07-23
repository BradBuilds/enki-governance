"""
Geospatial auto-enrichment engine.

When a knowledge node is created with lat/lon, this module calls
free public APIs in parallel to enrich the node with contextual data:
elevation, address, census, flood zone, weather, etc.

Each enrichment source is independent — if one fails, others still succeed.
Sources can be enabled/disabled and some accept optional API keys.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

logger = logging.getLogger("enki.enrichment")

# Timeout for each individual enrichment call
REQUEST_TIMEOUT = 10.0


# ─── Individual Enrichment Sources ───────────────────────────────────────────

async def enrich_elevation(client: httpx.AsyncClient, lat: float, lon: float) -> dict:
    """USGS Elevation Point Query — US coverage, no key needed."""
    resp = await client.get(
        "https://epqs.nationalmap.gov/v1/json",
        params={"x": lon, "y": lat, "wkid": 4326, "units": "Meters"},
    )
    resp.raise_for_status()
    data = resp.json()
    value = data.get("value")
    if value is not None:
        try:
            elev_m = float(value)
            return {"elevation_m": round(elev_m, 1), "elevation_ft": round(elev_m * 3.28084, 1)}
        except (ValueError, TypeError):
            pass
    return {}


async def enrich_address(client: httpx.AsyncClient, lat: float, lon: float) -> dict:
    """Nominatim reverse geocode — global, no key, 1req/s limit."""
    resp = await client.get(
        "https://nominatim.openstreetmap.org/reverse",
        params={"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 18},
        headers={"User-Agent": "EnkiCommandCenter/1.0 (admin@example.com)"},
    )
    resp.raise_for_status()
    data = resp.json()
    addr = data.get("address", {})
    result = {
        "display_name": data.get("display_name", ""),
        "road": addr.get("road", ""),
        "city": addr.get("city") or addr.get("town") or addr.get("village", ""),
        "state": addr.get("state", ""),
        "postcode": addr.get("postcode", ""),
        "country": addr.get("country", ""),
        "country_code": addr.get("country_code", ""),
    }
    # Clean empty values
    return {k: v for k, v in result.items() if v}


async def enrich_census(client: httpx.AsyncClient, lat: float, lon: float) -> dict:
    """US Census Geocoder — FIPS codes for state, county, tract, block. US only."""
    resp = await client.get(
        "https://geocoding.geo.census.gov/geocoder/geographies/coordinates",
        params={
            "x": lon, "y": lat,
            "benchmark": "Public_AR_Current",
            "vintage": "Current_Current",
            "format": "json",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    geographies = data.get("result", {}).get("geographies", {})

    result = {}
    # Census blocks
    blocks = geographies.get("2020 Census Blocks") or geographies.get("Census Blocks")
    if blocks and len(blocks) > 0:
        b = blocks[0]
        result["state_fips"] = b.get("STATE", "")
        result["county_fips"] = b.get("COUNTY", "")
        result["tract"] = b.get("TRACT", "")
        result["block"] = b.get("BLOCK", "")
        result["geoid"] = b.get("GEOID", "")

    # Counties
    counties = geographies.get("Counties")
    if counties and len(counties) > 0:
        result["county_name"] = counties[0].get("NAME", "")

    # States
    states = geographies.get("States")
    if states and len(states) > 0:
        result["state_name"] = states[0].get("NAME", "")

    return {k: v for k, v in result.items() if v}


async def enrich_fcc(client: httpx.AsyncClient, lat: float, lon: float) -> dict:
    """FCC Census/Area lookup — market area codes. US only."""
    resp = await client.get(
        "https://geo.fcc.gov/api/census/area",
        params={"lat": lat, "lon": lon, "format": "json"},
    )
    resp.raise_for_status()
    data = resp.json()
    results = data.get("results", [])
    if not results:
        return {}

    r = results[0]
    return {
        "county_name": r.get("county_name", ""),
        "state_code": r.get("state_code", ""),
        "state_fips": r.get("state_fips", ""),
        "county_fips": r.get("county_fips", ""),
    }


async def enrich_weather(client: httpx.AsyncClient, lat: float, lon: float) -> dict:
    """NWS current weather + alerts. US only."""
    result = {}
    try:
        # Get point metadata (forecast office, grid)
        resp = await client.get(
            f"https://api.weather.gov/points/{lat},{lon}",
            headers={"User-Agent": "(EnkiCommandCenter, admin@example.com)"},
        )
        if resp.status_code == 200:
            data = resp.json()
            props = data.get("properties", {})
            result["forecast_office"] = props.get("cwa", "")
            result["timezone"] = props.get("timeZone", "")

            # Get active alerts
            alerts_resp = await client.get(
                f"https://api.weather.gov/alerts/active",
                params={"point": f"{lat},{lon}"},
                headers={"User-Agent": "(EnkiCommandCenter, admin@example.com)"},
            )
            if alerts_resp.status_code == 200:
                alerts_data = alerts_resp.json()
                features = alerts_data.get("features", [])
                result["active_alerts"] = len(features)
                if features:
                    result["alert_headlines"] = [
                        f.get("properties", {}).get("headline", "")
                        for f in features[:3]
                    ]
    except Exception:
        pass
    return {k: v for k, v in result.items() if v}


async def enrich_flood_zone(client: httpx.AsyncClient, lat: float, lon: float) -> dict:
    """FEMA Flood Zone at point. US only."""
    # Query a small bbox around the point
    delta = 0.001  # ~100m
    resp = await client.get(
        "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query",
        params={
            "where": "1=1",
            "geometry": f"{lon - delta},{lat - delta},{lon + delta},{lat + delta}",
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "FLD_ZONE,ZONE_SUBTY,SFHA_TF",
            "returnGeometry": "false",
            "f": "json",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    features = data.get("features", [])
    if not features:
        return {}

    attrs = features[0].get("attributes", {})
    zone = attrs.get("FLD_ZONE", "")
    sfha = attrs.get("SFHA_TF", "")

    zone_desc = {
        "A": "High risk (100-year flood)",
        "AE": "High risk (base flood elevation determined)",
        "AH": "High risk (shallow flooding)",
        "AO": "High risk (sheet flow)",
        "V": "High risk (coastal flooding)",
        "VE": "High risk (coastal with wave action)",
        "X": "Moderate to low risk",
        "D": "Undetermined risk",
    }

    return {
        "flood_zone": zone,
        "flood_description": zone_desc.get(zone, f"Zone {zone}"),
        "special_flood_hazard": sfha == "T",
    }


# ─── Orchestrator ────────────────────────────────────────────────────────────

# ─── Global Sources ──────────────────────────────────────────────────────────

async def enrich_elevation_global(client: httpx.AsyncClient, lat: float, lon: float) -> dict:
    """Open-Elevation — global coverage using SRTM data. No key."""
    resp = await client.get(
        "https://api.open-elevation.com/api/v1/lookup",
        params={"locations": f"{lat},{lon}"},
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if results:
        elev = results[0].get("elevation")
        if elev is not None:
            return {"elevation_m": round(elev, 1), "elevation_ft": round(elev * 3.28084, 1)}
    return {}


async def enrich_weather_global(client: httpx.AsyncClient, lat: float, lon: float) -> dict:
    """Open-Meteo — global weather forecast. No key."""
    resp = await client.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,wind_speed_10m,relative_humidity_2m,weather_code,cloud_cover,pressure_msl",
            "timezone": "auto",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    current = data.get("current", {})

    # Map WMO weather codes to descriptions
    wmo_codes = {
        0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
        45: "Foggy", 48: "Rime fog", 51: "Light drizzle", 53: "Moderate drizzle",
        55: "Dense drizzle", 61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
        71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow", 77: "Snow grains",
        80: "Slight showers", 81: "Moderate showers", 82: "Violent showers",
        85: "Slight snow showers", 86: "Heavy snow showers",
        95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Thunderstorm with heavy hail",
    }

    code = current.get("weather_code", -1)
    return {
        "temperature_c": current.get("temperature_2m"),
        "temperature_f": round(current.get("temperature_2m", 0) * 9/5 + 32, 1) if current.get("temperature_2m") is not None else None,
        "wind_speed_kmh": current.get("wind_speed_10m"),
        "humidity_pct": current.get("relative_humidity_2m"),
        "cloud_cover_pct": current.get("cloud_cover"),
        "pressure_hpa": current.get("pressure_msl"),
        "conditions": wmo_codes.get(code, f"Code {code}"),
        "timezone": data.get("timezone", ""),
    }


async def enrich_air_quality(client: httpx.AsyncClient, lat: float, lon: float) -> dict:
    """Open-Meteo Air Quality — global. No key."""
    resp = await client.get(
        "https://air-quality-api.open-meteo.com/v1/air-quality",
        params={
            "latitude": lat, "longitude": lon,
            "current": "pm2_5,pm10,carbon_monoxide,nitrogen_dioxide,ozone,us_aqi",
        },
    )
    resp.raise_for_status()
    current = resp.json().get("current", {})
    aqi = current.get("us_aqi")
    aqi_desc = "Good" if aqi and aqi <= 50 else "Moderate" if aqi and aqi <= 100 else "Unhealthy" if aqi and aqi <= 150 else "Very Unhealthy" if aqi else None

    result = {
        "pm2_5": current.get("pm2_5"),
        "pm10": current.get("pm10"),
        "ozone": current.get("ozone"),
        "nitrogen_dioxide": current.get("nitrogen_dioxide"),
        "carbon_monoxide": current.get("carbon_monoxide"),
        "us_aqi": aqi,
    }
    if aqi_desc:
        result["aqi_description"] = aqi_desc
    return {k: v for k, v in result.items() if v is not None}


async def enrich_marine(client: httpx.AsyncClient, lat: float, lon: float) -> dict:
    """Open-Meteo Marine — wave, swell, ocean data. Only works over water."""
    try:
        resp = await client.get(
            "https://marine-api.open-meteo.com/v1/marine",
            params={
                "latitude": lat, "longitude": lon,
                "current": "wave_height,wave_direction,wave_period",
            },
        )
        resp.raise_for_status()
        current = resp.json().get("current", {})
        result = {
            "wave_height_m": current.get("wave_height"),
            "wave_direction_deg": current.get("wave_direction"),
            "wave_period_s": current.get("wave_period"),
        }
        return {k: v for k, v in result.items() if v is not None}
    except Exception:
        return {}  # Not over water — expected failure


async def enrich_country(client: httpx.AsyncClient, lat: float, lon: float) -> dict:
    """REST Countries via Nominatim country code — global context."""
    # First get country code from address (Nominatim)
    try:
        resp = await client.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 3},
            headers={"User-Agent": "EnkiCommandCenter/1.0 (admin@example.com)"},
        )
        resp.raise_for_status()
        cc = resp.json().get("address", {}).get("country_code", "").upper()
        if not cc:
            return {}

        # Get country details
        resp2 = await client.get(
            f"https://restcountries.com/v3.1/alpha/{cc}",
            params={"fields": "name,capital,population,area,region,subregion,languages,currencies,timezones"},
        )
        resp2.raise_for_status()
        c = resp2.json()

        return {
            "country": c.get("name", {}).get("common", ""),
            "capital": c.get("capital", [""])[0] if c.get("capital") else "",
            "population": c.get("population"),
            "area_km2": c.get("area"),
            "region": c.get("region", ""),
            "subregion": c.get("subregion", ""),
            "languages": list(c.get("languages", {}).values())[:3],
            "timezones": c.get("timezones", [])[:2],
        }
    except Exception:
        return {}


async def enrich_wikidata(client: httpx.AsyncClient, lat: float, lon: float) -> dict:
    """Wikidata SPARQL — nearby named entities. Global."""
    query = f"""
    SELECT ?place ?placeLabel ?placeDescription ?instanceofLabel WHERE {{
      SERVICE wikibase:around {{
        ?place wdt:P625 ?location .
        bd:serviceParam wikibase:center "Point({lon} {lat})"^^geo:wktLiteral .
        bd:serviceParam wikibase:radius "1" .
      }}
      OPTIONAL {{ ?place wdt:P31 ?instanceof }}
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" }}
    }} LIMIT 8
    """
    try:
        resp = await client.get(
            "https://query.wikidata.org/sparql",
            params={"format": "json", "query": query},
            headers={"User-Agent": "EnkiCommandCenter/1.0 (admin@example.com)"},
        )
        resp.raise_for_status()
        bindings = resp.json().get("results", {}).get("bindings", [])

        entities = []
        seen = set()
        for b in bindings:
            name = b.get("placeLabel", {}).get("value", "")
            if not name or name in seen:
                continue
            seen.add(name)
            entities.append({
                "name": name,
                "type": b.get("instanceofLabel", {}).get("value", ""),
                "description": b.get("placeDescription", {}).get("value", "")[:100],
            })

        return {"nearby_entities": entities} if entities else {}
    except Exception:
        return {}


async def enrich_disasters(client: httpx.AsyncClient, lat: float, lon: float) -> dict:
    """GDACS — active global disasters near this point."""
    try:
        resp = await client.get(
            "https://www.gdacs.org/gdacsapi/api/events/geteventlist/MAP",
        )
        resp.raise_for_status()
        features = resp.json().get("features", [])

        # Filter to within ~200km of our point
        from math import radians, sin, cos, sqrt, atan2
        nearby = []
        for f in features:
            coords = f.get("geometry", {}).get("coordinates", [])
            if len(coords) < 2:
                continue
            flon, flat = coords[0], coords[1]
            # Haversine approximation
            dlat = radians(flat - lat)
            dlon = radians(flon - lon)
            a = sin(dlat/2)**2 + cos(radians(lat)) * cos(radians(flat)) * sin(dlon/2)**2
            dist_km = 6371 * 2 * atan2(sqrt(a), sqrt(1-a))
            if dist_km < 500:
                p = f.get("properties", {})
                nearby.append({
                    "type": p.get("eventtype", ""),
                    "name": p.get("eventname", p.get("name", "")),
                    "alert_level": p.get("alertlevel", ""),
                    "distance_km": round(dist_km),
                })

        return {"active_disasters": nearby} if nearby else {}
    except Exception:
        return {}


# Source registry — each source has an async function and metadata
ENRICHMENT_SOURCES = {
    "elevation": {
        "fn": enrich_elevation,
        "name": "USGS Elevation",
        "requires_key": False,
        "coverage": "US",
        "category": "terrain",
    },
    "address": {
        "fn": enrich_address,
        "name": "Nominatim (OpenStreetMap)",
        "requires_key": False,
        "coverage": "Global",
        "category": "location",
    },
    "census": {
        "fn": enrich_census,
        "name": "US Census Geocoder",
        "requires_key": False,
        "coverage": "US",
        "category": "demographics",
    },
    "fcc": {
        "fn": enrich_fcc,
        "name": "FCC Area Lookup",
        "requires_key": False,
        "coverage": "US",
        "category": "infrastructure",
    },
    "weather": {
        "fn": enrich_weather,
        "name": "NWS Weather",
        "requires_key": False,
        "coverage": "US",
        "category": "environmental",
    },
    "flood_zone": {
        "fn": enrich_flood_zone,
        "name": "FEMA Flood Zones",
        "requires_key": False,
        "coverage": "US",
        "category": "environmental",
    },
    # ── Global sources ──
    "elevation_global": {
        "fn": enrich_elevation_global,
        "name": "Open-Elevation (Global)",
        "requires_key": False,
        "coverage": "Global",
        "category": "terrain",
    },
    "weather_global": {
        "fn": enrich_weather_global,
        "name": "Open-Meteo Weather",
        "requires_key": False,
        "coverage": "Global",
        "category": "environmental",
    },
    "air_quality": {
        "fn": enrich_air_quality,
        "name": "Open-Meteo Air Quality",
        "requires_key": False,
        "coverage": "Global",
        "category": "environmental",
    },
    "marine": {
        "fn": enrich_marine,
        "name": "Open-Meteo Marine",
        "requires_key": False,
        "coverage": "Global",
        "category": "environmental",
    },
    "country": {
        "fn": enrich_country,
        "name": "REST Countries",
        "requires_key": False,
        "coverage": "Global",
        "category": "demographics",
    },
    "wikidata": {
        "fn": enrich_wikidata,
        "name": "Wikidata Nearby Entities",
        "requires_key": False,
        "coverage": "Global",
        "category": "knowledge",
    },
    "disasters": {
        "fn": enrich_disasters,
        "name": "GDACS Active Disasters",
        "requires_key": False,
        "coverage": "Global",
        "category": "environmental",
    },
}


def get_smart_sources(lat: float, lon: float) -> list[str]:
    """Select enrichment sources based on location.

    US locations get US-specific sources (Census, FEMA, FCC, NWS).
    All locations get global sources.
    Avoids running both US elevation and global elevation.
    """
    # Rough US bounding box
    is_us = (24.0 <= lat <= 50.0 and -130.0 <= lon <= -65.0)
    # Rough ocean check (very approximate)
    is_likely_ocean = (abs(lat) > 60 or  # polar
                       (abs(lat) < 5 and abs(lon) > 100))  # deep ocean

    sources = ["address", "weather_global", "air_quality", "country", "wikidata", "disasters"]

    if is_us:
        sources.extend(["elevation", "census", "fcc", "weather", "flood_zone"])
    else:
        sources.append("elevation_global")

    if is_likely_ocean or (lat < 0 and lon < -50):  # rough marine areas
        sources.append("marine")

    return sources


async def auto_enrich_location(
    lat: float,
    lon: float,
    enabled_sources: Optional[list[str]] = None,
    api_keys: Optional[dict[str, str]] = None,
) -> dict:
    """
    Run enrichment sources in parallel for a lat/lon.

    If no sources specified, auto-selects based on location
    (US sources for US locations, global sources everywhere).
    Failed sources are logged but don't block others.
    """
    if enabled_sources is None:
        enabled_sources = get_smart_sources(lat, lon)

    results = {}

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        tasks = {}
        for source_id in enabled_sources:
            source = ENRICHMENT_SOURCES.get(source_id)
            if not source:
                continue
            tasks[source_id] = source["fn"](client, lat, lon)

        # Run all in parallel
        gathered = await asyncio.gather(
            *[tasks[sid] for sid in tasks],
            return_exceptions=True,
        )

        for sid, result in zip(tasks.keys(), gathered):
            if isinstance(result, Exception):
                logger.warning(f"Enrichment source {sid} failed: {result}")
                results[sid] = {"_error": str(result)}
            elif result:
                results[sid] = result

    return results


def list_enrichment_sources() -> list[dict]:
    """List all available enrichment sources with metadata."""
    return [
        {
            "id": sid,
            "name": source["name"],
            "requires_key": source["requires_key"],
            "coverage": source["coverage"],
            "category": source["category"],
        }
        for sid, source in ENRICHMENT_SOURCES.items()
    ]
