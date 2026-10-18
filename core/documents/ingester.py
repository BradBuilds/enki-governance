"""
Document ingester — persists a document + its hierarchical structure
(sections, pages, paragraphs) into the schema added by migration 053.

The ingester does NOT do OCR or LLM extraction. It accepts already-extracted
text (one per-page string) plus optional structural hints, and writes the
graph rows. The OCR / LLM-vision / comparator path lives in the
per-doc-type workers (Phase 2.5).

Typical call shape:

    doc_id = persist_document({
        "tenant_id": "default",
        "dataset_id": "example_corpus_2024",
        "source_type": "pdf",
        "title": "Example trial exhibit 47",
        "docket_number": "1:23-cr-00000",
        "release_date": "2024-01-03",
        "page_count": 442,
        "extraction_method": "ocr_paddle",
        "ocr_quality": 0.94,
        "on_disk_path": "/opt/enki/corpus/Acme/exh47.pdf",
        "source_url": "...",
        "trust_class": "review_required",
    })

    for page_num, page_text in enumerate(page_texts, start=1):
        page_id = persist_page(
            document_id=doc_id, page_number=page_num,
            raw_text=page_text, ocr_confidence=0.93,
        )
        for order, paragraph_text in enumerate(split_paragraphs(page_text)):
            persist_paragraph(
                document_id=doc_id, page_id=page_id,
                order_index=order, raw_text=paragraph_text,
            )

Boilerplate detection (SimHash + corpus_term_frequency) runs as a
post-ingestion job — see `core.documents.boilerplate`.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.documents.ingester")

_PARA_SPLIT_RE = re.compile(r"\n\s*\n+")

# Sub-paragraph patterns that SHOULD be extracted as their own paragraph so
# SimHash clustering can find them duplicated across documents. These are
# the kinds of strings that adversarial-doc producers (court releases,
# FOIA dumps, large litigation corpora) put on every page:
#
#   - Bates stamps:        EFTA02846578, USCIS-1234-5678, DOJ-OIG-0042
#   - Page numbers:        "Page 4 of 12"  /  bare "4"
#   - Redaction markers:   "*Redactions for ...", "[REDACTED]", "(b)(6) (b)(7)(C)"
#   - Form section heads:  "Part 4. Information About Beneficiary"
#
# These patterns must match a WHOLE line. We compile them once and use them
# inside _split_smart() below.
_BOILERPLATE_LINE_PATTERNS = [
    # Bates-style identifiers (>= 6 alnum chars, often hyphen-separated)
    re.compile(r"^[A-Z][A-Z0-9]{2,}[\-_]?\d{4,}$"),
    re.compile(r"^[A-Z]{2,}\d{6,}$"),
    # "Page N of M" / standalone page numbers
    re.compile(r"^Page\s+\d+\s+of\s+\d+$", re.IGNORECASE),
    re.compile(r"^\d{1,4}$"),
    # Redaction headers / FOIA exemption codes
    re.compile(r"^\*?Redactions?\s+for\b.*", re.IGNORECASE),
    re.compile(r"^\[\s*REDACTED\s*\]$", re.IGNORECASE),
    re.compile(r"^\(\s*b\s*\)\s*\(\s*[0-9]+\s*\)(\s*\(\s*[A-Z]\s*\))?.*$"),
    # Form section markers ("Part N." / "Section N.")
    re.compile(r"^Part\s+\d+\.\s.{0,200}$"),
    re.compile(r"^Section\s+\d+\.\s.{0,200}$"),
]

# A "paragraph" longer than this is suspicious for adversarial docs — usually
# means pdfplumber lost blank-line breaks. When this happens, fall through to
# a line-level split with smart re-merging of very-short fragments.
_PARA_LONG_THRESHOLD = 600  # chars

# Minimum chars to keep a line as its own paragraph (avoids 1-char artifacts).
_MIN_LINE_KEEP = 3


def _is_boilerplate_line(line: str) -> bool:
    """True iff this single line is a known boilerplate pattern that should
    be its own paragraph so clustering can find duplicates."""
    s = line.strip()
    if not s:
        return False
    return any(p.match(s) for p in _BOILERPLATE_LINE_PATTERNS)


def _split_smart(text: str) -> list[str]:
    """Smarter paragraph splitter for adversarial docs.

    Step 1: Split on blank lines (the normal case).
    Step 2: Within each block, extract recognized boilerplate lines as their
            own paragraphs (so they cluster).
    Step 3: If the resulting block is still > _PARA_LONG_THRESHOLD chars
            AND contains many single-line breaks, fall back to line-level
            split with adjacent-short-line merging.

    Returns a flat list of paragraph strings, in order.
    """
    out: list[str] = []
    for block in _PARA_SPLIT_RE.split(text):
        block = block.strip()
        if not block:
            continue
        # Pass 1: pull boilerplate lines out
        non_boiler_lines: list[str] = []
        for line in block.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            if _is_boilerplate_line(stripped):
                out.append(stripped)
            else:
                non_boiler_lines.append(stripped)
        if not non_boiler_lines:
            continue
        remainder = "\n".join(non_boiler_lines)
        # Pass 2: if remainder is short enough, keep as one paragraph.
        if len(remainder) <= _PARA_LONG_THRESHOLD:
            out.append(remainder)
            continue
        # Pass 3: long block with many line breaks → line-level split, but
        # merge short adjacent lines into one paragraph so we don't make 100
        # 1-word "paragraphs". Heuristic: if a line is shorter than 50 chars,
        # join it to the previous line.
        merged: list[str] = []
        for line in non_boiler_lines:
            if len(line) < _MIN_LINE_KEEP:
                continue
            if merged and len(line) < 50 and not line[0].isupper():
                merged[-1] = merged[-1] + " " + line
            elif merged and len(merged[-1]) < 50:
                merged[-1] = merged[-1] + " " + line
            else:
                merged.append(line)
        out.extend(merged)
    return out


# ─── Document-level ─────────────────────────────────────────────────────

def _strip_nuls(value):
    """Postgres text/jsonb reject the NUL byte (\\u0000). Some PDF
    extractors leak it into title/author/metadata. Drop it recursively."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {k: _strip_nuls(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_nuls(v) for v in value]
    return value


def persist_document(doc: dict) -> int:
    """Insert (or upsert by hash) a document row. Returns document_id."""
    doc = _strip_nuls(doc)
    raw = doc.get("raw_bytes")
    content_hash = doc.get("hash")
    if raw and not content_hash:
        content_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
    # content_sha256 is the bare 64-hex key the federation extraction-sharing
    # path uses (extract once, peers consume by sha). The legacy ingest only set
    # the prefixed `hash`, so these docs were invisible to sharing and every node
    # re-ran its own LLM pass — a cross-node divergence source. Derive the bare
    # hex from the same digest so EVERY ingest path is shareable.
    content_sha256 = None
    if content_hash:
        content_sha256 = content_hash.split(":", 1)[1] if ":" in content_hash else content_hash
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO documents
                    (tenant_id, dataset_id, source_type, hash, content_sha256, title,
                     docket_number, bates_range, jurisdiction, publisher,
                     author, release_date, document_date, page_count,
                     word_count, ocr_quality, extraction_method,
                     source_url, on_disk_path, metadata, trust_class)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (hash) DO UPDATE SET
                    content_sha256 = COALESCE(EXCLUDED.content_sha256, documents.content_sha256),
                    title = COALESCE(EXCLUDED.title, documents.title),
                    page_count = COALESCE(EXCLUDED.page_count, documents.page_count),
                    extraction_method = COALESCE(EXCLUDED.extraction_method,
                                                  documents.extraction_method),
                    ocr_quality = COALESCE(EXCLUDED.ocr_quality, documents.ocr_quality),
                    last_refreshed_at = NOW()
                RETURNING document_id
                """,
                (
                    doc.get("tenant_id", "default"),
                    doc.get("dataset_id"),
                    doc.get("source_type"),
                    content_hash,
                    content_sha256,
                    doc.get("title"),
                    doc.get("docket_number"),
                    doc.get("bates_range"),
                    doc.get("jurisdiction"),
                    doc.get("publisher"),
                    doc.get("author"),
                    doc.get("release_date"),
                    doc.get("document_date"),
                    doc.get("page_count"),
                    doc.get("word_count"),
                    doc.get("ocr_quality"),
                    doc.get("extraction_method"),
                    doc.get("source_url"),
                    doc.get("on_disk_path"),
                    __import__("json").dumps(doc.get("metadata") or {}),
                    doc.get("trust_class", "mentioned"),
                ),
            )
            doc_id = cur.fetchone()[0]
        conn.commit()
    return doc_id


def persist_section(
    document_id: int, kind: str, heading: str, order_index: int,
    parent_section_id: Optional[int] = None,
    start_page: Optional[int] = None, end_page: Optional[int] = None,
) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO document_sections
                    (document_id, parent_section_id, kind, heading,
                     order_index, start_page, end_page)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING section_id
                """,
                (document_id, parent_section_id, kind, heading,
                 order_index, start_page, end_page),
            )
            section_id = cur.fetchone()[0]
        conn.commit()
    return section_id


def persist_page(
    document_id: int, page_number: int, raw_text: str,
    ocr_confidence: Optional[float] = None,
    raw_text_llm: Optional[str] = None,
    extraction_method: Optional[str] = None,
    has_redactions: bool = False,
    section_id: Optional[int] = None,
    on_disk_image: Optional[str] = None,
) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO document_pages
                    (document_id, section_id, page_number, raw_text,
                     raw_text_llm, ocr_confidence, extraction_method,
                     has_redactions, on_disk_image)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING page_id
                """,
                (document_id, section_id, page_number, _strip_nuls(raw_text),
                 _strip_nuls(raw_text_llm), ocr_confidence, extraction_method,
                 has_redactions, on_disk_image),
            )
            page_id = cur.fetchone()[0]
        conn.commit()
    return page_id


def persist_paragraph(
    document_id: int, page_id: int, order_index: int, raw_text: str,
    simhash: Optional[int] = None,
    is_redacted: bool = False,
) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO document_paragraphs
                    (page_id, document_id, order_index, raw_text,
                     simhash, is_redacted, word_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING paragraph_id
                """,
                (page_id, document_id, order_index, _strip_nuls(raw_text),
                 simhash, is_redacted, len((raw_text or "").split())),
            )
            paragraph_id = cur.fetchone()[0]
        conn.commit()
    return paragraph_id


def split_paragraphs(text: str) -> list[str]:
    """Paragraph splitter tuned for adversarial docs.

    Three-pass behavior in _split_smart():
      1. Blank-line split (the normal case for born-digital text).
      2. Within each block, recognized boilerplate lines (Bates stamps,
         redaction markers, page numbers, form section headers) are pulled
         out as their own paragraph so SimHash clustering can find them
         duplicated across documents and flag them as boilerplate.
      3. Blocks that survive the first two passes but are still very long
         AND line-break-heavy (typical of scanned/OCR'd court releases)
         fall through to a line-level split with adjacent-short-line
         merging, so we don't create 100 one-word paragraphs.

    For docs with clean blank-line paragraphs this behaves like the old
    splitter. For Acme/FOIA-class docs it produces meaningful sub-page
    paragraphs."""
    if not text:
        return []
    return _split_smart(text)


def persist_pages_with_paragraphs(
    document_id: int,
    page_texts: list[str],
    ocr_confidences: Optional[list[float]] = None,
    extraction_method: str = "unknown",
) -> dict:
    """Convenience: ingest a full document's pages + paragraphs in one call.
    Returns a summary dict."""
    page_ids = []
    paragraph_count = 0
    for idx, text in enumerate(page_texts):
        conf = ocr_confidences[idx] if ocr_confidences and idx < len(ocr_confidences) else None
        page_id = persist_page(
            document_id, idx + 1, text,
            ocr_confidence=conf,
            extraction_method=extraction_method,
        )
        page_ids.append(page_id)
        for order, paragraph_text in enumerate(split_paragraphs(text)):
            persist_paragraph(document_id, page_id, order, paragraph_text)
            paragraph_count += 1
    return {
        "document_id": document_id,
        "pages_indexed": len(page_ids),
        "paragraphs_indexed": paragraph_count,
    }
