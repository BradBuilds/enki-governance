"""
XLSX Parser — extracts sheets, rows, headers, and metadata from Excel files.

Uses openpyxl for extraction. Handles multiple sheets, formulas, dates.
"""

from __future__ import annotations

import hashlib
import io
from typing import Optional

from storage.ingest.parsers import ParseResult, register_parser
from storage.ingest.parsers.base import BaseParser
from storage.signals.models import Observable


@register_parser
class XlsxParser(BaseParser):
    name = "xlsx"

    def can_parse(self, raw_bytes: bytes, content_type: Optional[str] = None) -> float:
        # XLSX is a ZIP with specific structure
        if raw_bytes[:4] == b'PK\x03\x04':
            if b'xl/' in raw_bytes[:2000] or b'[Content_Types].xml' in raw_bytes[:2000]:
                # Could be XLSX or DOCX — check for xl/ directory
                if b'xl/worksheets' in raw_bytes[:4000] or b'xl/workbook' in raw_bytes[:4000]:
                    return 0.94
        if content_type and ("spreadsheet" in content_type.lower() or
                             "excel" in content_type.lower() or
                             "xlsx" in content_type.lower()):
            return 0.93
        return 0.0

    def parse(self, raw_bytes: bytes) -> ParseResult:
        file_hash = hashlib.sha256(raw_bytes).hexdigest()

        try:
            from openpyxl import load_workbook

            wb = load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)

            sheets = []
            total_rows = 0
            observables = []
            seen_obs = set()

            for sheet_name in wb.sheetnames[:10]:  # Cap at 10 sheets
                ws = wb[sheet_name]

                # Read headers (first row)
                headers = []
                rows_data = []
                row_count = 0

                for i, row in enumerate(ws.iter_rows(values_only=True)):
                    if i == 0:
                        headers = [str(c) if c is not None else f"col_{j}" for j, c in enumerate(row)]
                    else:
                        row_data = [str(c) if c is not None else "" for c in row]
                        if i <= 10:  # Preview first 10 rows
                            rows_data.append(row_data)

                        # Extract observables from first 100 rows
                        if i <= 100:
                            for cell_val in row_data:
                                self._check_observable(cell_val, observables, seen_obs)

                    row_count = i

                total_rows += row_count

                sheets.append({
                    "name": sheet_name,
                    "headers": headers,
                    "row_count": row_count,
                    "column_count": len(headers),
                    "preview": rows_data[:5],
                })

            wb.close()

            parsed = {
                "sheet_count": len(sheets),
                "total_rows": total_rows,
                "sheets": sheets,
                "file_sha256": file_hash,
                "size_bytes": len(raw_bytes),
            }

            # Title from first sheet name or first header
            title = sheets[0]["name"] if sheets else "Spreadsheet"

            return ParseResult(
                format_detected="xlsx",
                confidence=0.93,
                parsed=parsed,
                metadata={
                    "sheet_count": len(sheets),
                    "total_rows": total_rows,
                    "sheet_names": [s["name"] for s in sheets],
                },
                observables=observables[:50],
                signal_type_hint=None,  # XLSX is too generic for auto-type
                parser_name="xlsx",
            )

        except Exception as e:
            return ParseResult(
                format_detected="xlsx",
                confidence=0.40,
                parsed={"file_sha256": file_hash, "size_bytes": len(raw_bytes), "parse_error": str(e)},
                metadata={"parse_error": str(e)},
                observables=[Observable(observable_type="file_hash_sha256", observable_value=file_hash, normalized_value=file_hash, source_field="file")],
                signal_type_hint=None,
                parser_name="xlsx",
            )

    def _check_observable(self, value: str, observables: list, seen: set) -> None:
        import re
        if not value or len(value) > 500:
            return

        # Email
        m = re.match(r'^[\w.+-]+@[\w.-]+\.\w+$', value)
        if m and value.lower() not in seen:
            observables.append(Observable(observable_type="email_address", observable_value=value, normalized_value=value.lower(), source_field="xlsx_cell"))
            seen.add(value.lower())
            return

        # IP
        m = re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', value)
        if m and value not in seen:
            observables.append(Observable(observable_type="ip_address", observable_value=value, normalized_value=value, source_field="xlsx_cell"))
            seen.add(value)
