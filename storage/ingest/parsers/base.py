"""
Base parser interface. All parsers implement this.
"""

from abc import ABC, abstractmethod
from typing import Optional

from storage.ingest.parsers import ParseResult


class BaseParser(ABC):
    """Abstract base for all format parsers."""

    name: str = "base"
    format_id: str = "unknown"

    @abstractmethod
    def can_parse(self, raw_bytes: bytes, content_type: Optional[str] = None) -> float:
        """Return confidence (0.0-1.0) that this parser handles these bytes."""
        ...

    @abstractmethod
    def parse(self, raw_bytes: bytes) -> ParseResult:
        """Parse raw bytes into structured data + observables."""
        ...
