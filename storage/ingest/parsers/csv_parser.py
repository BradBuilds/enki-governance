"""
CSV parser — handles comma/tab/pipe separated data.

Infers column types and extracts observables from cell values.
Each CSV can produce multiple signals (one per row) or a single
signal containing all rows, depending on the use case.
"""

import csv
import io
from typing import Optional

from storage.ingest.parsers import ParseResult, register_parser
from storage.ingest.parsers.base import BaseParser
from storage.signals.models import Observable
from storage.ingest.parsers.json_parser import _classify_string_value


@register_parser
class CsvParser(BaseParser):
    name = "csv"
    format_id = "csv"

    def can_parse(self, raw_bytes: bytes, content_type: Optional[str] = None) -> float:
        if content_type and "csv" in content_type.lower():
            return 0.90

        try:
            text = raw_bytes[:4000].decode("utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError):
            return 0.0

        lines = text.strip().split("\n")
        if len(lines) < 2:
            return 0.0

        # Check if it looks like CSV (consistent delimiter)
        try:
            dialect = csv.Sniffer().sniff(text[:4000])
            reader = csv.reader(io.StringIO(text), dialect)
            rows = list(reader)
            if len(rows) >= 2:
                header_len = len(rows[0])
                if header_len >= 2 and all(len(r) == header_len for r in rows[:5]):
                    return 0.75
        except csv.Error:
            pass

        return 0.0

    def parse(self, raw_bytes: bytes) -> ParseResult:
        text = raw_bytes.decode("utf-8", errors="replace")
        dialect = csv.Sniffer().sniff(text[:4000])
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)

        headers = reader.fieldnames or []
        rows = []
        observables = []
        max_rows = 10000  # safety cap

        for i, row in enumerate(reader):
            if i >= max_rows:
                break
            rows.append(dict(row))

            # Extract observables from first 100 rows only (performance)
            if i < 100:
                for key, value in row.items():
                    if value:
                        obs = _classify_string_value(value.strip(), f"row[{i}].{key}")
                        if obs:
                            observables.append(obs)

        parsed = {
            "headers": headers,
            "row_count": len(rows),
            "rows_preview": rows[:10],  # first 10 rows in payload
            "column_count": len(headers),
        }

        return ParseResult(
            format_detected="csv",
            confidence=0.75,
            parsed=parsed,
            metadata={
                "delimiter": dialect.delimiter,
                "total_rows": len(rows),
                "headers": headers,
            },
            observables=_deduplicate_observables(observables),
            signal_type_hint=None,  # CSV is too generic to hint a type
            parser_name=self.name,
        )


def _deduplicate_observables(observables: list[Observable]) -> list[Observable]:
    """Remove duplicate observables (same type + normalized value)."""
    seen = set()
    unique = []
    for obs in observables:
        key = (obs.observable_type, obs.normalized_value)
        if key not in seen:
            seen.add(key)
            unique.append(obs)
    return unique
