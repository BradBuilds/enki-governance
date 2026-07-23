"""
Gallery, content serving, view tracking, and collections API.

GET    /gallery                    Browse content (images, docs, articles)
GET    /gallery/unseen             Count of unseen items
GET    /signals/{id}/content       Raw content bytes (image, PDF, etc.)
POST   /signals/{id}/view          Mark as seen
POST   /signals/{id}/download      Download + record access
POST   /collections                Create collection/board
GET    /collections                List user's collections
POST   /collections/{id}/pin       Pin a signal to collection
GET    /collections/{id}/items     Get collection items
"""

from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse, Response
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from pydantic import BaseModel

from storage.signals.content import (
    get_signal_content, get_gallery, mark_signal_viewed,
    mark_signal_downloaded, get_unseen_count,
    create_collection, list_collections, pin_to_collection,
    get_collection_items,
)

router = APIRouter(tags=["gallery & content"])


# ─── Gallery ──────────────────────────────────────────────────────────────────

@router.get("/gallery")
async def gallery_endpoint(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    content_type: Optional[str] = Query(None),
    unseen_only: bool = Query(False),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
):
    """Browse content: images, documents, articles, media. Includes seen/unseen status."""
    return get_gallery(
        auth.tenant_id, identity_id=auth.subject_id,
        content_type=content_type, unseen_only=unseen_only,
        limit=limit, offset=offset,
    )


@router.get("/gallery/unseen")
async def unseen_count_endpoint(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Count of signals this user hasn't viewed."""
    count = get_unseen_count(auth.tenant_id, auth.subject_id)
    return {"unseen_count": count}


# ─── Content Serving ──────────────────────────────────────────────────────────

@router.get("/signals/{signal_id}/content")
async def serve_content(
    signal_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Serve raw content bytes (image, PDF, etc.) from LDU-T."""
    content = get_signal_content(signal_id, auth.tenant_id)
    if not content:
        return JSONResponse(status_code=404, content={"error": "CONTENT.NOT_FOUND"})

    return Response(
        content=content["raw_bytes"],
        media_type=content["content_type"],
        headers={
            "Content-Length": str(content["byte_length"]),
            "X-Content-SHA256": content["sha256"],
            "X-Signal-Type": content["signal_type"],
            "Cache-Control": "private, max-age=3600",
        },
    )


# ─── View Tracking ────────────────────────────────────────────────────────────

@router.post("/signals/{signal_id}/view")
async def mark_viewed(
    signal_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Mark a signal as viewed by this user."""
    # Need to get the signal RID from signal_id
    from storage.signals.store import get_signal
    signal = get_signal(signal_id, auth.tenant_id)
    if not signal:
        return JSONResponse(status_code=404, content={"error": "SIGNAL.NOT_FOUND"})

    mark_signal_viewed(auth.tenant_id, auth.subject_id, signal["rid"], "gallery")
    return {"signal_id": signal_id, "viewed": True}


@router.post("/signals/{signal_id}/download")
async def record_download(
    signal_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Record that a user downloaded raw content (audit trail)."""
    from storage.signals.store import get_signal
    signal = get_signal(signal_id, auth.tenant_id)
    if not signal:
        return JSONResponse(status_code=404, content={"error": "SIGNAL.NOT_FOUND"})

    mark_signal_downloaded(auth.tenant_id, auth.subject_id, signal["rid"])
    return {"signal_id": signal_id, "downloaded": True}


# ─── Collections ───────────────────────────────────────────────────────────────

class CreateCollectionRequest(BaseModel):
    name: str
    description: Optional[str] = None
    is_public: bool = False


class PinRequest(BaseModel):
    signal_rid: int
    notes: Optional[str] = None


@router.post("/collections")
async def create_collection_endpoint(
    body: CreateCollectionRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    result = create_collection(
        auth.tenant_id, auth.subject_id, body.name, body.description, body.is_public,
    )
    return JSONResponse(status_code=201, content=result)


@router.get("/collections")
async def list_collections_endpoint(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    return {"collections": list_collections(auth.tenant_id, auth.subject_id)}


@router.post("/collections/{collection_id}/pin")
async def pin_endpoint(
    collection_id: str,
    body: PinRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    ok = pin_to_collection(collection_id, body.signal_rid, auth.subject_id, body.notes)
    return {"pinned": ok, "collection_id": collection_id}


@router.get("/collections/{collection_id}/items")
async def collection_items_endpoint(
    collection_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
    limit: int = Query(100, le=1000),
):
    items = get_collection_items(collection_id, auth.tenant_id, limit)
    return {"items": items, "count": len(items)}


@router.get("/gallery/geo")
async def gallery_geo(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    after: str = Query(""),
    before: str = Query(""),
    device_id: str = Query(""),
    limit: int = Query(200, le=500),
):
    """Photos with GPS coordinates as GeoJSON for map display."""
    from storage.postgres.db import get_connection
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    try:
        t_after = datetime.fromisoformat(after.replace("Z", "+00:00")) if after else now - timedelta(days=30)
    except Exception:
        t_after = now - timedelta(days=30)
    try:
        t_before = datetime.fromisoformat(before.replace("Z", "+00:00")) if before else now
    except Exception:
        t_before = now

    features = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Get media nodes with GPS coordinates
                device_clause = "AND m.first_seen_signal_rid IN (SELECT rid FROM signals WHERE device_id = %s)" if device_id else ""
                params: list = [auth.tenant_id, t_after, t_before]
                if device_id:
                    params.append(device_id)
                params.append(limit)

                cur.execute(f"""
                    SELECT m.node_id, m.phash, m.width, m.height, m.bytes_path,
                           m.first_seen_outlet, m.first_seen_at, m.source_count,
                           m.analysis,
                           ST_Y(m.exif_coordinates::geometry) AS lat,
                           ST_X(m.exif_coordinates::geometry) AS lon,
                           m.exif_taken_at
                    FROM media_nodes m
                    WHERE m.tenant_id = %s
                      AND m.exif_coordinates IS NOT NULL
                      AND m.created_at BETWEEN %s AND %s
                      {device_clause}
                    ORDER BY m.exif_taken_at DESC NULLS LAST
                    LIMIT %s
                """, tuple(params))

                for row in cur.fetchall():
                    analysis = row[8] or {}
                    features.append({
                        "type": "Feature",
                        "geometry": {
                            "type": "Point",
                            "coordinates": [float(row[10]), float(row[9])],  # [lon, lat]
                        },
                        "properties": {
                            "node_id": row[0],
                            "thumbnail_url": f"/api/media/{row[0]}/image" if row[4] else None,
                            "width": row[2],
                            "height": row[3],
                            "outlet": row[5],
                            "taken_at": row[11].isoformat() if row[11] else (row[6].isoformat() if row[6] else None),
                            "source_count": row[7],
                            "classification": analysis.get("classification"),
                            "tags": analysis.get("tags", []),
                            "description": analysis.get("description"),
                            "is_intelligence": analysis.get("is_intelligence", False),
                        },
                    })
    except Exception as e:
        return {"type": "FeatureCollection", "features": [], "error": str(e)}

    return {"type": "FeatureCollection", "features": features, "total": len(features)}
