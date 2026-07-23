"""
Photo EXIF geo-tagging — extract GPS coordinates from image EXIF data.

Upload a photo → extract lat/lon from EXIF → create a knowledge node
at that location with the image attached.
"""

from __future__ import annotations

import io
from typing import Optional


def extract_gps_from_exif(image_bytes: bytes) -> Optional[dict]:
    """Extract GPS coordinates and metadata from image EXIF data.

    Returns dict with lat, lon, altitude (if available), and timestamp.
    Returns None if no GPS data found.
    """
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS, GPSTAGS
    except ImportError:
        return None

    try:
        img = Image.open(io.BytesIO(image_bytes))
    except Exception:
        return None

    exif_data = img._getexif()
    if not exif_data:
        return None

    gps_info = {}
    timestamp = None

    for tag_id, value in exif_data.items():
        tag_name = TAGS.get(tag_id, tag_id)

        if tag_name == "GPSInfo":
            for gps_tag_id, gps_value in value.items():
                gps_tag_name = GPSTAGS.get(gps_tag_id, gps_tag_id)
                gps_info[gps_tag_name] = gps_value

        elif tag_name == "DateTimeOriginal":
            timestamp = str(value)

    if not gps_info:
        return None

    lat = _convert_gps_coord(
        gps_info.get("GPSLatitude"),
        gps_info.get("GPSLatitudeRef", "N"),
    )
    lon = _convert_gps_coord(
        gps_info.get("GPSLongitude"),
        gps_info.get("GPSLongitudeRef", "E"),
    )

    if lat is None or lon is None:
        return None

    result = {"latitude": lat, "longitude": lon}

    # Altitude
    alt = gps_info.get("GPSAltitude")
    if alt is not None:
        try:
            if hasattr(alt, 'numerator'):
                result["altitude"] = float(alt.numerator) / float(alt.denominator)
            else:
                result["altitude"] = float(alt)
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    # Direction/bearing
    bearing = gps_info.get("GPSImgDirection")
    if bearing is not None:
        try:
            if hasattr(bearing, 'numerator'):
                result["bearing"] = float(bearing.numerator) / float(bearing.denominator)
            else:
                result["bearing"] = float(bearing)
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    if timestamp:
        result["timestamp"] = timestamp

    return result


def _convert_gps_coord(coord_tuple, ref: str) -> Optional[float]:
    """Convert EXIF GPS coordinate tuple to decimal degrees.

    EXIF stores as ((degrees, 1), (minutes, 1), (seconds, 100))
    or as IFDRational objects.
    """
    if not coord_tuple or len(coord_tuple) < 3:
        return None

    try:
        degrees = _rational_to_float(coord_tuple[0])
        minutes = _rational_to_float(coord_tuple[1])
        seconds = _rational_to_float(coord_tuple[2])

        decimal = degrees + (minutes / 60.0) + (seconds / 3600.0)

        if ref in ("S", "W"):
            decimal = -decimal

        return round(decimal, 8)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _rational_to_float(value) -> float:
    """Convert EXIF rational value to float."""
    if hasattr(value, 'numerator') and hasattr(value, 'denominator'):
        return float(value.numerator) / float(value.denominator)
    if isinstance(value, tuple) and len(value) == 2:
        return float(value[0]) / float(value[1]) if value[1] != 0 else 0.0
    return float(value)
