"""
Media node API — Phase 4B.

Endpoints:
    GET /media/{node_id}              — node detail + full source log
    GET /media/{node_id}/image        — serves the archived image bytes
    GET /media/{node_id}/similar      — near-duplicate candidates (by phash)
    GET /media                        — list view (most-sourced / recent / by outlet)

The detail + similar endpoints exist so the /nodes/[id] page can render
media nodes the same way it renders other nodes (Phase 2 card contract).
The /image endpoint serves from storage/cache/media_library/ — the bytes
are archived there by the media pipeline keyed by SHA-256 content hash.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from fastapi.responses import Response, FileResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

log = logging.getLogger("enki.media")
router = APIRouter(prefix="/media", tags=["media"])


@router.get("")
async def list_media(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    outlet: Optional[str] = Query(None),
    sort: str = Query("recent", regex="^(recent|sources|outlets)$"),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
):
    """List view. Sort by recent | most-sourced | most-outlets."""
    from storage.postgres.db import get_connection

    clauses = ["tenant_id = %s"]
    params: list = [auth.tenant_id]
    if outlet:
        # Filter by outlet via media_sources
        clauses.append(
            "node_id IN (SELECT media_node_id FROM media_sources "
            "WHERE tenant_id = %s AND outlet = %s)"
        )
        params += [auth.tenant_id, outlet]

    order = {
        "recent":  "first_seen_at DESC",
        "sources": "source_count DESC, first_seen_at DESC",
        "outlets": "outlet_count DESC, source_count DESC",
    }[sort]

    params += [limit, offset]
    media: list[dict] = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT node_id, phash, byte_hash, width, height, mime_type,
                           first_seen_url, first_seen_outlet, first_seen_at,
                           source_count, outlet_count, bytes_path
                      FROM media_nodes
                     WHERE {' AND '.join(clauses)}
                     ORDER BY {order}
                     LIMIT %s OFFSET %s
                    """,
                    params,
                )
                cols = [d[0] for d in cur.description]
                for r in cur.fetchall():
                    row = dict(zip(cols, r))
                    if row.get("first_seen_at"):
                        row["first_seen_at"] = row["first_seen_at"].isoformat()
                    media.append(row)
    except Exception as e:
        log.exception("list_media failed")
        raise HTTPException(500, detail=str(e))

    return {"media": media, "count": len(media)}


@router.get("/{node_id}")
async def get_media(
    node_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Detail: node row + full media_sources log (who's used this image)."""
    from storage.postgres.db import get_connection

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT node_id, subtype, phash, dhash, byte_hash,
                           width, height, bytes, mime_type, bytes_path,
                           first_seen_url, first_seen_outlet,
                           first_seen_signal_rid, first_seen_at,
                           source_count, outlet_count, analysis,
                           ST_Y(exif_coordinates::geometry) AS exif_lat,
                           ST_X(exif_coordinates::geometry) AS exif_lon,
                           exif_taken_at,
                           created_at, updated_at
                      FROM media_nodes
                     WHERE tenant_id = %s AND node_id = %s
                    """,
                    (auth.tenant_id, node_id),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(404, detail="not found")
                cols = [d[0] for d in cur.description]
                node = dict(zip(cols, row))
                for k in ("first_seen_at", "exif_taken_at", "created_at", "updated_at"):
                    if node.get(k):
                        node[k] = node[k].isoformat()

                # Full source log — every URL/outlet/signal that's used this image
                cur.execute(
                    """
                    SELECT source_id, signal_rid, article_node_id,
                           url, outlet, caption, role, seen_at
                      FROM media_sources
                     WHERE tenant_id = %s AND media_node_id = %s
                     ORDER BY seen_at DESC
                     LIMIT 500
                    """,
                    (auth.tenant_id, node_id),
                )
                scols = [d[0] for d in cur.description]
                sources = []
                for r in cur.fetchall():
                    s = dict(zip(scols, r))
                    if s.get("seen_at"):
                        s["seen_at"] = s["seen_at"].isoformat()
                    sources.append(s)
                node["sources"] = sources
    except HTTPException:
        raise
    except Exception as e:
        log.exception("get_media failed")
        raise HTTPException(500, detail=str(e))

    return node


@router.get("/{node_id}/image")
async def get_media_image(
    node_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Serve the archived image bytes from the library."""
    from storage.postgres.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT bytes_path, mime_type FROM media_nodes "
                "WHERE tenant_id = %s AND node_id = %s",
                (auth.tenant_id, node_id),
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(404, detail="not found")
    bytes_path, mime_type = row[0], row[1] or "application/octet-stream"
    if not bytes_path:
        raise HTTPException(404, detail="image bytes not archived")

    # bytes_path is repo-relative; resolve against repo root
    repo_root = Path(__file__).resolve().parents[4]
    abs_path = (repo_root / bytes_path).resolve()
    # Prevent path traversal — resolved path must stay inside repo root
    if not str(abs_path).startswith(str(repo_root)):
        raise HTTPException(403, detail="path traversal blocked")
    if not abs_path.exists() or not abs_path.is_file():
        raise HTTPException(404, detail="image file missing from library")
    return FileResponse(path=str(abs_path), media_type=mime_type)


@router.get("/{node_id}/similar")
async def get_media_similar(
    node_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
    hamming_max: int = Query(8, ge=0, le=32),
    limit: int = Query(20, le=100),
):
    """Near-duplicate candidates. Used to find visually-similar images
    even when bytes differ (same photo at different resolutions)."""
    from storage.postgres.db import get_connection
    from core.media.nodes import find_similar_media

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT phash FROM media_nodes "
                    "WHERE tenant_id = %s AND node_id = %s",
                    (auth.tenant_id, node_id),
                )
                row = cur.fetchone()
            if not row:
                raise HTTPException(404, detail="not found")
            phash = row[0]
            similar = find_similar_media(
                conn, phash,
                tenant_id=auth.tenant_id,
                hamming_max=hamming_max,
                limit=limit + 1,
            )
            # Drop the node itself from the result
            similar = [s for s in similar if s["node_id"] != node_id][:limit]
    except HTTPException:
        raise
    except Exception as e:
        log.exception("get_media_similar failed")
        raise HTTPException(500, detail=str(e))

    return {"node_id": node_id, "hamming_max": hamming_max, "similar": similar}
