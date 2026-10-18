"""
XML parser — handles generic XML and detects CoT (Cursor on Target).

Uses defusedxml to prevent entity expansion attacks (billion laughs).
"""

from typing import Optional
from xml.etree.ElementTree import Element

from storage.ingest.parsers import ParseResult, register_parser
from storage.ingest.parsers.base import BaseParser
from storage.signals.models import Observable

try:
    import defusedxml.ElementTree as ET
except ImportError:
    # Fallback (less safe, but functional)
    import xml.etree.ElementTree as ET


@register_parser
class XmlParser(BaseParser):
    name = "xml"
    format_id = "xml"

    def can_parse(self, raw_bytes: bytes, content_type: Optional[str] = None) -> float:
        if content_type and "xml" in content_type.lower():
            return 0.90

        try:
            text = raw_bytes[:500].decode("utf-8", errors="ignore").strip()
        except Exception:
            return 0.0

        if text.startswith("<?xml") or text.startswith("<"):
            # Check if it's actually parseable
            try:
                ET.fromstring(raw_bytes)
                return 0.85
            except Exception:
                # Might be partial or malformed — still XML-like
                if text.startswith("<?xml"):
                    return 0.60
                return 0.0
        return 0.0

    def parse(self, raw_bytes: bytes) -> ParseResult:
        root = ET.fromstring(raw_bytes)

        # Check for CoT (Cursor on Target)
        if _is_cot(root):
            return _parse_cot(root)

        # Generic XML → dict
        parsed = _element_to_dict(root)
        tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag

        return ParseResult(
            format_detected="xml",
            confidence=0.85,
            parsed=parsed,
            metadata={"root_element": tag, "namespace": root.tag if "}" in root.tag else None},
            observables=[],
            signal_type_hint=None,
            parser_name=self.name,
        )


def _is_cot(root: Element) -> bool:
    """Detect CoT XML: root element is <event> with uid, type, time attributes."""
    tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag
    if tag != "event":
        return False
    return "uid" in root.attrib and "type" in root.attrib


def _parse_cot(root: Element) -> ParseResult:
    """Parse Cursor on Target XML into structured signal."""
    attrs = root.attrib
    cot_type = attrs.get("type", "")

    # Extract point data
    point = root.find("point")
    if point is None:
        point = root.find(".//point")
    lat = float(point.get("lat", 0)) if point is not None else None
    lon = float(point.get("lon", 0)) if point is not None else None
    hae = float(point.get("hae", 0)) if point is not None else None

    # Extract detail/contact callsign
    detail = root.find("detail")
    if detail is None:
        detail = root.find(".//detail")
    callsign = None
    if detail is not None:
        contact = detail.find("contact")
        if contact is not None:
            callsign = contact.get("callsign")

    # Map CoT type to signal type
    signal_type = _map_cot_type(cot_type)

    parsed = {
        "uid": attrs.get("uid"),
        "cot_type": cot_type,
        "time": attrs.get("time"),
        "start": attrs.get("start"),
        "stale": attrs.get("stale"),
        "how": attrs.get("how"),
        "lat": lat,
        "lon": lon,
        "hae": hae,
        "callsign": callsign,
    }

    observables = []
    if callsign:
        observables.append(Observable(
            observable_type="callsign",
            observable_value=callsign,
            normalized_value=callsign.upper(),
            source_field="detail.contact.callsign",
        ))
    if lat and lon:
        observables.append(Observable(
            observable_type="coordinates",
            observable_value=f"{lat},{lon}",
            normalized_value=f"{lat:.6f},{lon:.6f}",
            source_field="point",
        ))

    return ParseResult(
        format_detected="xml/cot",
        confidence=0.95,
        parsed=parsed,
        metadata={"cot_type_raw": cot_type, "source_hint": "tak_cot"},
        observables=observables,
        signal_type_hint=signal_type,
        parser_name="xml/cot",
    )


def _map_cot_type(cot_type: str) -> str:
    """Map CoT type string to signal type."""
    prefix_map = {
        "a-f-G": "signal.geospatial.friendly_ground",
        "a-f-A": "signal.geospatial.friendly_air",
        "a-h-G": "signal.geospatial.hostile_ground",
        "a-h-A": "signal.geospatial.hostile_air",
        "a-u-G": "signal.geospatial.unknown_ground",
        "a-n": "signal.geospatial.neutral",
        "b-i": "signal.intelligence.imagery",
        "t-": "signal.operations.tasking",
        "r-": "signal.operations.reply",
    }
    for prefix, signal_type in prefix_map.items():
        if cot_type.startswith(prefix):
            return signal_type
    return "signal.geospatial.position_report"


def _element_to_dict(element: Element) -> dict:
    """Convert XML element tree to dict (recursive, max depth 10)."""
    result = {}
    if element.attrib:
        result["_attributes"] = dict(element.attrib)
    if element.text and element.text.strip():
        result["_text"] = element.text.strip()

    for child in element:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        child_dict = _element_to_dict(child)
        if tag in result:
            if isinstance(result[tag], list):
                result[tag].append(child_dict)
            else:
                result[tag] = [result[tag], child_dict]
        else:
            result[tag] = child_dict

    return result
