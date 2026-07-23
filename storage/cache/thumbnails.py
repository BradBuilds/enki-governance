"""
Thumbnail cache — downloads and stores article images locally.

Images are content-addressed by SHA-256 of the source URL, stored in a
two-level directory structure to avoid flat directories with thousands of files.

Layout:
    storage/cache/thumbnails/
        ab/ab3f...c7.jpg
        ab/ab3f...c7.json   (sidecar metadata)
"""

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

from storage.connectors.article_extract import validate_image_bytes

log = logging.getLogger("enki.thumbnails")

CACHE_DIR = Path(os.getenv(
    "ENKI_THUMBNAIL_CACHE",
    os.path.join(os.path.dirname(__file__), "thumbnails_data"),
))

MAX_IMAGE_BYTES = 2 * 1024 * 1024  # 2MB
MAX_AGE_DAYS = 30

_EXT_MAP = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _cache_key(url: str) -> str:
    """SHA-256 hex digest of the URL."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _cache_path(key: str) -> Path:
    """Two-level directory: first 2 chars as subdirectory."""
    return CACHE_DIR / key[:2] / key


def cache_thumbnail(image_url: str, timeout: int = 8) -> Optional[str]:
    """Download image, validate, save to disk. Returns cache key or None."""
    if not image_url or not image_url.startswith("http"):
        return None

    key = _cache_key(image_url)
    base = _cache_path(key)

    # Check if already cached (any extension)
    for ext in _EXT_MAP.values():
        if (base.parent / f"{key}{ext}").exists():
            return key

    try:
        req = Request(image_url, headers={
            "User-Agent": "Enki/1.0 (Thumbnail Cache)",
            "Accept": "image/*",
        })
        with urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            data = resp.read(MAX_IMAGE_BYTES)

        if len(data) < 100:
            return None

        # Validate image bytes
        detected_type = validate_image_bytes(data)
        if not detected_type:
            # Trust Content-Type header as fallback
            for mime in _EXT_MAP:
                if mime in content_type:
                    detected_type = mime
                    break
            if not detected_type:
                log.debug(f"Not a valid image: {image_url}")
                return None

        ext = _EXT_MAP.get(detected_type, ".bin")
        img_path = base.parent / f"{key}{ext}"

        # Ensure directory exists
        img_path.parent.mkdir(parents=True, exist_ok=True)

        # Write image
        img_path.write_bytes(data)

        # Write sidecar metadata
        sidecar = base.parent / f"{key}.json"
        sidecar.write_text(json.dumps({
            "source_url": image_url,
            "content_type": detected_type,
            "byte_size": len(data),
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "ext": ext,
        }))

        log.debug(f"Cached thumbnail: {key}{ext} ({len(data)} bytes)")
        return key

    except (URLError, TimeoutError, OSError) as e:
        log.debug(f"Thumbnail cache failed for {image_url}: {e}")
        return None
    except Exception as e:
        log.debug(f"Thumbnail cache unexpected error: {e}")
        return None


def get_thumbnail_info(cache_key: str) -> Optional[dict]:
    """Return sidecar metadata for a cached thumbnail, or None."""
    base = _cache_path(cache_key)
    sidecar = base.parent / f"{cache_key}.json"
    if not sidecar.exists():
        return None
    try:
        return json.loads(sidecar.read_text())
    except Exception:
        return None


def get_thumbnail_path(cache_key: str) -> Optional[Path]:
    """Return filesystem path for a cached thumbnail image, or None."""
    info = get_thumbnail_info(cache_key)
    if not info:
        return None
    ext = info.get("ext", ".jpg")
    img_path = _cache_path(cache_key).parent / f"{cache_key}{ext}"
    if img_path.exists():
        return img_path
    return None
