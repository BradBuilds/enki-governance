"""
Shared types for domain-aware extractors.

These mirror (a subset of) the live extraction tables — document_mentions
and statements — but are framed as PROPOSED output so callers can stage
them in shadow mode without writing to the live catalog.

A ProposedMention is what would become one document_mentions row.
A ProposedStatement is what would become one statements row.

Character offsets reference the concatenated document text the extractor
was given (page_text joined with "\n\n"), so callers re-using the same
joining convention will get offsets that line up with the live text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ProposedMention:
    surface_form:       str
    surface_form_norm:  str
    suggested_type:     str          # person | place | organization | facility | ...
    char_start:         int
    char_end:           int
    confidence:         float
    extraction_method:  str          # e.g. "amadeus_pnr_parser_v1"
    page_number:        Optional[int] = None
    metadata:           dict = field(default_factory=dict)


@dataclass
class ProposedStatement:
    predicate:          str
    raw_text:           str
    char_start:         int
    char_end:           int
    confidence:         float
    extraction_method:  str
    speaker_surface:    Optional[str] = None    # who/what the statement is about
    object_surface:     Optional[str] = None    # the other side of the predicate, if any
    event_time_iso:     Optional[str] = None    # when this happened (ISO 8601), if extractable
    page_number:        Optional[int] = None
    metadata:           dict = field(default_factory=dict)


@dataclass
class RedactionSpan:
    """Character range the agency redacted (e.g. FOIA b(6) personal-
    privacy black bars). The text inside is still in the raw bytes —
    we just flag the range so it never gets surfaced as a mention."""
    char_start:    int
    char_end:      int
    exemption:     str          # e.g. "(b)(6)", "(b)(7)(C)"
    page_number:   Optional[int] = None


@dataclass
class ExtractionOutput:
    document_id:    int
    extractor:      str          # e.g. "amadeus_pnr_parser_v1"
    mentions:       list[ProposedMention]
    statements:     list[ProposedStatement]
    redactions:     list[RedactionSpan]
    parser_notes:   list[str] = field(default_factory=list)

    def summary(self) -> dict:
        from collections import Counter
        mtype = Counter(m.suggested_type for m in self.mentions)
        pred  = Counter(s.predicate for s in self.statements)
        return {
            "document_id":     self.document_id,
            "extractor":       self.extractor,
            "mentions_total":  len(self.mentions),
            "mentions_by_type": dict(mtype),
            "statements_total": len(self.statements),
            "statements_by_predicate": dict(pred),
            "redactions_total": len(self.redactions),
            "parser_notes":    self.parser_notes,
        }
