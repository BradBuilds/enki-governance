"""
Map annotation CRUD — persistent user-drawn features on the map.

Annotations are pins, routes, polygons, and circles saved to the map_annotations table.
Each can link to a project or case. Styled with color, weight, fill opacity.
"""

from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection


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


def list_annotations(
    tenant_id: str,
    project_id: Optional[str] = None,
    case_id: Optional[str] = None,
    west: Optional[float] = None,
    south: Optional[float] = None,
    east: Optional[float] = None,
    north: Optional[float] = None,
    limit: int = 500,
    dsn: Optional[str] = None,
) -> list[dict]:
    """List annotations, optionally filtered by project, case, or bounding box."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            use_postgis = _has_postgis_column(cur, "map_annotations", "geometry")
            geom_expr = "ST_AsGeoJSON(geometry)" if use_postgis else "geometry::text"

            clauses = ["tenant_id = %s"]
            params: list = [tenant_id]

            if project_id:
                clauses.append("project_id = %s")
                params.append(project_id)
            if case_id:
                clauses.append("case_id = %s")
                params.append(case_id)
            if west is not None and south is not None and east is not None and north is not None:
                if use_postgis:
                    clauses.append("ST_Intersects(geometry, ST_MakeEnvelope(%s, %s, %s, %s, 4326))")
                    params.extend([west, south, east, north])
                # Skip bbox filter if no PostGIS (return all, client-side filter)

            params.append(limit)

            cur.execute(f"""
                SELECT annotation_id, project_id, case_id, identity_id,
                       annotation_type, title, description,
                       {geom_expr} as geojson,
                       style, metadata, created_at, updated_at
                FROM map_annotations
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC
                LIMIT %s
            """, params)

            return [_row_to_annotation(r) for r in cur.fetchall()]


def create_annotation(
    tenant_id: str,
    identity_id: str,
    annotation_type: str,
    geometry_geojson: dict,
    title: Optional[str] = None,
    description: Optional[str] = None,
    project_id: Optional[str] = None,
    case_id: Optional[str] = None,
    style: Optional[dict] = None,
    metadata: Optional[dict] = None,
    dsn: Optional[str] = None,
) -> dict:
    """Create a map annotation from GeoJSON geometry."""
    aid = f"ann_{hashlib.sha256(f'{tenant_id}:{datetime.now().isoformat()}'.encode()).hexdigest()[:12]}"

    geojson_str = json.dumps(geometry_geojson)

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            use_postgis = _has_postgis_column(cur, "map_annotations", "geometry")

            if use_postgis:
                geom_val = "ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)"
            else:
                geom_val = "%s::jsonb"

            cur.execute(f"""
                INSERT INTO map_annotations
                    (annotation_id, tenant_id, project_id, case_id, identity_id,
                     annotation_type, title, description,
                     geometry, style, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                        {geom_val}, %s, %s)
                RETURNING annotation_id
            """, (
                aid, tenant_id, project_id, case_id, identity_id,
                annotation_type, title, description,
                geojson_str,
                json.dumps(style or {}),
                json.dumps(metadata or {}),
            ))
            conn.commit()

    return {"annotation_id": aid, "annotation_type": annotation_type, "title": title}


def update_annotation(
    annotation_id: str,
    tenant_id: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    style: Optional[dict] = None,
    dsn: Optional[str] = None,
) -> bool:
    """Update annotation metadata."""
    sets = ["updated_at = NOW()"]
    params: list = []

    if title is not None:
        sets.append("title = %s")
        params.append(title)
    if description is not None:
        sets.append("description = %s")
        params.append(description)
    if style is not None:
        sets.append("style = %s")
        params.append(json.dumps(style))

    params.extend([annotation_id, tenant_id])

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE map_annotations SET {', '.join(sets)}
                WHERE annotation_id = %s AND tenant_id = %s
            """, params)
            ok = cur.rowcount > 0
            conn.commit()
            return ok


def delete_annotation(
    annotation_id: str,
    tenant_id: str,
    dsn: Optional[str] = None,
) -> bool:
    """Delete an annotation."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM map_annotations WHERE annotation_id = %s AND tenant_id = %s
            """, (annotation_id, tenant_id))
            ok = cur.rowcount > 0
            conn.commit()
            return ok


def annotations_to_geojson(annotations: list[dict]) -> dict:
    """Convert to GeoJSON FeatureCollection."""
    features = []
    for a in annotations:
        if not a.get("geometry"):
            continue
        features.append({
            "type": "Feature",
            "geometry": a["geometry"],
            "properties": {
                "annotation_id": a["annotation_id"],
                "annotation_type": a["annotation_type"],
                "title": a.get("title"),
                "description": a.get("description"),
                "project_id": a.get("project_id"),
                "case_id": a.get("case_id"),
                "style": a.get("style", {}),
                "created_at": a.get("created_at"),
            },
        })
    return {"type": "FeatureCollection", "features": features}


# ─── KML/GPX Import ─────────────────────────────────────────────────────────

def parse_gpx(gpx_bytes: bytes) -> list[dict]:
    """Parse GPX file into a list of annotation-ready dicts (waypoints + tracks)."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(gpx_bytes)
    ns = {'': 'http://www.topografix.com/GPX/1/1'}
    # Handle both namespaced and non-namespaced
    if root.tag.startswith('{'):
        ns_prefix = root.tag.split('}')[0] + '}'
    else:
        ns_prefix = ''

    results = []

    # Waypoints → pins
    for wpt in root.iter(f'{ns_prefix}wpt'):
        lat = float(wpt.get('lat', 0))
        lon = float(wpt.get('lon', 0))
        name_el = wpt.find(f'{ns_prefix}name')
        desc_el = wpt.find(f'{ns_prefix}desc')

        results.append({
            "annotation_type": "pin",
            "title": name_el.text if name_el is not None else None,
            "description": desc_el.text if desc_el is not None else None,
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
        })

    # Tracks → routes
    for trk in root.iter(f'{ns_prefix}trk'):
        name_el = trk.find(f'{ns_prefix}name')
        coords = []
        for trkpt in trk.iter(f'{ns_prefix}trkpt'):
            lat = float(trkpt.get('lat', 0))
            lon = float(trkpt.get('lon', 0))
            coords.append([lon, lat])

        if len(coords) >= 2:
            results.append({
                "annotation_type": "route",
                "title": name_el.text if name_el is not None else "Track",
                "geometry": {"type": "LineString", "coordinates": coords},
            })

    # Routes → routes
    for rte in root.iter(f'{ns_prefix}rte'):
        name_el = rte.find(f'{ns_prefix}name')
        coords = []
        for rtept in rte.iter(f'{ns_prefix}rtept'):
            lat = float(rtept.get('lat', 0))
            lon = float(rtept.get('lon', 0))
            coords.append([lon, lat])

        if len(coords) >= 2:
            results.append({
                "annotation_type": "route",
                "title": name_el.text if name_el is not None else "Route",
                "geometry": {"type": "LineString", "coordinates": coords},
            })

    return results


def parse_kml(kml_bytes: bytes) -> list[dict]:
    """Parse KML file into annotation-ready dicts (placemarks → pins/routes/polygons)."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(kml_bytes)
    ns_prefix = ''
    if root.tag.startswith('{'):
        ns_prefix = root.tag.split('}')[0] + '}'

    results = []

    for pm in root.iter(f'{ns_prefix}Placemark'):
        name_el = pm.find(f'{ns_prefix}name')
        desc_el = pm.find(f'{ns_prefix}description')
        title = name_el.text if name_el is not None else None
        description = desc_el.text if desc_el is not None else None

        # Point
        point = pm.find(f'.//{ns_prefix}Point/{ns_prefix}coordinates')
        if point is not None and point.text:
            parts = point.text.strip().split(',')
            lon, lat = float(parts[0]), float(parts[1])
            results.append({
                "annotation_type": "pin",
                "title": title,
                "description": description,
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
            })
            continue

        # LineString
        line = pm.find(f'.//{ns_prefix}LineString/{ns_prefix}coordinates')
        if line is not None and line.text:
            coords = _parse_kml_coords(line.text)
            if len(coords) >= 2:
                results.append({
                    "annotation_type": "route",
                    "title": title,
                    "description": description,
                    "geometry": {"type": "LineString", "coordinates": coords},
                })
            continue

        # Polygon
        outer = pm.find(f'.//{ns_prefix}Polygon/{ns_prefix}outerBoundaryIs/{ns_prefix}LinearRing/{ns_prefix}coordinates')
        if outer is not None and outer.text:
            coords = _parse_kml_coords(outer.text)
            if len(coords) >= 3:
                # Close ring
                if coords[0] != coords[-1]:
                    coords.append(coords[0])
                results.append({
                    "annotation_type": "polygon",
                    "title": title,
                    "description": description,
                    "geometry": {"type": "Polygon", "coordinates": [coords]},
                })

    return results


def _parse_kml_coords(text: str) -> list[list[float]]:
    """Parse KML coordinate string 'lon,lat,alt lon,lat,alt ...' into [[lon, lat], ...]."""
    coords = []
    for part in text.strip().split():
        vals = part.strip().split(',')
        if len(vals) >= 2:
            coords.append([float(vals[0]), float(vals[1])])
    return coords


def _row_to_annotation(r) -> dict:
    geom = r[7]
    if geom and isinstance(geom, str):
        try:
            geom = json.loads(geom)
        except (json.JSONDecodeError, TypeError):
            geom = None
    return {
        "annotation_id": r[0], "project_id": r[1], "case_id": r[2],
        "identity_id": r[3], "annotation_type": r[4],
        "title": r[5], "description": r[6],
        "geometry": geom,
        "style": r[8] if isinstance(r[8], dict) else json.loads(r[8] or "{}"),
        "metadata": r[9] if isinstance(r[9], dict) else json.loads(r[9] or "{}"),
        "created_at": r[10].isoformat() if r[10] else None,
        "updated_at": r[11].isoformat() if r[11] else None,
    }
