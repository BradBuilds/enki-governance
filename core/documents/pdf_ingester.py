"""
PDF ingester — born-digital PDFs → documents/pages/paragraphs in PG.

Uses pdfplumber for text extraction (already in requirements.txt). Per page:
  - extract text
  - compute average char-confidence (None for born-digital, used for OCR'd)
  - detect "no extractable text" → page flagged for OCR worker
  - split into paragraphs (default: blank-line separation)
  - stamp each paragraph with a SimHash for downstream boilerplate clustering

Does NOT attempt OCR. Scanned-only PDFs land with `extraction_method='pending_ocr'`
on the document row and `raw_text=''` on each page; the OCR worker picks them
up later. This keeps the born-digital fast path zero-cost.

Typical call:

    from core.documents.pdf_ingester import ingest_pdf
    summary = ingest_pdf(
        path="/opt/enki/corpus/aaro/aaro_2024_report.pdf",
        dataset_id="aaro_releases",
        tenant_id="default",
        title="AARO Annual Report 2024",
        trust_class="mentioned",
        source_url="https://www.defense.gov/...",
    )
    # → {"document_id": 17, "pages": 42, "paragraphs": 312,
    #    "scanned_pages": 0, "extraction_method": "pdfplumber",
    #    "skipped": False, "reason": None}

After ingestion, the document is ready for mention extraction
(core/documents/extractor.py) and statement extraction.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Optional

from core.documents.ingester import (
    persist_document, persist_page, persist_paragraph,
    split_paragraphs,
)
from core.documents.simhash import simhash, signed_64

log = logging.getLogger("enki.documents.pdf_ingester")

# Pages with fewer than this many extractable characters are treated as
# image-only (scanned). They land with raw_text='' and trigger the OCR worker.
_MIN_TEXT_CHARS_PER_PAGE = 50

# pdfplumber occasionally returns a page text that is ONLY whitespace+ligature
# artifacts. Strip and re-check.
_WHITESPACE_RE = re.compile(r"\s+")

# A page can carry a text layer that is pure OCR gibberish ("_ __ ___ii ‘-I",
# "I~£§U££'RA") — plenty of characters, so a length check calls it born-digital
# and OCR never runs. These signals catch that: real prose is mostly letters in
# multi-letter words; a junk layer is symbols and stray single chars. Content-
# derived and deterministic (no lexicon needed for this coarse gate — the fine
# per-paragraph judgement is text_quality's job downstream).
_WORDLIKE_RE = re.compile(r"[A-Za-z]{3,}")
_MIN_ALPHA_RATIO = 0.5        # letters / non-space chars
_MIN_WORDLIKE_RATIO = 0.35    # ≥3-letter tokens / all tokens


def _looks_like_real_text(stripped: str) -> bool:
    """Coarse gibberish gate: is this a real text layer, or scanned-image junk?"""
    non_space = [c for c in stripped if not c.isspace()]
    if not non_space:
        return False
    alpha_ratio = sum(c.isalpha() for c in non_space) / len(non_space)
    if alpha_ratio < _MIN_ALPHA_RATIO:
        return False
    toks = stripped.split()
    if not toks:
        return False
    wordlike = sum(1 for t in toks if _WORDLIKE_RE.fullmatch(t) or _WORDLIKE_RE.search(t))
    return (wordlike / len(toks)) >= _MIN_WORDLIKE_RATIO


def _hash_file(path: Path) -> str:
    """SHA-256 of the file contents. Used as the document's content hash for
    dedupe-on-upsert."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _is_extractable(text: str) -> bool:
    """True iff the page yielded enough REAL text to skip OCR. A page must have
    both enough characters AND a text layer that reads like real words — a
    scanned page whose embedded text is OCR gibberish is routed to OCR instead."""
    if not text:
        return False
    stripped = _WHITESPACE_RE.sub(" ", text).strip()
    if len(stripped) < _MIN_TEXT_CHARS_PER_PAGE:
        return False
    return _looks_like_real_text(stripped)


def ingest_pdf(
    *,
    path: str,
    dataset_id: str,
    tenant_id: str = "default",
    title: Optional[str] = None,
    docket_number: Optional[str] = None,
    bates_range: Optional[str] = None,
    jurisdiction: Optional[str] = None,
    publisher: Optional[str] = None,
    author: Optional[str] = None,
    release_date: Optional[str] = None,
    document_date: Optional[str] = None,
    source_url: Optional[str] = None,
    trust_class: str = "mentioned",
    paragraph_splitter: Optional[callable] = None,
    metadata: Optional[dict] = None,
) -> dict:
    """Ingest one PDF. Returns a summary dict. Idempotent on file hash:
    re-ingesting the same file updates the existing document row.

    paragraph_splitter: optional override callable(page_text) -> list[str]
    when a specific doc class needs custom segmentation (legal headers,
    Q-and-A transcripts, etc.). Default is blank-line split.
    """
    pdf_path = Path(path)
    if not pdf_path.exists():
        return {"ok": False, "error": f"file not found: {path}"}

    splitter = paragraph_splitter or split_paragraphs

    try:
        import pdfplumber
    except ImportError:
        return {"ok": False, "error": "pdfplumber not installed in this environment"}

    content_hash = _hash_file(pdf_path)
    file_bytes = pdf_path.stat().st_size

    # First pass: count pages + extract text per page.
    # Layout-aware normalization recovers true reading order for multi-column
    # pages (condensed transcripts, mini-scripts) that a bare extract_text()
    # would flatten into cross-column jumble. Deterministic, geometry-driven,
    # safe fallback to extract_text() on any failure. See layout_normalizer.
    from core.documents.layout_normalizer import normalize_page

    page_texts: list[str] = []
    scanned_pages: list[int] = []
    page_layouts: list[dict] = []
    total_chars = 0

    with pdfplumber.open(str(pdf_path)) as pdf:
        for i, page in enumerate(pdf.pages):
            try:
                text, layout = normalize_page(page)
            except Exception as e:
                log.warning("pdfplumber failed on page %d of %s: %s",
                            i + 1, pdf_path.name, e)
                text, layout = "", {"columns": 0, "tabular": False, "error": str(e)}
            page_texts.append(text)
            page_layouts.append(layout)
            if not _is_extractable(text):
                scanned_pages.append(i + 1)
            else:
                total_chars += len(text)

        # Pull document-level metadata if we can
        meta = pdf.metadata or {}

    page_count = len(page_texts)
    word_count = sum(len(t.split()) for t in page_texts)
    fully_scanned = len(scanned_pages) == page_count and page_count > 0

    # Doc-level method is 'pending_ocr' whenever ANY page needs OCR — the OCR
    # worker claims by the document row, then fills only the pending pages (good
    # pages keep their pdfplumber text). Without this, a MIXED doc (some real
    # pages, some scanned) was never claimed and its scanned pages stayed blank
    # forever — the common shape for partially-redacted archive scans.
    extraction_method = "pending_ocr" if scanned_pages else "pdfplumber"

    # Persist document
    doc_id = persist_document({
        "tenant_id": tenant_id,
        "dataset_id": dataset_id,
        "source_type": "pdf",
        "hash": content_hash,
        "title": title or meta.get("Title") or pdf_path.stem,
        "docket_number": docket_number,
        "bates_range": bates_range,
        "jurisdiction": jurisdiction,
        "publisher": publisher or meta.get("Producer") or meta.get("Creator"),
        "author": author or meta.get("Author"),
        "release_date": release_date,
        "document_date": document_date,
        "page_count": page_count,
        "word_count": word_count,
        "extraction_method": extraction_method,
        "source_url": source_url,
        "on_disk_path": str(pdf_path.resolve()),
        "metadata": {
            **(metadata or {}),
            "file_bytes": file_bytes,
            "pdf_metadata": {k: str(v) for k, v in meta.items()},
            "scanned_pages_count": len(scanned_pages),
            "layout": {
                "extractor": "layout_normalizer",
                "multicolumn_pages": sum(1 for L in page_layouts
                                         if (L.get("columns") or 0) > 1),
                "tabular_pages": sum(1 for L in page_layouts if L.get("tabular")),
            },
        },
        "trust_class": trust_class,
    })

    log.info(
        "PDF ingested: %s document_id=%d pages=%d scanned=%d hash=%s...",
        pdf_path.name, doc_id, page_count, len(scanned_pages), content_hash[:16],
    )

    # If fully scanned: write empty page placeholders so the OCR worker
    # knows what pages exist, but do not persist paragraphs (they'll come
    # from OCR'd text later).
    if fully_scanned:
        for i in range(page_count):
            persist_page(
                document_id=doc_id, page_number=i + 1, raw_text="",
                extraction_method="pending_ocr",
                has_redactions=False,
            )
        return {
            "ok": True,
            "document_id": doc_id,
            "pages": page_count,
            "paragraphs": 0,
            "scanned_pages": page_count,
            "extraction_method": extraction_method,
            "skipped": False,
            "needs_ocr": True,
            "content_hash": content_hash,
        }

    # Mixed or fully-born-digital: persist pages + paragraphs with SimHash
    paragraph_count = 0
    for i, text in enumerate(page_texts):
        page_no = i + 1
        is_scanned_page = not _is_extractable(text)
        page_id = persist_page(
            document_id=doc_id,
            page_number=page_no,
            raw_text="" if is_scanned_page else text,
            extraction_method="pending_ocr" if is_scanned_page else "pdfplumber",
            has_redactions=False,
        )
        if is_scanned_page:
            # We DO record this page so the OCR worker can later fill it in;
            # we just don't try to make paragraphs from empty text.
            continue
        for order, paragraph_text in enumerate(splitter(text)):
            sig = signed_64(simhash(paragraph_text))
            persist_paragraph(
                document_id=doc_id,
                page_id=page_id,
                order_index=order,
                raw_text=paragraph_text,
                simhash=sig,
                is_redacted=False,
            )
            paragraph_count += 1

    return {
        "ok": True,
        "document_id": doc_id,
        "pages": page_count,
        "paragraphs": paragraph_count,
        "scanned_pages": len(scanned_pages),
        "extraction_method": extraction_method,
        "skipped": False,
        "needs_ocr": len(scanned_pages) > 0,
        "content_hash": content_hash,
    }


def ingest_pdf_directory(
    *,
    directory: str,
    dataset_id: str,
    tenant_id: str = "default",
    trust_class: str = "mentioned",
    glob: str = "*.pdf",
    limit: Optional[int] = None,
) -> dict:
    """Ingest every PDF in a directory tree. Yields a summary per file plus
    an aggregate."""
    base = Path(directory)
    if not base.exists():
        return {"ok": False, "error": f"directory not found: {directory}"}

    paths = sorted(base.rglob(glob))
    if limit:
        paths = paths[:limit]

    results = []
    totals = {"documents": 0, "pages": 0, "paragraphs": 0, "scanned": 0,
              "needs_ocr": 0, "failed": 0}

    for p in paths:
        r = ingest_pdf(
            path=str(p),
            dataset_id=dataset_id,
            tenant_id=tenant_id,
            trust_class=trust_class,
        )
        results.append({"file": str(p.relative_to(base)), **r})
        if r.get("ok"):
            totals["documents"] += 1
            totals["pages"] += r.get("pages", 0)
            totals["paragraphs"] += r.get("paragraphs", 0)
            totals["scanned"] += r.get("scanned_pages", 0)
            if r.get("needs_ocr"):
                totals["needs_ocr"] += 1
        else:
            totals["failed"] += 1

    return {"ok": True, "directory": str(base), "totals": totals, "results": results}
