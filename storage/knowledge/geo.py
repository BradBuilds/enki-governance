"""
Geo-knowledge queries — spatial queries for knowledge nodes on the map.

Knowledge nodes can have location via:
  1. Explicit lat/lon (map-placed observations)
  2. Linked signal_rid (inherits signal's location)

This module handles direct geo-located nodes (case 1).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection


def query_nodes_bbox(
    tenant_id: str,
    project_id: Optional[str] = None,
    west: float = -180, south: float = -90,
    east: float = 180, north: float = 90,
    epistemic_type: Optional[str] = None,
    entity_type: Optional[str] = None,
    after: Optional[str] = None,
    before: Optional[str] = None,
    limit: int = 500,
    dsn: Optional[str] = None,
) -> list[dict]:
    """Query knowledge nodes within a bounding box.

    Optional filters:
      entity_type  — canonical_type on the linked entity (person, country, …)
      after/before — ISO-8601 timestamps; bound knowledge_nodes.created_at
    """
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Use lat/lon filtering (works with or without PostGIS).
            # LEFT JOIN entities so we can return the canonical type alongside.
            clauses = [
                "kn.tenant_id = %s",
                "kn.latitude IS NOT NULL AND kn.longitude IS NOT NULL",
                "kn.longitude >= %s AND kn.latitude >= %s AND kn.longitude <= %s AND kn.latitude <= %s",
            ]
            params: list = [tenant_id, west, south, east, north]

            if project_id:
                clauses.append("kn.project_id = %s")
                params.append(project_id)
            if epistemic_type:
                clauses.append("kn.epistemic_type = %s")
                params.append(epistemic_type)
            if entity_type:
                clauses.append("e.canonical_type = %s")
                params.append(entity_type)
            if after:
                clauses.append("kn.created_at >= %s")
                params.append(after)
            if before:
                clauses.append("kn.created_at <= %s")
                params.append(before)

            params.append(limit)

            cur.execute(f"""
                SELECT kn.node_id, kn.project_id, kn.title, kn.description,
                       kn.epistemic_type, kn.concepts, kn.source_authority,
                       kn.latitude, kn.longitude, kn.images, kn.ai_enriched,
                       kn.signal_rid, kn.depth, kn.created_at,
                       kn.master_entity_id, e.canonical_type, e.canonical_name
                FROM knowledge_nodes kn
                LEFT JOIN entities e ON e.entity_id = kn.master_entity_id
                WHERE {' AND '.join(clauses)}
                ORDER BY kn.created_at DESC
                LIMIT %s
            """, params)

            return [_row_to_node(r) for r in cur.fetchall()]


def _row_to_node(r) -> dict:
    return {
        "node_id": r[0], "project_id": r[1], "title": r[2],
        "description": r[3], "epistemic_type": r[4],
        "concepts": r[5] or [], "source_authority": r[6],
        "latitude": r[7], "longitude": r[8],
        "images": r[9] if isinstance(r[9], list) else json.loads(r[9] or "[]"),
        "ai_enriched": r[10], "signal_rid": r[11], "depth": r[12],
        "created_at": r[13].isoformat() if r[13] else None,
        "entity_id": r[14], "entity_type": r[15], "entity_name": r[16],
    }


def nodes_to_geojson(nodes: list[dict]) -> dict:
    """Convert node list to GeoJSON FeatureCollection."""
    features = []
    for n in nodes:
        if n.get("latitude") is None or n.get("longitude") is None:
            continue
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [n["longitude"], n["latitude"]],
            },
            "properties": {
                "node_id": n["node_id"],
                "project_id": n["project_id"],
                "title": n["title"],
                "description": n.get("description"),
                "epistemic_type": n["epistemic_type"],
                "concepts": n.get("concepts", []),
                "source_authority": n.get("source_authority"),
                "images": n.get("images", []),
                "latitude": n["latitude"],
                "longitude": n["longitude"],
                "ai_enriched": n.get("ai_enriched", False),
                "signal_rid": n.get("signal_rid"),
                "created_at": n.get("created_at"),
                "entity_id": n.get("entity_id"),
                "entity_type": n.get("entity_type"),
                "entity_name": n.get("entity_name"),
            },
        })
    return {"type": "FeatureCollection", "features": features}


def create_geo_node(
    project_id: str,
    tenant_id: str,
    title: str,
    latitude: float,
    longitude: float,
    description: Optional[str] = None,
    epistemic_type: str = "observation",
    concepts: Optional[list[str]] = None,
    images: Optional[list[dict]] = None,
    parent_node_id: Optional[str] = None,
    source_authority: str = "unverified",
    dsn: Optional[str] = None,
) -> Optional[dict]:
    """Create a knowledge node at a geographic location.

    Uses core create_node for standard creation + depth enforcement,
    then sets lat/lon/location/images via UPDATE.
    """
    from core.knowledge.graph import create_node

    node = create_node(
        project_id=project_id,
        tenant_id=tenant_id,
        title=title,
        description=description,
        epistemic_type=epistemic_type,
        concepts=concepts,
        parent_node_id=parent_node_id,
        source_authority=source_authority,
        dsn=dsn,
    )

    if not node:
        return None

    node_id = node["node_id"]

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE knowledge_nodes
                SET latitude = %s, longitude = %s,
                    location = ST_SetSRID(ST_MakePoint(%s, %s), 4326),
                    images = %s
                WHERE node_id = %s AND tenant_id = %s
            """, (
                latitude, longitude,
                longitude, latitude,
                json.dumps(images or []),
                node_id, tenant_id,
            ))
            conn.commit()

    node["latitude"] = latitude
    node["longitude"] = longitude
    node["images"] = images or []
    return node


def add_image_to_node(
    node_id: str,
    tenant_id: str,
    url: str,
    caption: Optional[str] = None,
    dsn: Optional[str] = None,
) -> bool:
    """Append an image to a node's images JSONB array."""
    image_entry = json.dumps({"url": url, "caption": caption or ""})

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE knowledge_nodes
                SET images = COALESCE(images, '[]'::jsonb) || %s::jsonb,
                    updated_at = NOW()
                WHERE node_id = %s AND tenant_id = %s
            """, (f"[{image_entry}]", node_id, tenant_id))
            ok = cur.rowcount > 0
            conn.commit()
            return ok


def update_node_geo(
    node_id: str,
    tenant_id: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    epistemic_type: Optional[str] = None,
    concepts: Optional[list[str]] = None,
    dsn: Optional[str] = None,
) -> bool:
    """Update a knowledge node's geo/content fields."""
    sets = ["updated_at = NOW()"]
    params: list = []

    if title is not None:
        sets.append("title = %s")
        params.append(title)
    if description is not None:
        sets.append("description = %s")
        params.append(description)
    if latitude is not None and longitude is not None:
        sets.append("latitude = %s")
        params.append(latitude)
        sets.append("longitude = %s")
        params.append(longitude)
        sets.append("location = ST_SetSRID(ST_MakePoint(%s, %s), 4326)")
        params.extend([longitude, latitude])
    if epistemic_type is not None:
        sets.append("epistemic_type = %s")
        params.append(epistemic_type)
    if concepts is not None:
        sets.append("concepts = %s")
        params.append(concepts)

    params.extend([node_id, tenant_id])

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE knowledge_nodes SET {', '.join(sets)}
                WHERE node_id = %s AND tenant_id = %s
            """, params)
            ok = cur.rowcount > 0
            conn.commit()
            return ok
