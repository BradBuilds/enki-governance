"""
Geo-knowledge endpoints — knowledge nodes on the map.

GET   /knowledge/geo              — GeoJSON of geo-located nodes
POST  /knowledge/geo              — Create node at location (right-click → Add Observation)
POST  /knowledge/geo/{node_id}/images — Add image to node
PATCH /knowledge/geo/{node_id}    — Update node content/location
"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

from storage.knowledge.geo import (
    query_nodes_bbox, nodes_to_geojson, create_geo_node,
    add_image_to_node, update_node_geo,
)
from storage.map.photo import extract_gps_from_exif

router = APIRouter(prefix="/knowledge/geo", tags=["knowledge-geo"])


@router.get("")
async def query_geo_nodes(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    project_id: Optional[str] = Query(None),
    west: Optional[float] = Query(None),
    south: Optional[float] = Query(None),
    east: Optional[float] = Query(None),
    north: Optional[float] = Query(None),
    epistemic_type: Optional[str] = Query(None),
    entity_type: Optional[str] = Query(None),
    after: Optional[str] = Query(None),
    before: Optional[str] = Query(None),
    limit: int = Query(500, le=2000),
):
    """GeoJSON FeatureCollection of geo-located knowledge nodes.

    Filters:
      entity_type — JOIN entities.canonical_type (person, country, organization, …)
      after/before — ISO-8601, bound knowledge_nodes.created_at
    """
    nodes = query_nodes_bbox(
        tenant_id=auth.tenant_id,
        project_id=project_id,
        west=west or -180, south=south or -90,
        east=east or 180, north=north or 90,
        epistemic_type=epistemic_type,
        entity_type=entity_type,
        after=after, before=before,
        limit=limit,
    )
    return nodes_to_geojson(nodes)


@router.post("")
async def create_geo_knowledge_node(
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """
    Create a knowledge node at a geographic location.

    Body: {
        "project_id": "proj_123",
        "title": "Gobekli Tepe Observation",
        "latitude": 37.2235,
        "longitude": 35.2345,
        "description": "Stone T-shaped pillars with animal carvings",
        "epistemic_type": "observation",
        "concepts": ["neolithic", "megalith"],
        "images": [{"url": "https://...", "caption": "Enclosure D"}]
    }
    """
    body = await request.json()

    project_id = body.get("project_id")
    title = body.get("title")
    latitude = body.get("latitude")
    longitude = body.get("longitude")

    if not project_id:
        return JSONResponse(status_code=400, content={
            "error": "NODE.MISSING_PROJECT", "detail": "project_id is required"
        })
    if not title:
        return JSONResponse(status_code=400, content={
            "error": "NODE.MISSING_TITLE", "detail": "title is required"
        })
    if latitude is None or longitude is None:
        return JSONResponse(status_code=400, content={
            "error": "NODE.MISSING_LOCATION", "detail": "latitude and longitude are required"
        })

    node = create_geo_node(
        project_id=project_id,
        tenant_id=auth.tenant_id,
        title=title,
        latitude=latitude,
        longitude=longitude,
        description=body.get("description"),
        epistemic_type=body.get("epistemic_type", "observation"),
        concepts=body.get("concepts"),
        images=body.get("images"),
        parent_node_id=body.get("parent_node_id"),
        source_authority=body.get("source_authority", "unverified"),
    )

    if not node:
        return JSONResponse(status_code=400, content={
            "error": "NODE.CREATION_FAILED",
            "detail": "Could not create node (check project exists and depth limit)"
        })

    return JSONResponse(status_code=201, content=node)


@router.post("/{node_id}/images")
async def add_node_image(
    node_id: str,
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Add an image to a knowledge node.

    Body: {"url": "https://...", "caption": "Description"}
    """
    body = await request.json()
    url = body.get("url")

    if not url:
        return JSONResponse(status_code=400, content={
            "error": "IMAGE.MISSING_URL", "detail": "url is required"
        })

    ok = add_image_to_node(
        node_id=node_id,
        tenant_id=auth.tenant_id,
        url=url,
        caption=body.get("caption"),
    )

    if not ok:
        return JSONResponse(status_code=404, content={
            "error": "NODE.NOT_FOUND", "detail": f"Node {node_id} not found"
        })

    return {"status": "image_added", "node_id": node_id}


@router.patch("/{node_id}")
async def update_node(
    node_id: str,
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Update a knowledge node's content or location.

    Body: any of {title, description, latitude, longitude, epistemic_type, concepts}
    """
    body = await request.json()

    ok = update_node_geo(
        node_id=node_id,
        tenant_id=auth.tenant_id,
        title=body.get("title"),
        description=body.get("description"),
        latitude=body.get("latitude"),
        longitude=body.get("longitude"),
        epistemic_type=body.get("epistemic_type"),
        concepts=body.get("concepts"),
    )

    if not ok:
        return JSONResponse(status_code=404, content={
            "error": "NODE.NOT_FOUND", "detail": f"Node {node_id} not found"
        })

    return {"status": "updated", "node_id": node_id}


@router.post("/photo")
async def upload_geotagged_photo(
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """
    Upload a photo with EXIF GPS data. Extracts coordinates and creates
    a knowledge node at that location with the image attached.

    Accepts multipart/form-data with:
      - file: the image file (JPEG with EXIF GPS)
      - project_id: target project
      - title: optional title (defaults to filename)
      - epistemic_type: optional (defaults to "observation")

    Or JSON with base64 image:
      {"image_base64": "...", "filename": "...", "project_id": "..."}
    """
    import base64

    content_type = request.headers.get("content-type", "")

    if "multipart" in content_type:
        form = await request.form()
        file = form.get("file")
        project_id = str(form.get("project_id", ""))
        title = str(form.get("title", "")) or None
        epistemic_type = str(form.get("epistemic_type", "observation"))

        if not file:
            return JSONResponse(status_code=400, content={
                "error": "PHOTO.NO_FILE", "detail": "file is required"
            })
        filename = file.filename or "photo.jpg"
        image_bytes = await file.read()
    else:
        body = await request.json()
        image_b64 = body.get("image_base64", "")
        image_bytes = base64.b64decode(image_b64) if image_b64 else b""
        project_id = body.get("project_id", "")
        title = body.get("title")
        filename = body.get("filename", "photo.jpg")
        epistemic_type = body.get("epistemic_type", "observation")

    if not image_bytes:
        return JSONResponse(status_code=400, content={
            "error": "PHOTO.EMPTY", "detail": "Image data is empty"
        })

    if not project_id:
        return JSONResponse(status_code=400, content={
            "error": "PHOTO.NO_PROJECT", "detail": "project_id is required"
        })

    # Extract GPS from EXIF
    gps = extract_gps_from_exif(image_bytes)
    if not gps:
        return JSONResponse(status_code=422, content={
            "error": "PHOTO.NO_GPS",
            "detail": "No GPS coordinates found in image EXIF data. "
                      "Ensure the photo was taken with location services enabled."
        })

    # Create knowledge node at extracted location
    node = create_geo_node(
        project_id=project_id,
        tenant_id=auth.tenant_id,
        title=title or filename.rsplit(".", 1)[0].replace("_", " ").replace("-", " ").title(),
        latitude=gps["latitude"],
        longitude=gps["longitude"],
        description=f"Photo taken at {gps['latitude']:.6f}, {gps['longitude']:.6f}"
                    + (f" · Alt: {gps['altitude']:.0f}m" if gps.get('altitude') else "")
                    + (f" · {gps['timestamp']}" if gps.get('timestamp') else ""),
        epistemic_type=epistemic_type,
        concepts=["photo", "field-observation"],
    )

    if not node:
        return JSONResponse(status_code=500, content={
            "error": "PHOTO.NODE_FAILED", "detail": "Failed to create knowledge node"
        })

    return JSONResponse(status_code=201, content={
        "status": "photo_placed",
        "node_id": node["node_id"],
        "latitude": gps["latitude"],
        "longitude": gps["longitude"],
        "altitude": gps.get("altitude"),
        "timestamp": gps.get("timestamp"),
        "project_id": project_id,
    })
