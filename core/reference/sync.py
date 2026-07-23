"""
Reference data sync engine.

Bulk-loads static infrastructure data (ports, airports, aircraft registry)
into local PostgreSQL for fast spatial queries. Runs on a schedule.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx

from storage.postgres.db import get_connection

logger = logging.getLogger("enki.reference.sync")
TIMEOUT = 20


async def sync_ports(dsn: Optional[str] = None) -> dict:
    """Sync NGA World Port Index — 2,900+ ports worldwide."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get("https://msi.nga.mil/api/publications/world-port-index?output=json")
        resp.raise_for_status()
        data = resp.json()

    ports = data.get("ports", data) if isinstance(data, dict) else data
    if not isinstance(ports, list):
        return {"error": "unexpected format", "synced": 0}

    count = 0
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            for port in ports:
                try:
                    lat = port.get("ycoord") or port.get("latitude")
                    lon = port.get("xcoord") or port.get("longitude")
                    if lat is None or lon is None:
                        continue
                    lat, lon = float(lat), float(lon)
                    if lat == 0 and lon == 0:
                        continue

                    port_id = str(port.get("indexNumber", port.get("portNumber", "")))
                    name = port.get("portName", port.get("name", "Unknown Port"))

                    cur.execute("""
                        INSERT INTO ports (port_id, name, country, latitude, longitude,
                            harbor_size, harbor_type, shelter, metadata, last_synced)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (port_id) DO UPDATE SET
                            name = EXCLUDED.name, latitude = EXCLUDED.latitude,
                            longitude = EXCLUDED.longitude, metadata = EXCLUDED.metadata,
                            last_synced = NOW()
                    """, (
                        port_id, name,
                        port.get("countryName", port.get("country", "")),
                        lat, lon,
                        port.get("harborSize", ""),
                        port.get("harborType", ""),
                        port.get("shelter", ""),
                        __import__('json').dumps({k: v for k, v in port.items()
                            if k in ('dnc', 'dodWaterBody', 's57Enc', 'regionName',
                                     'alternateName', 'entranceWidth', 'channelDepth',
                                     'cargoPierDepth', 'anchorageDepth', 'maxVesselLength')}),
                    ))
                    count += 1
                except Exception:
                    pass
        conn.commit()

    _update_sync_status("ports", count, dsn=dsn)
    return {"synced": count}


async def sync_airports(dsn: Optional[str] = None) -> dict:
    """Sync global airports from OurAirports CSV (75,000+ worldwide).

    Falls back to FAA ArcGIS for US-only data if OurAirports is unreachable.
    Data is stored locally in PostgreSQL for fast spatial queries.
    """
    import csv
    import io

    total = 0

    # ── Primary: OurAirports global data ──────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get("https://davidmegginson.github.io/ourairports-data/airports.csv")
            resp.raise_for_status()

        reader = csv.DictReader(io.StringIO(resp.text))
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                for row in reader:
                    lat = row.get("latitude_deg", "")
                    lon = row.get("longitude_deg", "")
                    if not lat or not lon:
                        continue
                    try:
                        lat, lon = float(lat), float(lon)
                    except ValueError:
                        continue
                    if lat == 0 and lon == 0:
                        continue

                    ident = row.get("ident", "").strip()
                    if not ident:
                        continue

                    # Map OurAirports type to our schema
                    oa_type = row.get("type", "").strip()
                    scheduled = row.get("scheduled_service", "").strip().lower() == "yes"

                    cur.execute("""
                        INSERT INTO airports (airport_id, icao, name, city, country,
                            latitude, longitude, elevation_ft, type, military,
                            scheduled_service, source, last_synced)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s, 'ourairports', NOW())
                        ON CONFLICT (airport_id) DO UPDATE SET
                            icao = COALESCE(NULLIF(EXCLUDED.icao, ''), airports.icao),
                            name = EXCLUDED.name,
                            city = COALESCE(EXCLUDED.city, airports.city),
                            country = EXCLUDED.country,
                            latitude = EXCLUDED.latitude,
                            longitude = EXCLUDED.longitude,
                            elevation_ft = COALESCE(EXCLUDED.elevation_ft, airports.elevation_ft),
                            type = EXCLUDED.type,
                            scheduled_service = EXCLUDED.scheduled_service,
                            source = 'ourairports',
                            last_synced = NOW()
                    """, (
                        ident,
                        row.get("gps_code", "") or row.get("local_code", ""),
                        row.get("name", "Unknown"),
                        row.get("municipality", ""),
                        row.get("iso_country", ""),
                        lat, lon,
                        _safe_int(row.get("elevation_ft")),
                        oa_type,
                        scheduled,
                    ))
                    total += 1

                conn.commit()

        logger.info(f"Synced {total} airports from OurAirports (global)")
        _update_sync_status("airports", total, dsn=dsn)
        return {"synced": total, "source": "ourairports"}

    except Exception as e:
        logger.warning(f"OurAirports sync failed ({e}), falling back to FAA")

    # ── Fallback: FAA ArcGIS (US only) ───────────────────────────────────
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        total = 0
        offset = 0
        batch_size = 2000

        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                while True:
                    resp = await client.get(
                        "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/US_Airport/FeatureServer/0/query",
                        params={
                            "where": "1=1",
                            "outFields": "*",
                            "f": "json",
                            "resultOffset": offset,
                            "resultRecordCount": batch_size,
                        },
                    )
                    if resp.status_code != 200:
                        break

                    data = resp.json()
                    features = data.get("features", [])
                    if not features:
                        break

                    for f in features:
                        a = f.get("attributes", {})
                        lat_str = a.get("LATITUDE", "")
                        lon_str = a.get("LONGITUDE", "")

                        lat = _parse_dms(lat_str)
                        lon = _parse_dms(lon_str)
                        if lat is None or lon is None:
                            continue

                        ident = a.get("IDENT", "")
                        if not ident:
                            continue

                        cur.execute("""
                            INSERT INTO airports (airport_id, icao, name, city, country,
                                latitude, longitude, elevation_ft, type, military, source, last_synced)
                            VALUES (%s, %s, %s, %s, 'US', %s, %s, %s, %s, %s, 'faa', NOW())
                            ON CONFLICT (airport_id) DO UPDATE SET
                                name = EXCLUDED.name, latitude = EXCLUDED.latitude,
                                longitude = EXCLUDED.longitude, last_synced = NOW()
                        """, (
                            ident,
                            a.get("ICAO_ID", ""),
                            a.get("NAME", "Unknown"),
                            a.get("SERVCITY", a.get("CITY", "")),
                            lat, lon,
                            _safe_int(a.get("ELEVATION")),
                            a.get("TYPE_CODE", ""),
                            str(a.get("OWNER_TYPE", "")).upper() == "MA",
                        ))
                        total += 1

                    offset += len(features)
                    if not data.get("exceededTransferLimit", False):
                        break

                conn.commit()

    _update_sync_status("airports", total, dsn=dsn)
    return {"synced": total, "source": "faa_fallback"}


def _parse_dms(s: str) -> Optional[float]:
    """Parse DMS coordinate like '29-55-00.8650N' to decimal degrees."""
    if not s or not isinstance(s, str):
        return None
    try:
        return float(s)
    except ValueError:
        pass

    m = re.match(r'(\d+)-(\d+)-([\d.]+)([NSEW])', str(s).strip())
    if not m:
        return None
    deg = int(m.group(1))
    mins = int(m.group(2))
    secs = float(m.group(3))
    direction = m.group(4)
    dd = deg + mins / 60 + secs / 3600
    if direction in ('S', 'W'):
        dd = -dd
    return round(dd, 6)


def _safe_int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(float(str(v).replace(',', '')))
    except (ValueError, TypeError):
        return None


def _update_sync_status(dataset: str, count: int, error: str = None, dsn=None):
    """Update the sync status tracker."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE reference_sync_status SET
                    last_synced = NOW(),
                    next_due = NOW() + (sync_interval_hours * interval '1 hour'),
                    item_count = %s,
                    last_error = %s,
                    status = %s
                WHERE dataset = %s
            """, (count, error, 'error' if error else 'synced', dataset))
        conn.commit()


async def sync_aircraft_registry(dsn: Optional[str] = None) -> dict:
    """Populate aircraft registry from our own ADS-B observations.

    The FAA bulk download is blocked by CDN. Instead, we build the registry
    from aircraft we've actually observed — their tail numbers and types
    are in the ADS-B signal payloads and hot cache.
    """
    count = 0
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Populate from live_cache (most recent observations)
            cur.execute("""
                INSERT INTO aircraft_registry (n_number, model, last_synced)
                SELECT DISTINCT
                    upper(trim(payload->>'r')),
                    payload->>'t',
                    NOW()
                FROM live_cache
                WHERE source = 'adsb'
                  AND payload->>'r' IS NOT NULL
                  AND trim(payload->>'r') != ''
                ON CONFLICT (n_number) DO UPDATE SET
                    model = COALESCE(EXCLUDED.model, aircraft_registry.model),
                    last_synced = NOW()
            """)
            count += cur.rowcount

            # Also populate from ingested signals
            cur.execute("""
                INSERT INTO aircraft_registry (n_number, model, last_synced)
                SELECT DISTINCT
                    upper(trim(payload->>'r')),
                    payload->>'t',
                    NOW()
                FROM signals
                WHERE signal_type = 'signal.geospatial.aircraft_position'
                  AND payload->>'r' IS NOT NULL
                  AND trim(payload->>'r') != ''
                ON CONFLICT (n_number) DO UPDATE SET
                    model = COALESCE(EXCLUDED.model, aircraft_registry.model),
                    last_synced = NOW()
            """)
            count += cur.rowcount

            # Also from live_history
            cur.execute("""
                INSERT INTO aircraft_registry (n_number, model, last_synced)
                SELECT DISTINCT
                    upper(trim(payload_summary->>'r')),
                    payload_summary->>'t',
                    NOW()
                FROM live_history
                WHERE source = 'adsb'
                  AND payload_summary->>'r' IS NOT NULL
                  AND trim(payload_summary->>'r') != ''
                ON CONFLICT (n_number) DO UPDATE SET
                    model = COALESCE(EXCLUDED.model, aircraft_registry.model),
                    last_synced = NOW()
            """)
            count += cur.rowcount

        conn.commit()

    # Get actual count in registry
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM aircraft_registry")
            total = cur.fetchone()[0]

    _update_sync_status("aircraft_registry", total, dsn=dsn)
    return {"synced": total, "new_from_observations": count}


async def sync_all(dsn: Optional[str] = None) -> dict:
    """Run all reference data syncs."""
    results = {}

    # Import camera sync
    from services.api.src.routes.cameras import sync_cameras

    for name, fn in [
        ("cameras", sync_cameras),
        ("ports", lambda: sync_ports(dsn)),
        ("airports", lambda: sync_airports(dsn)),
    ]:
        try:
            result = await fn()
            results[name] = result
        except Exception as e:
            results[name] = {"error": str(e)}
            _update_sync_status(name, 0, error=str(e), dsn=dsn)

    return results


def get_sync_status(dsn: Optional[str] = None) -> list[dict]:
    """Get sync status for all reference datasets."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT dataset, display_name, sync_interval_hours,
                       last_synced, next_due, item_count, last_error, status
                FROM reference_sync_status
                ORDER BY display_name
            """)
            return [{
                "dataset": r[0], "display_name": r[1],
                "sync_interval_hours": r[2],
                "last_synced": r[3].isoformat() if r[3] else None,
                "next_due": r[4].isoformat() if r[4] else None,
                "item_count": r[5], "last_error": r[6], "status": r[7],
            } for r in cur.fetchall()]
