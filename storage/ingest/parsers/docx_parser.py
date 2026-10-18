"""
DOCX Parser — extracts text, metadata, and structure from Word documents.

Uses python-docx for extraction. Handles paragraphs, tables, headers.
"""

from __future__ import annotations

import hashlib
import io
from typing import Optional

from storage.ingest.parsers import ParseResult, register_parser
from storage.ingest.parsers.base import BaseParser
from storage.signals.models import Observable


@register_parser
class DocxParser(BaseParser):
    name = "docx"

    def can_parse(self, raw_bytes: bytes, content_type: Optional[str] = None) -> float:
        # DOCX is a ZIP with specific structure
        if raw_bytes[:4] == b'PK\x03\x04':
            # Check for word/ directory inside ZIP
            if b'word/' in raw_bytes[:2000]:
                return 0.93
        if content_type and ("wordprocessing" in content_type.lower() or
                             "msword" in content_type.lower() or
                             "docx" in content_type.lower()):
            return 0.92
        return 0.0

    def parse(self, raw_bytes: bytes) -> ParseResult:
        file_hash = hashlib.sha256(raw_bytes).hexdigest()

        try:
            from docx import Document

            doc = Document(io.BytesIO(raw_bytes))

            # Extract paragraphs
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            full_text = "\n".join(paragraphs)

            # Extract tables
            tables = []
            for table in doc.tables[:10]:  # Cap at 10 tables
                rows = []
                for row in table.rows[:50]:  # Cap at 50 rows per table
                    rows.append([cell.text.strip() for cell in row.cells])
                if rows:
                    tables.append(rows)

            # Core properties
            props = doc.core_properties
            title = props.title or ""
            author = props.author or ""
            created = str(props.created) if props.created else ""

            # Observables from text
            observables = self._extract_observables(full_text)

            parsed = {
                "title": title or paragraphs[0][:100] if paragraphs else "Untitled",
                "author": author,
                "created": created,
                "paragraph_count": len(paragraphs),
                "table_count": len(doc.tables),
                "text": full_text[:3000],
                "full_text_length": len(full_text),
                "file_sha256": file_hash,
                "size_bytes": len(raw_bytes),
            }

            if tables:
                parsed["tables_preview"] = tables[0][:5]  # First table, first 5 rows

            return ParseResult(
                format_detected="docx",
                confidence=0.92,
                parsed=parsed,
                metadata={
                    "title": title, "author": author,
                    "paragraph_count": len(paragraphs),
                    "table_count": len(doc.tables),
                },
                observables=observables,
                signal_type_hint="signal.content.document",
                parser_name="docx",
            )

        except Exception as e:
            return ParseResult(
                format_detected="docx",
                confidence=0.40,
                parsed={"file_sha256": file_hash, "size_bytes": len(raw_bytes), "parse_error": str(e)},
                metadata={"parse_error": str(e)},
                observables=[Observable(observable_type="file_hash_sha256", observable_value=file_hash, normalized_value=file_hash, source_field="file")],
                signal_type_hint="signal.content.document",
                parser_name="docx",
            )

    def _extract_observables(self, text: str) -> list[Observable]:
        import re
        observables = []
        seen = set()
        for match in re.finditer(r'[\w.+-]+@[\w.-]+\.\w+', text[:10000]):
            val = match.group().lower()
            if val not in seen:
                observables.append(Observable(observable_type="email_address", observable_value=match.group(), normalized_value=val, source_field="docx_text"))
                seen.add(val)
        for match in re.finditer(r'https?://[^\s<>"\']+', text[:10000]):
            val = match.group().rstrip('.,;:)')
            if val not in seen:
                observables.append(Observable(observable_type="url", observable_value=val, normalized_value=val, source_field="docx_text"))
                seen.add(val)
        return observables[:50]
