"""
PDF Parser — extracts text per page, metadata, and page count.

Uses pdfplumber for text extraction. Falls back to basic metadata
if text extraction fails (scanned PDFs need OCR worker later).
"""

from __future__ import annotations

import hashlib
import io
from typing import Optional

from storage.ingest.parsers import ParseResult, register_parser
from storage.ingest.parsers.base import BaseParser
from storage.signals.models import Observable


@register_parser
class PdfParser(BaseParser):
    name = "pdf"

    def can_parse(self, raw_bytes: bytes, content_type: Optional[str] = None) -> float:
        # Magic bytes: %PDF
        if raw_bytes[:5] == b'%PDF-':
            return 0.97
        if content_type and "pdf" in content_type.lower():
            return 0.95
        return 0.0

    def parse(self, raw_bytes: bytes) -> ParseResult:
        file_hash = hashlib.sha256(raw_bytes).hexdigest()
        metadata = {"file_sha256": file_hash, "size_bytes": len(raw_bytes)}

        try:
            import pdfplumber

            pages_text = []
            page_count = 0
            total_chars = 0

            with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
                page_count = len(pdf.pages)
                metadata["page_count"] = page_count
                metadata["pdf_metadata"] = pdf.metadata or {}

                for i, page in enumerate(pdf.pages[:50]):  # Cap at 50 pages
                    text = page.extract_text() or ""
                    pages_text.append(text)
                    total_chars += len(text)

            full_text = "\n\n--- Page Break ---\n\n".join(pages_text)
            text_preview = full_text[:2000] if full_text else ""

            # Determine if this is a scanned PDF (no extractable text)
            is_scanned = total_chars < 100 and page_count > 0
            metadata["is_scanned"] = is_scanned
            metadata["total_chars"] = total_chars

            if is_scanned:
                metadata["needs_ocr"] = True

            # Extract observables from text
            observables = self._extract_observables_from_text(full_text)

            parsed = {
                "page_count": page_count,
                "text": text_preview,
                "full_text_length": total_chars,
                "is_scanned": is_scanned,
                "file_sha256": file_hash,
                "size_bytes": len(raw_bytes),
            }

            # Add PDF metadata
            pdf_meta = metadata.get("pdf_metadata", {})
            if pdf_meta.get("Title"):
                parsed["title"] = pdf_meta["Title"]
            if pdf_meta.get("Author"):
                parsed["author"] = pdf_meta["Author"]
            if pdf_meta.get("CreationDate"):
                parsed["created"] = pdf_meta["CreationDate"]

            return ParseResult(
                format_detected="pdf",
                confidence=0.95 if not is_scanned else 0.60,
                parsed=parsed,
                metadata=metadata,
                observables=observables,
                signal_type_hint="signal.content.document",
                parser_name="pdf",
            )

        except Exception as e:
            # PDF parsing failed — store basic metadata
            return ParseResult(
                format_detected="pdf",
                confidence=0.50,
                parsed={
                    "file_sha256": file_hash,
                    "size_bytes": len(raw_bytes),
                    "parse_error": str(e),
                },
                metadata={**metadata, "parse_error": str(e)},
                observables=[Observable(observable_type="file_hash_sha256", observable_value=file_hash, normalized_value=file_hash, source_field="file")],
                signal_type_hint="signal.content.document",
                parser_name="pdf",
            )

    def _extract_observables_from_text(self, text: str) -> list[Observable]:
        """Extract IPs, emails, URLs from PDF text."""
        import re
        observables = []
        seen = set()

        if not text:
            return observables

        # Emails
        for match in re.finditer(r'[\w.+-]+@[\w.-]+\.\w+', text[:10000]):
            val = match.group().lower()
            if val not in seen:
                observables.append(Observable(observable_type="email_address", observable_value=match.group(), normalized_value=val, source_field="pdf_text"))
                seen.add(val)

        # IPs
        for match in re.finditer(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', text[:10000]):
            val = match.group()
            if val not in seen:
                observables.append(Observable(observable_type="ip_address", observable_value=val, normalized_value=val, source_field="pdf_text"))
                seen.add(val)

        # URLs
        for match in re.finditer(r'https?://[^\s<>"\']+', text[:10000]):
            val = match.group().rstrip('.,;:)')
            if val not in seen:
                observables.append(Observable(observable_type="url", observable_value=val, normalized_value=val, source_field="pdf_text"))
                seen.add(val)

        return observables[:50]  # Cap
