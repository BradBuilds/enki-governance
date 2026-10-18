"""
Thumbnail proxy — serves cached article images.

GET /thumbnails/{cache_key} — Returns the cached image file.
"""

import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from storage.cache.thumbnails import get_thumbnail_path, get_thumbnail_info

router = APIRouter(prefix="/thumbnails", tags=["thumbnails"])

_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


@router.get("/{cache_key}")
async def get_thumbnail(cache_key: str):
    """Serve a cached thumbnail image by its SHA-256 cache key."""
    if not _HEX_RE.match(cache_key):
        raise HTTPException(status_code=400, detail="Invalid cache key")

    path = get_thumbnail_path(cache_key)
    if not path:
        raise HTTPException(status_code=404, detail="Thumbnail not found")

    info = get_thumbnail_info(cache_key)
    media_type = info.get("content_type", "image/jpeg") if info else "image/jpeg"

    return FileResponse(
        path=str(path),
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )
