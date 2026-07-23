"""
Parser registry — auto-detection and dispatch.

The registry holds all available parsers. When raw bytes arrive,
the auto-detect engine runs each parser's `can_parse()` method and
selects the highest-confidence match.

Usage:
    from storage.ingest.parsers import detect_and_parse

    result = detect_and_parse(raw_bytes, content_type_hint="application/json")
    # result.format_detected = "json"
    # result.confidence = 0.85
    # result.parsed = {"temperature": 72, ...}
    # result.observables = [Observable(...), ...]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from storage.signals.models import Observable


@dataclass
class ParseResult:
    """Result of parsing raw bytes."""
    format_detected: str                          # json, xml/cot, email, csv, etc.
    confidence: float                             # 0.0 - 1.0
    parsed: dict                                  # structured representation
    metadata: dict = field(default_factory=dict)  # parser-specific metadata
    observables: list[Observable] = field(default_factory=list)
    signal_type_hint: Optional[str] = None        # proposed signal type
    parser_name: str = ""                         # which parser produced this


# Parser registry — populated by register_parser()
_REGISTRY: list[type] = []


def register_parser(cls: type) -> type:
    """Decorator to register a parser class in the global registry."""
    _REGISTRY.append(cls)
    return cls


def get_registered_parsers() -> list[type]:
    """Return all registered parser classes, ordered by priority."""
    return list(_REGISTRY)


def detect_and_parse(
    raw_bytes: bytes,
    content_type: Optional[str] = None,
    device_id: Optional[str] = None,
) -> ParseResult:
    """
    Auto-detect format and parse raw bytes.

    Runs each registered parser's can_parse() method, selects the
    highest-confidence match, then calls parse().

    If no parser scores above 0.5 confidence, returns opaque result.

    Args:
        raw_bytes: The raw payload bytes
        content_type: MIME type hint from request (optional)
        device_id: Source device ID (for profile lookup, future)

    Returns:
        ParseResult with format, confidence, parsed data, observables
    """
    best_parser = None
    best_confidence = 0.0

    for parser_cls in _REGISTRY:
        parser = parser_cls()
        confidence = parser.can_parse(raw_bytes, content_type)
        if confidence > best_confidence:
            best_confidence = confidence
            best_parser = parser

    if best_parser and best_confidence >= 0.5:
        result = best_parser.parse(raw_bytes)
        return result

    # Fallback: opaque (no parser matched)
    from storage.ingest.parsers.opaque import OpaqueParser
    return OpaqueParser().parse(raw_bytes)


# Import parsers to trigger registration (order matters for priority)
# Bank statement parser BEFORE generic csv/pdf so it wins when bank patterns detected.
from storage.ingest.parsers import (  # noqa: E402, F401
    json_parser,
    xml_parser,
    email_parser,
    bank_statement_parser,
    csv_parser,
    image_parser,
    pdf_parser,
    docx_parser,
    xlsx_parser,
    html_parser,
    opaque,
)
