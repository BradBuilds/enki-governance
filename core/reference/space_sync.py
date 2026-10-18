"""
Space & astronomy data sync engine.

Syncs satellite TLEs, near-Earth objects, fireballs, and space weather
into local PostgreSQL for fast queries. All sources are free.
"""

from __future__ import annotations

import asyncio
import logging
import json
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

from storage.postgres.db import get_connection
from core.reference.sync import _update_sync_status, _safe_int

logger = logging.getLogger("enki.reference.space_sync")
TIMEOUT = 30

# CelesTrak rate-limits aggressively and 403s requests with default httpx
# User-Agent. They publish a policy at celestrak.org/CelesTrak-policy.php
# — identify yourself as a real project and they're happy. Pace the
# request loop too so we don't burst.
CELESTRAK_HEADERS = {
    "User-Agent": "Enki Governance / sync (https://example.com; contact: ops@example.com)",
    "Accept": "text/plain",
}
CELESTRAK_DELAY_S = 0.6  # ~1.5 req/s, well under the 1 req/3s policy ceiling


# ─── CelesTrak Satellites ────────────────────────────────────────────────────

# Constellation groups to sync (most useful subsets).
#
# The third element marks debris/breakup catalogs so the renderer can
# default them to off — they balloon the object count by ~7k pieces
# without obvious payoff for the typical user. Operators who want them
# turn on the "Show debris" toggle in the map layers panel.
CELESTRAK_GROUPS = [
    # Active payloads
    ("stations", "ISS", False),
    ("starlink", "STARLINK", False),
    ("gps-ops", "GPS", False),
    ("galileo", "GALILEO", False),
    ("glonass-operational", "GLONASS", False),
    ("weather", "WEATHER", False),
    ("resource", "EARTH_RESOURCE", False),
    ("geo", "GEO", False),
    ("active", None, False),  # everything still operating
    # Debris and rocket bodies from notable breakups
    ("cosmos-1408-debris", "DEBRIS", True),     # Russia 2021 ASAT test
    ("fengyun-1c-debris", "DEBRIS", True),      # China 2007 ASAT test
    ("iridium-33-debris", "DEBRIS", True),      # 2009 collision
    ("cosmos-2251-debris", "DEBRIS", True),     # 2009 collision (other half)
    ("2012-044", "DEBRIS", True),               # Briz-M upper-stage breakup
    ("last-30-days", None, False),              # New launches + recent objects
]


def _guess_constellation(name: str) -> str:
    """Guess constellation from satellite name."""
    n = name.upper()
    if "STARLINK" in n:
        return "STARLINK"
    if "GPS" in n or "NAVSTAR" in n:
        return "GPS"
    if "GALILEO" in n:
        return "GALILEO"
    if "GLONASS" in n or "COSMOS" in n:
        return "GLONASS"
    if "ISS" in n or "ZARYA" in n or "UNITY" in n:
        return "ISS"
    if "IRIDIUM" in n:
        return "IRIDIUM"
    if "ONEWEB" in n:
        return "ONEWEB"
    if "NOAA" in n or "GOES" in n or "METEOSAT" in n:
        return "WEATHER"
    return ""


def _classify_object_type(name: str) -> str:
    """Infer DEBRIS / ROCKET_BODY / PAYLOAD from the catalog name.

    CelesTrak's naming convention:
      "FOO DEB"   → debris fragment
      "FOO R/B"   → spent rocket body / upper stage
      "FOO PLAT"  → orbital platform (rare)
      everything else → operational payload
    """
    n = (name or "").upper()
    if " DEB" in n or n.endswith("DEB"):
        return "DEBRIS"
    if " R/B" in n or n.endswith("R/B"):
        return "ROCKET_BODY"
    if " PLAT" in n:
        return "PLATFORM"
    return "PAYLOAD"


def _orbit_type_from_period(mean_motion: float) -> str:
    """Determine orbit type from TLE mean motion (revs/day)."""
    if mean_motion == 0:
        return ""
    period_min = 1440.0 / mean_motion
    if period_min < 128:
        return "LEO"
    elif period_min < 800:
        return "MEO"
    elif period_min > 1400:
        return "GEO"
    else:
        return "HEO"


def _parse_3le_text(text: str) -> list[tuple[str, str, str]]:
    """Parse 3-line element format: name, tle1, tle2 on consecutive lines."""
    lines = [l.rstrip() for l in text.strip().splitlines() if l.strip()]
    results = []
    i = 0
    while i < len(lines) - 2:
        # TLE line 1 starts with '1 ', line 2 with '2 '
        if lines[i + 1].startswith('1 ') and lines[i + 2].startswith('2 '):
            results.append((lines[i].strip(), lines[i + 1], lines[i + 2]))
            i += 3
        else:
            i += 1
    return results


def _norad_from_tle(tle1: str) -> int | None:
    """Extract NORAD catalog number from TLE line 1."""
    try:
        return int(tle1[2:7].strip())
    except (ValueError, IndexError):
        return None


async def sync_celestrak_tle(dsn: Optional[str] = None) -> dict:
    """Sync satellite TLE data from CelesTrak (no API key needed).

    Fetches 3-line element format (3LE) which includes raw TLE lines
    needed for satellite.js client-side propagation.
    """
    total = 0
    errors = {}

    async with httpx.AsyncClient(timeout=TIMEOUT, headers=CELESTRAK_HEADERS) as client:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                first = True
                for group, default_constellation, _is_debris_group in CELESTRAK_GROUPS:
                    # Polite pacing between groups (skip before the first call).
                    if not first:
                        await asyncio.sleep(CELESTRAK_DELAY_S)
                    first = False
                    try:
                        # Use 3le format to get raw TLE lines
                        resp = await client.get(
                            f"https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=3le"
                        )
                        if resp.status_code != 200:
                            errors[group] = f"HTTP {resp.status_code}"
                            continue

                        entries = _parse_3le_text(resp.text)
                        for name, tle1, tle2 in entries:
                            norad_id = _norad_from_tle(tle1)
                            if not norad_id:
                                continue

                            # Object type from the TLE name suffix
                            # (DEB / R/B / PAYLOAD). Reliable across catalogs.
                            object_type = _classify_object_type(name)

                            # Constellation: prefer the group's default unless
                            # the row is debris — then keep DEBRIS no matter
                            # what name-guessing would say (Cosmos 2251 DEB
                            # looked like GLONASS to the old guesser).
                            if object_type in ("DEBRIS", "ROCKET_BODY"):
                                constellation = "DEBRIS"
                            else:
                                constellation = default_constellation or _guess_constellation(name)

                            # Extract mean motion from TLE line 2 for orbit type
                            try:
                                mean_motion = float(tle2[52:63].strip())
                            except (ValueError, IndexError):
                                mean_motion = 0

                            cur.execute("""
                                INSERT INTO satellites (
                                    satellite_id, name, intl_designator, object_type,
                                    constellation, orbit_type,
                                    tle_line1, tle_line2, last_synced
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                                ON CONFLICT (satellite_id) DO UPDATE SET
                                    name = EXCLUDED.name,
                                    object_type = EXCLUDED.object_type,
                                    constellation = COALESCE(NULLIF(EXCLUDED.constellation, ''), satellites.constellation),
                                    orbit_type = COALESCE(NULLIF(EXCLUDED.orbit_type, ''), satellites.orbit_type),
                                    tle_line1 = EXCLUDED.tle_line1,
                                    tle_line2 = EXCLUDED.tle_line2,
                                    last_synced = NOW()
                            """, (
                                norad_id,
                                name,
                                tle1[9:17].strip() if len(tle1) > 17 else "",
                                object_type,
                                constellation,
                                _orbit_type_from_period(mean_motion),
                                tle1,
                                tle2,
                            ))
                            total += 1

                    except Exception as e:
                        errors[group] = str(e)

                conn.commit()

    logger.info(f"Synced {total} satellites from CelesTrak (3LE format)")
    _update_sync_status("satellites", total, dsn=dsn)
    return {"synced": total, "errors": errors if errors else None}


# ─── NASA Fireballs ──────────────────────────────────────────────────────────

async def sync_fireballs(dsn: Optional[str] = None) -> dict:
    """Sync fireball/bolide events from NASA CNEOS (no API key).

    These are bright meteor events detected by US government sensors,
    with lat/lon of the event.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(
            "https://ssd-api.jpl.nasa.gov/fireball.api",
            params={"date-min": "2020-01-01", "req-loc": "true", "vel-comp": "true"},
        )
        resp.raise_for_status()
        data = resp.json()

    fields = data.get("fields", [])
    rows = data.get("data", [])
    if not fields or not rows:
        return {"synced": 0}

    # Build field index
    fi = {f: i for i, f in enumerate(fields)}

    total = 0
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            for row in rows:
                try:
                    date_str = row[fi.get("date", 0)]
                    lat = row[fi.get("lat", -1)]
                    lon = row[fi.get("lon", -1)]
                    lat_dir = row[fi.get("lat-dir", -1)]
                    lon_dir = row[fi.get("lon-dir", -1)]

                    if lat is None or lon is None:
                        continue

                    lat_f = float(lat) * (-1 if lat_dir == "S" else 1)
                    lon_f = float(lon) * (-1 if lon_dir == "W" else 1)

                    alt = row[fi["alt"]] if "alt" in fi else None
                    vel = row[fi["vel"]] if "vel" in fi else None
                    energy_j = row[fi["energy"]] if "energy" in fi else None
                    impact_e = row[fi["impact-e"]] if "impact-e" in fi else None

                    cur.execute("""
                        INSERT INTO fireballs (
                            date, latitude, longitude, altitude_km,
                            velocity_kms, total_radiated_energy_j, impact_energy_kt,
                            last_synced
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (date, latitude, longitude) DO UPDATE SET
                            altitude_km = COALESCE(EXCLUDED.altitude_km, fireballs.altitude_km),
                            velocity_kms = COALESCE(EXCLUDED.velocity_kms, fireballs.velocity_kms),
                            last_synced = NOW()
                    """, (
                        date_str, lat_f, lon_f,
                        float(alt) if alt else None,
                        float(vel) if vel else None,
                        float(energy_j) * 1e10 if energy_j else None,  # Convert from 10^10 J
                        float(impact_e) if impact_e else None,
                    ))
                    total += 1
                except Exception:
                    pass

        conn.commit()

    logger.info(f"Synced {total} fireballs from NASA CNEOS")
    _update_sync_status("fireballs", total, dsn=dsn)
    return {"synced": total}


# ─── NASA NeoWs (Near-Earth Objects) ─────────────────────────────────────────

async def sync_neows(api_key: str = "DEMO_KEY", dsn: Optional[str] = None) -> dict:
    """Sync near-Earth object close approaches from NASA NeoWs.

    DEMO_KEY works (30 req/hr). Get free key at https://api.nasa.gov/ for 1000 req/hr.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    end = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d")

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(
            "https://api.nasa.gov/neo/rest/v1/feed",
            params={"start_date": today, "end_date": end, "api_key": api_key},
        )
        resp.raise_for_status()
        data = resp.json()

    neo_by_date = data.get("near_earth_objects", {})
    neo_count = 0
    approach_count = 0

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            for date_str, neos in neo_by_date.items():
                for neo in neos:
                    neo_id = str(neo.get("id", ""))
                    if not neo_id:
                        continue

                    diameter = neo.get("estimated_diameter", {}).get("kilometers", {})

                    cur.execute("""
                        INSERT INTO near_earth_objects (
                            neo_id, name, designation, nasa_jpl_url,
                            absolute_magnitude_h,
                            estimated_diameter_min_km, estimated_diameter_max_km,
                            is_potentially_hazardous, is_sentry_object,
                            last_synced
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (neo_id) DO UPDATE SET
                            name = EXCLUDED.name,
                            is_potentially_hazardous = EXCLUDED.is_potentially_hazardous,
                            estimated_diameter_min_km = EXCLUDED.estimated_diameter_min_km,
                            estimated_diameter_max_km = EXCLUDED.estimated_diameter_max_km,
                            last_synced = NOW()
                    """, (
                        neo_id,
                        neo.get("name", "Unknown"),
                        neo.get("designation", ""),
                        neo.get("nasa_jpl_url", ""),
                        neo.get("absolute_magnitude_h"),
                        diameter.get("estimated_diameter_min"),
                        diameter.get("estimated_diameter_max"),
                        neo.get("is_potentially_hazardous_asteroid", False),
                        neo.get("is_sentry_object", False),
                    ))
                    neo_count += 1

                    for ca in neo.get("close_approach_data", []):
                        vel = ca.get("relative_velocity", {})
                        miss = ca.get("miss_distance", {})
                        cur.execute("""
                            INSERT INTO neo_close_approaches (
                                neo_id, close_approach_date,
                                relative_velocity_kms, miss_distance_km,
                                miss_distance_lunar, orbiting_body
                            ) VALUES (%s, %s, %s, %s, %s, %s)
                            ON CONFLICT DO NOTHING
                        """, (
                            neo_id,
                            ca.get("close_approach_date"),
                            float(vel.get("kilometers_per_second", 0)),
                            float(miss.get("kilometers", 0)),
                            float(miss.get("lunar", 0)),
                            ca.get("orbiting_body", "Earth"),
                        ))
                        approach_count += 1

        conn.commit()

    logger.info(f"Synced {neo_count} NEOs with {approach_count} close approaches")
    _update_sync_status("near_earth_objects", neo_count, dsn=dsn)
    return {"synced_neos": neo_count, "synced_approaches": approach_count}


# ─── NASA DONKI (Space Weather) ──────────────────────────────────────────────

async def sync_donki(api_key: str = "DEMO_KEY", dsn: Optional[str] = None) -> dict:
    """Sync space weather events from NASA DONKI.

    Solar flares, CMEs, geomagnetic storms.
    DEMO_KEY works. Free key at https://api.nasa.gov/
    """
    start = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    total = 0
    results = {}

    endpoints = {
        "solar_flare": ("FLR", "flrID"),
        "cme": ("CME", "activityID"),
        "geomagnetic_storm": ("GST", "gstID"),
    }

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                for event_type, (path, id_field) in endpoints.items():
                    try:
                        resp = await client.get(
                            f"https://api.nasa.gov/DONKI/{path}",
                            params={"startDate": start, "api_key": api_key},
                        )
                        if resp.status_code != 200:
                            results[event_type] = f"HTTP {resp.status_code}"
                            continue

                        events = resp.json()
                        if not isinstance(events, list):
                            continue

                        count = 0
                        for event in events:
                            event_id = event.get(id_field, "")
                            if not event_id:
                                continue

                            # Extract class/intensity based on event type
                            class_type = ""
                            intensity = None
                            if event_type == "solar_flare":
                                class_type = event.get("classType", "")
                            elif event_type == "geomagnetic_storm":
                                # Get max Kp from allKpIndex
                                kp_list = event.get("allKpIndex", [])
                                if kp_list:
                                    max_kp = max(ki.get("kpIndex", 0) for ki in kp_list)
                                    class_type = f"Kp{max_kp}"
                                    intensity = float(max_kp)

                            instruments = []
                            for inst in event.get("instruments", []):
                                instruments.append(inst.get("displayName", ""))

                            cur.execute("""
                                INSERT INTO space_weather_events (
                                    event_id, event_type, class_type, intensity,
                                    start_time, peak_time, end_time,
                                    source_location, link, instruments, last_synced
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                                ON CONFLICT (event_id) DO UPDATE SET
                                    class_type = EXCLUDED.class_type,
                                    intensity = EXCLUDED.intensity,
                                    end_time = EXCLUDED.end_time,
                                    last_synced = NOW()
                            """, (
                                event_id, event_type, class_type, intensity,
                                event.get("beginTime") or event.get("startTime"),
                                event.get("peakTime"),
                                event.get("endTime"),
                                event.get("sourceLocation", ""),
                                event.get("link", ""),
                                instruments if instruments else None,
                            ))
                            count += 1
                            total += 1

                        results[event_type] = count

                    except Exception as e:
                        results[event_type] = f"error: {e}"

            conn.commit()

    logger.info(f"Synced {total} space weather events from DONKI")
    _update_sync_status("space_weather", total, dsn=dsn)
    return {"synced": total, "by_type": results}


# ─── Sync All Space Data ─────────────────────────────────────────────────────

async def sync_all_space(dsn: Optional[str] = None) -> dict:
    """Run all space data syncs."""
    results = {}
    for name, fn in [
        ("satellites", lambda: sync_celestrak_tle(dsn)),
        ("fireballs", lambda: sync_fireballs(dsn)),
        ("near_earth_objects", lambda: sync_neows(dsn=dsn)),
        ("space_weather", lambda: sync_donki(dsn=dsn)),
    ]:
        try:
            results[name] = await fn()
        except Exception as e:
            results[name] = {"error": str(e)}
            logger.error(f"Space sync {name} failed: {e}")
    return results
