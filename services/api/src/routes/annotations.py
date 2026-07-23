"""
Map annotation + import endpoints.

GET    /map/annotations         — List annotations (filtered by project/case/bbox)
POST   /map/annotations         — Create annotation from drawn feature
DELETE /map/annotations/{id}    — Delete annotation
POST   /map/import              — Import KML or GPX file → annotations
"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, File, Header, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

from storage.map.annotations import (
    list_annotations, create_annotation, delete_annotation,
    annotations_to_geojson, parse_gpx, parse_kml,
)

router = APIRouter(prefix="/map", tags=["map"])


@router.get("/annotations")
async def get_annotations(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    project_id: Optional[str] = Query(None),
    case_id: Optional[str] = Query(None),
    west: Optional[float] = Query(None),
    south: Optional[float] = Query(None),
    east: Optional[float] = Query(None),
    north: Optional[float] = Query(None),
    limit: int = Query(500, le=2000),
):
    """List annotations as GeoJSON FeatureCollection."""
    anns = list_annotations(
        tenant_id=auth.tenant_id,
        project_id=project_id, case_id=case_id,
        west=west, south=south, east=east, north=north,
        limit=limit,
    )
    return annotations_to_geojson(anns)


@router.post("/annotations")
async def create_map_annotation(
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """
    Create a map annotation from a drawn feature.

    Body: {
        "annotation_type": "pin" | "route" | "polygon" | "circle",
        "geometry": { GeoJSON geometry object },
        "title": "My Route",
        "description": "Path from camp to site",
        "project_id": "proj_123",
        "style": {"color": "#3b82f6", "weight": 3}
    }
    """
    body = await request.json()

    geom = body.get("geometry")
    ann_type = body.get("annotation_type")

    if not geom or not ann_type:
        return JSONResponse(status_code=400, content={
            "error": "ANNOTATION.MISSING_FIELDS",
            "detail": "geometry and annotation_type are required"
        })

    result = create_annotation(
        tenant_id=auth.tenant_id,
        identity_id=auth.subject_id,
        annotation_type=ann_type,
        geometry_geojson=geom,
        title=body.get("title"),
        description=body.get("description"),
        project_id=body.get("project_id"),
        case_id=body.get("case_id"),
        style=body.get("style"),
        metadata=body.get("metadata"),
    )

    return JSONResponse(status_code=201, content=result)


@router.delete("/annotations/{annotation_id}")
async def delete_map_annotation(
    annotation_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Delete a map annotation."""
    ok = delete_annotation(annotation_id, auth.tenant_id)
    if not ok:
        return JSONResponse(status_code=404, content={
            "error": "ANNOTATION.NOT_FOUND",
            "detail": f"Annotation {annotation_id} not found"
        })
    return {"status": "deleted", "annotation_id": annotation_id}


@router.post("/import")
async def import_geo_file(
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """
    Import a KML or GPX file. Creates annotations for each feature.

    Body: {
        "filename": "field_data.gpx",
        "content": "<base64 or raw text of the file>",
        "project_id": "proj_123"
    }

    Or use multipart/form-data with file upload.
    """
    content_type = request.headers.get("content-type", "")

    if "multipart" in content_type:
        form = await request.form()
        file = form.get("file")
        project_id = form.get("project_id")
        if not file:
            return JSONResponse(status_code=400, content={
                "error": "IMPORT.NO_FILE", "detail": "file is required"
            })
        filename = file.filename or ""
        file_bytes = await file.read()
    else:
        body = await request.json()
        filename = body.get("filename", "")
        content = body.get("content", "")
        project_id = body.get("project_id")
        file_bytes = content.encode("utf-8") if isinstance(content, str) else content

    if not file_bytes:
        return JSONResponse(status_code=400, content={
            "error": "IMPORT.EMPTY_FILE", "detail": "File is empty"
        })

    # Detect format
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    text_start = file_bytes[:200].decode("utf-8", errors="replace").strip().lower()

    if ext == "gpx" or "<gpx" in text_start:
        features = parse_gpx(file_bytes)
        fmt = "gpx"
    elif ext in ("kml", "kmz") or "<kml" in text_start:
        features = parse_kml(file_bytes)
        fmt = "kml"
    else:
        return JSONResponse(status_code=400, content={
            "error": "IMPORT.UNSUPPORTED_FORMAT",
            "detail": f"Unsupported format. Use .gpx or .kml. Got: {ext}"
        })

    # Create annotations for each parsed feature
    created = []
    for feat in features:
        result = create_annotation(
            tenant_id=auth.tenant_id,
            identity_id=auth.subject_id,
            annotation_type=feat["annotation_type"],
            geometry_geojson=feat["geometry"],
            title=feat.get("title"),
            description=feat.get("description"),
            project_id=project_id,
            style={"color": "#3b82f6", "weight": 2},
        )
        created.append(result)

    return {
        "status": "imported",
        "format": fmt,
        "features_imported": len(created),
        "annotations": created,
    }
