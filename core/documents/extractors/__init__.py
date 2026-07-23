"""
Domain-aware extractors per document_class.

The generic LLM extractor works well for unstructured prose (news,
research, court filings) but produces noise on structured records
(GDS PNRs, tabular data, FOIA-redacted government forms). For those,
a small dedicated parser yields cleaner output than any LLM would.

Public API:

  extract(document_id: int, text: str, document_class: str) -> ExtractionOutput

Dispatches by class. Unknown classes return None (caller should fall
back to the generic extractor). Per-class extractors live in submodules:

  pnr     — Amadeus / Sabre PNR booking records
  (future) court, news, government, email, etc.
"""

from __future__ import annotations

from typing import Optional

from .types import ExtractionOutput
from . import pnr


def extract(*, document_id: int, text: str, document_class: str) -> Optional[ExtractionOutput]:
    """Return per-class extraction or None for unknown / generic."""
    if document_class == "gds_booking_record":
        return pnr.extract(document_id=document_id, text=text)
    return None
