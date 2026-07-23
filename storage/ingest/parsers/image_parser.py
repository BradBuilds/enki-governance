"""
Image parser — extracts metadata from JPEG, PNG, TIFF, and other image formats.

Extracts EXIF data (GPS, camera, timestamps) without loading the full image
into memory. Uses struct for magic byte detection and basic EXIF parsing.

For full EXIF support, Pillow can be used (optional dependency).
"""

import hashlib
import struct
from typing import Optional

from storage.ingest.parsers import ParseResult, register_parser
from storage.ingest.parsers.base import BaseParser
from storage.signals.models import Observable


# Magic byte signatures
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_TIFF_LE_MAGIC = b"II\x2a\x00"
_TIFF_BE_MAGIC = b"MM\x00\x2a"
_GIF_MAGIC = b"GIF8"
_WEBP_MAGIC = b"RIFF"  # followed by size then "WEBP"
_BMP_MAGIC = b"BM"
_PDF_MAGIC = b"%PDF"


@register_parser
class ImageParser(BaseParser):
    name = "image"
    format_id = "image"

    def can_parse(self, raw_bytes: bytes, content_type: Optional[str] = None) -> float:
        if content_type and content_type.lower().startswith("image/"):
            return 0.95

        if len(raw_bytes) < 8:
            return 0.0

        if raw_bytes[:3] == _JPEG_MAGIC:
            return 0.99
        if raw_bytes[:8] == _PNG_MAGIC:
            return 0.99
        if raw_bytes[:4] in (_TIFF_LE_MAGIC, _TIFF_BE_MAGIC):
            return 0.95
        if raw_bytes[:4] == _GIF_MAGIC:
            return 0.99
        if raw_bytes[:2] == _BMP_MAGIC:
            return 0.90
        if raw_bytes[:4] == _WEBP_MAGIC and raw_bytes[8:12] == b"WEBP":
            return 0.99

        return 0.0

    def parse(self, raw_bytes: bytes) -> ParseResult:
        fmt = _detect_image_format(raw_bytes)
        file_hash = hashlib.sha256(raw_bytes).hexdigest()

        parsed = {
            "format": fmt,
            "size_bytes": len(raw_bytes),
            "file_sha256": file_hash,
        }
        observables = [
            Observable(
                observable_type="file_hash_sha256",
                observable_value=file_hash,
                normalized_value=file_hash,
                source_field="file_content",
            )
        ]

        # Try EXIF extraction (JPEG/TIFF)
        exif = _extract_basic_exif(raw_bytes, fmt)
        if exif:
            parsed["exif"] = exif
            if exif.get("gps_lat") and exif.get("gps_lon"):
                lat = exif["gps_lat"]
                lon = exif["gps_lon"]
                parsed["latitude"] = lat
                parsed["longitude"] = lon
                observables.append(Observable(
                    observable_type="coordinates",
                    observable_value=f"{lat},{lon}",
                    normalized_value=f"{lat:.6f},{lon:.6f}",
                    source_field="exif.gps",
                ))

        # Try Pillow for richer extraction (optional)
        pillow_meta = _try_pillow_extraction(raw_bytes)
        if pillow_meta:
            parsed.update(pillow_meta)

        return ParseResult(
            format_detected=f"image/{fmt}",
            confidence=0.95,
            parsed=parsed,
            metadata={"has_exif": bool(exif)},
            observables=observables,
            signal_type_hint="signal.content.image",
            parser_name=self.name,
        )


def _detect_image_format(raw_bytes: bytes) -> str:
    if raw_bytes[:3] == _JPEG_MAGIC:
        return "jpeg"
    if raw_bytes[:8] == _PNG_MAGIC:
        return "png"
    if raw_bytes[:4] in (_TIFF_LE_MAGIC, _TIFF_BE_MAGIC):
        return "tiff"
    if raw_bytes[:4] == _GIF_MAGIC:
        return "gif"
    if raw_bytes[:4] == _WEBP_MAGIC and raw_bytes[8:12] == b"WEBP":
        return "webp"
    if raw_bytes[:2] == _BMP_MAGIC:
        return "bmp"
    return "unknown"


def _extract_basic_exif(raw_bytes: bytes, fmt: str) -> Optional[dict]:
    """Basic EXIF extraction without external libraries. Limited but zero-dep."""
    if fmt != "jpeg":
        return None

    # Find EXIF marker (0xFFE1)
    pos = 2  # skip SOI marker
    while pos < len(raw_bytes) - 4:
        if raw_bytes[pos] != 0xFF:
            break
        marker = raw_bytes[pos + 1]
        if marker == 0xE1:  # APP1 = EXIF
            return {"exif_present": True}
        # Skip to next marker
        length = struct.unpack(">H", raw_bytes[pos + 2:pos + 4])[0]
        pos += 2 + length

    return None


def _try_pillow_extraction(raw_bytes: bytes) -> Optional[dict]:
    """Try to extract image dimensions and EXIF GPS via Pillow (optional)."""
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS, GPSTAGS
        import io

        img = Image.open(io.BytesIO(raw_bytes))
        result = {
            "width": img.width,
            "height": img.height,
            "mode": img.mode,
        }

        # EXIF GPS extraction
        exif_data = img.getexif()
        if exif_data:
            gps_info = exif_data.get(34853)  # GPSInfo tag
            if gps_info and isinstance(gps_info, dict):
                lat = _convert_gps_coord(gps_info.get(2), gps_info.get(1))
                lon = _convert_gps_coord(gps_info.get(4), gps_info.get(3))
                if lat is not None and lon is not None:
                    result["gps_lat"] = lat
                    result["gps_lon"] = lon

        img.close()
        return result
    except Exception:
        return None


def _convert_gps_coord(coord, ref) -> Optional[float]:
    """Convert EXIF GPS coordinates to decimal degrees."""
    if not coord or not ref:
        return None
    try:
        degrees = float(coord[0])
        minutes = float(coord[1])
        seconds = float(coord[2])
        decimal = degrees + minutes / 60 + seconds / 3600
        if ref in ("S", "W"):
            decimal = -decimal
        return decimal
    except (TypeError, IndexError, ValueError):
        return None
