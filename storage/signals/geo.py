"""
Geospatial query functions — spatial queries, track assembly, geofence management.

Uses PostGIS for all spatial operations:
  - Bounding box queries (ST_MakeEnvelope)
  - Radius queries (ST_DWithin)
  - Track assembly (ST_MakeLine from ordered points)
  - Geofence CRUD

All functions are tenant-scoped.
"""

from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection


# ─── Signal Spatial Queries ──────────────────────────────────────────────────

def _has_postgis_column(cur, table: str, column: str) -> bool:
    """Check if a PostGIS geometry column exists on a table."""
    try:
        cur.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = %s AND column_name = %s
              AND udt_name IN ('geometry', 'geography')
        """, (table, column))
        return cur.fetchone() is not None
    except Exception:
        return False


def query_signals_bbox(
    tenant_id: str,
    west: float, south: float, east: float, north: float,
    signal_type: Optional[str] = None,
    after: Optional[datetime] = None,
    before: Optional[datetime] = None,
    limit: int = 500,
    dsn: Optional[str] = None,
) -> list[dict]:
    """Query signals within a bounding box. Returns list of signal dicts.
    Falls back to lat/lon filtering if PostGIS location column is unavailable."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            use_postgis = _has_postgis_column(cur, "signals", "location")

            if use_postgis:
                clauses = [
                    "tenant_id = %s",
                    "location IS NOT NULL",
                    "ST_Within(location, ST_MakeEnvelope(%s, %s, %s, %s, 4326))",
                ]
                params: list = [tenant_id, west, south, east, north]
            else:
                clauses = [
                    "tenant_id = %s",
                    "latitude IS NOT NULL AND longitude IS NOT NULL",
                    "longitude >= %s AND latitude >= %s AND longitude <= %s AND latitude <= %s",
                ]
                params = [tenant_id, west, south, east, north]

            if signal_type:
                if signal_type.endswith(".*"):
                    clauses.append("signal_type LIKE %s")
                    params.append(signal_type.replace(".*", ".%"))
                else:
                    clauses.append("signal_type = %s")
                    params.append(signal_type)
            if after:
                clauses.append("timestamp >= %s")
                params.append(after)
            if before:
                clauses.append("timestamp <= %s")
                params.append(before)

            params.append(limit)

            cur.execute(f"""
                SELECT signal_id, signal_type, device_id, severity, confidence,
                       timestamp, latitude, longitude, altitude, accuracy,
                       payload, metadata, rid
                FROM signals
                WHERE {' AND '.join(clauses)}
                ORDER BY timestamp DESC
                LIMIT %s
            """, params)

            return [_row_to_signal(r) for r in cur.fetchall()]


def query_signals_radius(
    tenant_id: str,
    lat: float, lon: float, radius_km: float,
    signal_type: Optional[str] = None,
    after: Optional[datetime] = None,
    before: Optional[datetime] = None,
    limit: int = 500,
    dsn: Optional[str] = None,
) -> list[dict]:
    """Query signals within a radius of a point.
    Falls back to bounding-box approximation if PostGIS is unavailable."""
    radius_m = radius_km * 1000

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            use_postgis = _has_postgis_column(cur, "signals", "location")

            if use_postgis:
                clauses = [
                    "tenant_id = %s",
                    "location IS NOT NULL",
                    "ST_DWithin(location::geography, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)",
                ]
                params: list = [tenant_id, lon, lat, radius_m]
            else:
                # Approximate bbox for radius (1 degree ≈ 111km)
                delta_lat = radius_km / 111.0
                delta_lon = radius_km / (111.0 * max(abs(__import__('math').cos(__import__('math').radians(lat))), 0.01))
                clauses = [
                    "tenant_id = %s",
                    "latitude IS NOT NULL AND longitude IS NOT NULL",
                    "latitude BETWEEN %s AND %s",
                    "longitude BETWEEN %s AND %s",
                ]
                params = [tenant_id, lat - delta_lat, lat + delta_lat, lon - delta_lon, lon + delta_lon]

            if signal_type:
                clauses.append("signal_type = %s")
                params.append(signal_type)
            if after:
                clauses.append("timestamp >= %s")
                params.append(after)
            if before:
                clauses.append("timestamp <= %s")
                params.append(before)

            params.append(limit)

            cur.execute(f"""
                SELECT signal_id, signal_type, device_id, severity, confidence,
                       timestamp, latitude, longitude, altitude, accuracy,
                       payload, metadata, rid
                FROM signals
                WHERE {' AND '.join(clauses)}
                ORDER BY timestamp DESC
                LIMIT %s
            """, params)

            return [_row_to_signal(r) for r in cur.fetchall()]


def _row_to_signal(r) -> dict:
    return {
        "signal_id": r[0], "signal_type": r[1], "device_id": r[2],
        "severity": r[3], "confidence": r[4],
        "timestamp": r[5].isoformat() if r[5] else None,
        "latitude": r[6], "longitude": r[7], "altitude": r[8], "accuracy": r[9],
        "payload": r[10], "metadata": r[11], "rid": r[12],
    }


def signals_to_geojson(signals: list[dict]) -> dict:
    """Convert signal list to GeoJSON FeatureCollection."""
    features = []
    for s in signals:
        if s.get("latitude") is None or s.get("longitude") is None:
            continue
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [s["longitude"], s["latitude"]],
            },
            "properties": {
                "signal_id": s["signal_id"],
                "signal_type": s["signal_type"],
                "device_id": s["device_id"],
                "severity": s["severity"],
                "timestamp": s["timestamp"],
                "latitude": s["latitude"],
                "longitude": s["longitude"],
                "altitude": s.get("altitude"),
            },
        })
    return {"type": "FeatureCollection", "features": features}


# ─── Track Assembly ──────────────────────────────────────────────────────────

def get_entity_positions(
    tenant_id: str,
    entity_id: str,
    after: Optional[datetime] = None,
    before: Optional[datetime] = None,
    dsn: Optional[str] = None,
) -> list[dict]:
    """Get time-ordered positions for an entity (device_id)."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            clauses = [
                "tenant_id = %s",
                "device_id = %s",
                "latitude IS NOT NULL AND longitude IS NOT NULL",
            ]
            params: list = [tenant_id, entity_id]

            if after:
                clauses.append("timestamp >= %s")
                params.append(after)
            if before:
                clauses.append("timestamp <= %s")
                params.append(before)

            cur.execute(f"""
                SELECT signal_id, timestamp, latitude, longitude, altitude,
                       accuracy, rid, payload
                FROM signals
                WHERE {' AND '.join(clauses)}
                ORDER BY timestamp ASC
            """, params)

            return [{
                "signal_id": r[0],
                "timestamp": r[1].isoformat() if r[1] else None,
                "latitude": r[2], "longitude": r[3], "altitude": r[4],
                "accuracy": r[5], "rid": r[6], "payload": r[7],
            } for r in cur.fetchall()]


def assemble_track(
    tenant_id: str,
    entity_id: str,
    start_time: datetime,
    end_time: datetime,
    entity_type: str = "device",
    display_name: Optional[str] = None,
    dsn: Optional[str] = None,
) -> Optional[dict]:
    """Assemble a track from position signals. Creates LineString and stores in tracks table."""
    positions = get_entity_positions(tenant_id, entity_id, start_time, end_time, dsn)

    if len(positions) < 2:
        return None  # need at least 2 points for a line

    signal_rids = [p["rid"] for p in positions]
    track_id = f"trk_{hashlib.sha256(f'{entity_id}:{start_time.isoformat()}'.encode()).hexdigest()[:16]}"

    # Build GeoJSON LineString from positions for JSONB fallback
    line_coords = [[p["longitude"], p["latitude"]] for p in positions
                   if p.get("latitude") is not None and p.get("longitude") is not None]
    geojson_line = json.dumps({
        "type": "LineString",
        "coordinates": line_coords,
    })

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            use_postgis = _has_postgis_column(cur, "tracks", "geometry")

            if use_postgis:
                # Use PostGIS ST_MakeLine for native geometry
                cur.execute("""
                    INSERT INTO tracks (track_id, tenant_id, entity_id, entity_type,
                        display_name, geometry, start_time, end_time, signal_count, signal_rids)
                    VALUES (%s, %s, %s, %s, %s,
                        ST_MakeLine(
                            (SELECT array_agg(
                                COALESCE(location, ST_SetSRID(ST_MakePoint(longitude, latitude), 4326))
                                ORDER BY timestamp ASC)
                             FROM signals
                             WHERE tenant_id = %s AND device_id = %s
                               AND latitude IS NOT NULL AND longitude IS NOT NULL
                               AND timestamp >= %s AND timestamp <= %s)
                        ),
                        %s, %s, %s, %s)
                    ON CONFLICT (track_id) DO UPDATE SET
                        geometry = EXCLUDED.geometry,
                        end_time = EXCLUDED.end_time,
                        signal_count = EXCLUDED.signal_count,
                        signal_rids = EXCLUDED.signal_rids
                    RETURNING track_id
                """, (
                    track_id, tenant_id, entity_id, entity_type,
                    display_name or entity_id,
                    tenant_id, entity_id, start_time, end_time,
                    start_time, end_time, len(positions), signal_rids,
                ))
            else:
                # Store as JSONB GeoJSON when PostGIS is unavailable
                cur.execute("""
                    INSERT INTO tracks (track_id, tenant_id, entity_id, entity_type,
                        display_name, geometry, start_time, end_time, signal_count, signal_rids)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (track_id) DO UPDATE SET
                        geometry = EXCLUDED.geometry,
                        end_time = EXCLUDED.end_time,
                        signal_count = EXCLUDED.signal_count,
                        signal_rids = EXCLUDED.signal_rids
                    RETURNING track_id
                """, (
                    track_id, tenant_id, entity_id, entity_type,
                    display_name or entity_id,
                    geojson_line,
                    start_time, end_time, len(positions), signal_rids,
                ))
            conn.commit()

    return {
        "track_id": track_id,
        "entity_id": entity_id,
        "entity_type": entity_type,
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "signal_count": len(positions),
        "positions": positions,
    }


def list_tracks(
    tenant_id: str,
    limit: int = 100,
    dsn: Optional[str] = None,
) -> list[dict]:
    """List all tracks for a tenant.
    Handles geometry as PostGIS geometry (ST_AsGeoJSON) or JSONB."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            use_postgis = _has_postgis_column(cur, "tracks", "geometry")

            if use_postgis:
                geom_expr = "ST_AsGeoJSON(geometry)"
            else:
                geom_expr = "geometry::text"

            cur.execute(f"""
                SELECT track_id, entity_id, entity_type, display_name,
                       {geom_expr} as geojson,
                       start_time, end_time, signal_count, created_at
                FROM tracks
                WHERE tenant_id = %s
                ORDER BY start_time DESC
                LIMIT %s
            """, (tenant_id, limit))

            results = []
            for r in cur.fetchall():
                geom = r[4]
                if geom and isinstance(geom, str):
                    try:
                        geom = json.loads(geom)
                    except (json.JSONDecodeError, TypeError):
                        geom = None
                elif isinstance(geom, dict):
                    pass  # already a dict (JSONB)
                results.append({
                    "track_id": r[0], "entity_id": r[1], "entity_type": r[2],
                    "display_name": r[3],
                    "geometry": geom,
                    "start_time": r[5].isoformat() if r[5] else None,
                    "end_time": r[6].isoformat() if r[6] else None,
                    "signal_count": r[7],
                    "created_at": r[8].isoformat() if r[8] else None,
                })
            return results


# ─── Geofence CRUD ───────────────────────────────────────────────────────────

def _geofence_geom_expr(cur) -> str:
    """Return the SQL expression for reading geofence geometry."""
    if _has_postgis_column(cur, "geofences", "geometry"):
        return "ST_AsGeoJSON(geometry)"
    return "geometry::text"


def list_geofences(
    tenant_id: str,
    dsn: Optional[str] = None,
) -> list[dict]:
    """List all geofences for a tenant.
    Handles geometry as PostGIS geometry or JSONB."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            geom_expr = _geofence_geom_expr(cur)
            cur.execute(f"""
                SELECT geofence_id, name, description,
                       {geom_expr} as geojson,
                       rule_id, alert_on, color, enabled, metadata,
                       created_by, created_at
                FROM geofences
                WHERE tenant_id = %s
                ORDER BY created_at DESC
            """, (tenant_id,))

            return [_geofence_row(r) for r in cur.fetchall()]


def get_geofence(
    geofence_id: str,
    tenant_id: str,
    dsn: Optional[str] = None,
) -> Optional[dict]:
    """Get a single geofence."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            geom_expr = _geofence_geom_expr(cur)
            cur.execute(f"""
                SELECT geofence_id, name, description,
                       {geom_expr} as geojson,
                       rule_id, alert_on, color, enabled, metadata,
                       created_by, created_at
                FROM geofences
                WHERE geofence_id = %s AND tenant_id = %s
            """, (geofence_id, tenant_id))

            r = cur.fetchone()
            return _geofence_row(r) if r else None


def create_geofence(
    tenant_id: str,
    name: str,
    polygon: list[list[float]],
    created_by: str,
    description: Optional[str] = None,
    alert_on: str = "enter",
    color: str = "#ef4444",
    metadata: Optional[dict] = None,
    dsn: Optional[str] = None,
) -> dict:
    """Create a geofence from a polygon (list of [lon, lat] coordinate pairs).

    The polygon must be closed (first point == last point). If not, it's auto-closed.
    """
    # Ensure polygon is closed
    if polygon[0] != polygon[-1]:
        polygon = polygon + [polygon[0]]

    geofence_id = f"gf_{hashlib.sha256(f'{name}:{tenant_id}'.encode()).hexdigest()[:12]}"

    # Build WKT polygon string
    coords_str = ", ".join(f"{p[0]} {p[1]}" for p in polygon)
    wkt = f"POLYGON(({coords_str}))"

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO geofences (geofence_id, tenant_id, name, description,
                    geometry, alert_on, color, metadata, created_by)
                VALUES (%s, %s, %s, %s, ST_GeomFromText(%s, 4326), %s, %s, %s, %s)
                RETURNING geofence_id
            """, (
                geofence_id, tenant_id, name, description,
                wkt, alert_on, color, json.dumps(metadata or {}), created_by,
            ))
            conn.commit()

    return {"geofence_id": geofence_id, "name": name, "status": "created"}


def update_geofence(
    geofence_id: str,
    tenant_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    polygon: Optional[list[list[float]]] = None,
    alert_on: Optional[str] = None,
    color: Optional[str] = None,
    enabled: Optional[bool] = None,
    dsn: Optional[str] = None,
) -> bool:
    """Update a geofence. Returns True if updated."""
    sets = ["updated_at = NOW()"]
    params: list = []

    if name is not None:
        sets.append("name = %s")
        params.append(name)
    if description is not None:
        sets.append("description = %s")
        params.append(description)
    if polygon is not None:
        if polygon[0] != polygon[-1]:
            polygon = polygon + [polygon[0]]
        coords_str = ", ".join(f"{p[0]} {p[1]}" for p in polygon)
        wkt = f"POLYGON(({coords_str}))"
        sets.append("geometry = ST_GeomFromText(%s, 4326)")
        params.append(wkt)
    if alert_on is not None:
        sets.append("alert_on = %s")
        params.append(alert_on)
    if color is not None:
        sets.append("color = %s")
        params.append(color)
    if enabled is not None:
        sets.append("enabled = %s")
        params.append(enabled)

    params.extend([geofence_id, tenant_id])

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE geofences SET {', '.join(sets)}
                WHERE geofence_id = %s AND tenant_id = %s
            """, params)
            ok = cur.rowcount > 0
            conn.commit()
            return ok


def delete_geofence(
    geofence_id: str,
    tenant_id: str,
    dsn: Optional[str] = None,
) -> bool:
    """Delete a geofence. Returns True if deleted."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM geofences WHERE geofence_id = %s AND tenant_id = %s
            """, (geofence_id, tenant_id))
            ok = cur.rowcount > 0
            conn.commit()
            return ok


def _geofence_row(r) -> dict:
    geom = r[3]
    if geom and isinstance(geom, str):
        try:
            geom = json.loads(geom)
        except (json.JSONDecodeError, TypeError):
            geom = None
    return {
        "geofence_id": r[0], "name": r[1], "description": r[2],
        "geometry": geom,
        "rule_id": r[4], "alert_on": r[5], "color": r[6],
        "enabled": r[7], "metadata": r[8],
        "created_by": r[9],
        "created_at": r[10].isoformat() if r[10] else None,
    }
