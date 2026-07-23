"""
Paragraph classifier — tags each document_paragraphs row with a content
type so the collapse pipeline can route it to the right extractor.

The old pipeline treated every paragraph as prose. That produced
bibliographic noise on academic docs (footnotes parsed as facts),
record-fragment noise on structured logs (CIA FOIA case logs), and
wasted Gemma calls on index/TOC pages.

This module classifies via fast regex heuristics first; LLM fallback
is used only for paragraphs that don't match any heuristic AND look
ambiguous.

Public surface:

  classify_text(text) -> str
      One-shot classifier. Returns one of:
        prose | record | bibliography | index | caption | header |
        table | transcript | boilerplate | unknown

  classify_paragraph(paragraph_id) -> dict
      Pulls the paragraph from DB, classifies, writes back.

  classify_document(document_id) -> dict
      Classify every paragraph in a doc. Idempotent.

  classify_all(limit=None) -> dict
      Classify every paragraph with paragraph_type='unknown' across
      the substrate. Used for backfill.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.documents.paragraph_classifier")

CLASSIFIER_VERSION = "heuristic_v1_2026_05_18"


# ─── Heuristic patterns ───────────────────────────────────────────────

# Bibliographic citation: typically starts with a numeric ID then author/
# title. Examples:
#   "117. AF Form 697, 'Identification Findings...'"
#   "236. ibid. 237. Kittinger. 238. Fulgham."
#   "Memorial Affairs Branch, Air Force Services Division, Accession No."
_BIBLIO_PATTERNS = [
    re.compile(r"^\s*\d{1,4}\.\s+[A-Z][a-zA-Z]"),                # "117. AF Form..."
    re.compile(r"^\s*\d+\.\s+ibid\b", re.IGNORECASE),            # "236. ibid."
    re.compile(r"\bAccession No\.?\b", re.IGNORECASE),
    re.compile(r"^[A-Z][a-zA-Z]+(\s[A-Z][a-zA-Z]+)?,\s+"
               r"[A-Z][a-zA-Z]+(\s+[A-Z][a-zA-Z]+)*,\s+"
               r"[A-Z][a-zA-Z]+"),                                # "Author, Title, Source"
]
# Multiple author-comma-author-comma patterns suggest bibliography
_BIBLIO_HEAVY_COMMA = re.compile(r"^([A-Z][a-zA-Z]+,?\s+){4,}")

# Structured record: date + ID + subject. CIA FOIA case logs look like:
#   "January 4, 2006 F-2006-00385 LEAVING S. VIETNAM FOR..."
#   "January 5, 2006 F-2006-00395 IGNAZIO SILONE"
# Case IDs vary: F_2006_00385, F-2006-00395, F_2oo6_oo385 (OCR garbled)
_RECORD_PATTERNS = [
    # Date + case ID (any separator + alphanumeric, OCR-tolerant)
    re.compile(r"^\s*[A-Z][a-z]+\s+\d{1,2},?\s+\d{4}\s+[A-Z][\w\d_\-]{4,}"),
    re.compile(r"^\s*\d{1,2}\s+[A-Z][a-z]+\s+\d{4}\s+[A-Z][\w\d_\-]+"),
    re.compile(r"^\s*[A-Z]{1,3}[-_]\d{4}[-_]\d{2,5}\b"),         # F-2006-00385
]

# Flight manifest row: a structured passenger-flight log line. Shapes seen
# in the Acme aircraft logs (text-layer PDFs):
#   "12 1/5/97 N123AA PBI TEB AA JD AF ..."   (seq + date + tail + route + pax)
#   "1/5/1997 PBI-TEB Acme, Doe"
# A date plus a departure/arrival airport-code pair is the decisive signal.
# Parsing these as prose makes the LLM hallucinate S-P-O facts at ~96% noise;
# they belong on the deterministic record path (extract_records → flight events).
_FLIGHT_PATTERNS = [
    # seq# + M/D/Y + ... + AAA BBB airport pair (3-letter IATA or 4-letter ICAO)
    re.compile(r"^\s*\d{1,4}\s+\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b.*\b[A-Z]{3,4}\b.{0,40}\b[A-Z]{3,4}\b"),
    # M/D/Y + AAA<sep>BBB route right after the date
    re.compile(r"^\s*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\s+[A-Z]{3,4}[ \-/][A-Z]{3,4}\b"),
]

# Index entry: "Name, FirstName ........ 47, 102, 195" or
# "Subject ......... 14, 22, 38"
_INDEX_PATTERNS = [
    re.compile(r"^[A-Z][a-zA-Z]+,\s+[A-Z][a-zA-Z]+.{0,50}\.{3,}.{0,20}\d+"),
    re.compile(r"^[A-Z][a-zA-Z ]{2,30}\s*\.{5,}\s*\d+"),
    re.compile(r"^[A-Z][a-zA-Z]+\s+\d+(,\s*\d+){3,}"),           # "Doe 14, 22, 38, 47"
]

# Caption: starts with Figure/Table/Photo
_CAPTION_PATTERN = re.compile(
    r"^\s*(figure|table|photo|plate|map|chart|fig\.|tbl\.)\s+\d+",
    re.IGNORECASE,
)

# Header / chapter / TOC / page number
_HEADER_PATTERNS = [
    re.compile(r"^\s*(chapter|section|part|appendix)\s+(\d+|[ivxIVX]+)\b",
               re.IGNORECASE),
    re.compile(r"^\s*page\s+\d+\s*$", re.IGNORECASE),
    re.compile(r"^\s*-?\s*\d+\s*-?\s*$"),                        # bare page number
    re.compile(r"^\s*\d+\s*$"),                                  # just a number
    re.compile(r"^Approved for Release", re.IGNORECASE),
    re.compile(r"^Page \d+\s+Approved", re.IGNORECASE),
]
# TOC line "Chapter Title ........ 78"
_TOC_PATTERN = re.compile(r"^[A-Z][a-zA-Z ]+\s+\.{3,}\s*\d+\s*$")

# Transcript (Q&A): "Q. ..." / "A. ..." / "SPEAKER:"
_TRANSCRIPT_PATTERNS = [
    re.compile(r"^\s*Q[.:]\s+", re.IGNORECASE),
    re.compile(r"^\s*A[.:]\s+", re.IGNORECASE),
    re.compile(r"^\s*[A-Z]{2,}\s*:\s+"),                         # "Acme: yes"
]


# Words that strongly signal prose (verbs + connectives)
_PROSE_VERBS = frozenset({
    "said", "told", "asked", "saw", "met", "went", "took", "gave",
    "knew", "believed", "thought", "called", "wrote", "reported",
    "investigated", "traveled", "arrived", "departed", "lived",
    "resided", "worked", "served", "founded", "owned", "managed",
    "controlled", "communicated", "spoke", "talked", "discussed",
    "stated", "claimed", "alleged", "denied", "described",
    "testified", "witnessed",
})


def _looks_like_prose(text: str) -> bool:
    """Quick prose check: has a known action verb + reasonable length."""
    if len(text) < 30:
        return False
    words = re.findall(r"\b\w+\b", text.lower())
    if len(words) < 8:
        return False
    return any(w in _PROSE_VERBS for w in words)


def classify_text(text: str) -> str:
    """Classify one paragraph by content type. Pure-function, no DB."""
    if not text:
        return "unknown"
    s = text.strip()
    if not s:
        return "unknown"

    # 1. Header / page-number / chapter
    for p in _HEADER_PATTERNS:
        if p.search(s):
            return "header"
    if _TOC_PATTERN.match(s):
        return "header"

    # 2. Caption
    if _CAPTION_PATTERN.match(s):
        return "caption"

    # 3. Index
    for p in _INDEX_PATTERNS:
        if p.match(s):
            return "index"

    # 4. Structured record — flight manifests (checked first; more specific)
    #    then CIA-case-log shape. Both ride the deterministic record path.
    for p in _FLIGHT_PATTERNS:
        if p.match(s):
            return "record"
    for p in _RECORD_PATTERNS:
        if p.match(s):
            return "record"

    # 5. Bibliography. Use .search (not .match) so "Accession No"
    # anywhere in the line triggers it, and accept short fragments —
    # bibliographies are often citation fragments that don't span 80 chars.
    for p in _BIBLIO_PATTERNS:
        if p.search(s):
            return "bibliography"
    if _BIBLIO_HEAVY_COMMA.match(s) and s.count(",") >= 4 and not _looks_like_prose(s):
        return "bibliography"

    # 6. Transcript Q&A
    for p in _TRANSCRIPT_PATTERNS:
        if p.match(s):
            return "transcript"

    # 7. Prose if it has action verbs + length
    if _looks_like_prose(s):
        return "prose"

    # 8. Short fragments default to "fragment" (skipped by extractors)
    if len(s) < 80:
        return "fragment"

    # 9. Long-ish but no verb hits → call it prose anyway (defer to LLM)
    return "prose"


def classify_paragraph(paragraph_id: int) -> dict:
    """Classify one paragraph, write paragraph_type back."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT raw_text FROM document_paragraphs WHERE paragraph_id = %s",
                (paragraph_id,),
            )
            row = cur.fetchone()
            if not row:
                return {"ok": False, "error": "not found"}
            text = row[0]
            ptype = classify_text(text or "")
            cur.execute(
                """
                UPDATE document_paragraphs
                   SET paragraph_type = %s,
                       classified_at = NOW(),
                       classifier_version = %s
                 WHERE paragraph_id = %s
                """,
                (ptype, CLASSIFIER_VERSION, paragraph_id),
            )
        conn.commit()
    return {"ok": True, "paragraph_type": ptype}


def classify_document(document_id: int) -> dict:
    """Classify every paragraph in a doc. Idempotent (overwrites existing
    classification — useful when classifier_version changes)."""
    counts: dict[str, int] = {}
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT paragraph_id, raw_text FROM document_paragraphs "
                " WHERE document_id = %s ORDER BY paragraph_id",
                (document_id,),
            )
            rows = cur.fetchall()
            for pid, text in rows:
                ptype = classify_text(text or "")
                counts[ptype] = counts.get(ptype, 0) + 1
                cur.execute(
                    """
                    UPDATE document_paragraphs
                       SET paragraph_type = %s,
                           classified_at = NOW(),
                           classifier_version = %s
                     WHERE paragraph_id = %s
                    """,
                    (ptype, CLASSIFIER_VERSION, pid),
                )
        conn.commit()
    return {"document_id": document_id, "total": sum(counts.values()),
            "by_type": counts}


def classify_all(limit: Optional[int] = None, batch: int = 5000) -> dict:
    """Backfill: classify every paragraph with paragraph_type='unknown'.
    Processes in batches to avoid memory pressure."""
    total_counts: dict[str, int] = {}
    processed = 0
    while True:
        if limit and processed >= limit:
            break
        b = batch if not limit else min(batch, limit - processed)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT paragraph_id, raw_text FROM document_paragraphs "
                    " WHERE paragraph_type = 'unknown' LIMIT %s",
                    (b,),
                )
                rows = cur.fetchall()
                if not rows:
                    break
                updates = []
                for pid, text in rows:
                    ptype = classify_text(text or "")
                    total_counts[ptype] = total_counts.get(ptype, 0) + 1
                    updates.append((ptype, pid))
                # Batch UPDATE via execute_values would be faster but
                # individual UPDATEs are fine at this scale.
                for ptype, pid in updates:
                    cur.execute(
                        """
                        UPDATE document_paragraphs
                           SET paragraph_type = %s,
                               classified_at = NOW(),
                               classifier_version = %s
                         WHERE paragraph_id = %s
                        """,
                        (ptype, CLASSIFIER_VERSION, pid),
                    )
                conn.commit()
            processed += len(rows)
        log.info("Classified %d paragraphs (cumulative); %s", processed, total_counts)
    return {"processed": processed, "by_type": total_counts}
