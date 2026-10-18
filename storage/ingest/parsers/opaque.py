"""
Opaque parser — fallback for unrecognized binary data.

Always succeeds. Stores hash + size. Data is preserved in LDU-T
and staged in LDU-P for manual review.

This parser is NOT registered in the registry — it's called directly
by detect_and_parse() when no other parser matches above threshold.
"""

import hashlib
from typing import Optional

from storage.ingest.parsers import ParseResult
from storage.ingest.parsers.base import BaseParser
from storage.signals.models import Observable


class OpaqueParser(BaseParser):
    name = "opaque"
    format_id = "binary/opaque"

    def can_parse(self, raw_bytes: bytes, content_type: Optional[str] = None) -> float:
        # Always matches as last resort, but with low confidence
        return 0.10

    def parse(self, raw_bytes: bytes) -> ParseResult:
        file_hash = hashlib.sha256(raw_bytes).hexdigest()

        # Try to detect if it's text
        is_text = False
        text_preview = None
        try:
            decoded = raw_bytes[:500].decode("utf-8", errors="strict")
            is_text = True
            text_preview = decoded[:200]
        except (UnicodeDecodeError, ValueError):
            pass

        parsed = {
            "size_bytes": len(raw_bytes),
            "file_sha256": file_hash,
            "is_text": is_text,
            "text_preview": text_preview,
            "first_bytes_hex": raw_bytes[:32].hex(),
        }

        observables = [
            Observable(
                observable_type="file_hash_sha256",
                observable_value=file_hash,
                normalized_value=file_hash,
                source_field="file_content",
            )
        ]

        return ParseResult(
            format_detected="binary/opaque",
            confidence=0.10,
            parsed=parsed,
            metadata={"is_text": is_text, "needs_review": True},
            observables=observables,
            signal_type_hint="signal.custom.untyped",
            parser_name=self.name,
        )
