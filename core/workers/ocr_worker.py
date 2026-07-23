"""
OCR Worker — fills in raw_text for pages flagged extraction_method='pending_ocr'.

How the pipeline gets here:
  pdf_ingester runs pdfplumber on the PDF. Pages with extractable text land
  with raw_text='...'; scanned pages land with raw_text='' and
  extraction_method='pending_ocr'. This worker picks them up, renders each
  page to PNG via poppler, runs PaddleOCR (with Ollama-vision fallback on
  low-confidence pages), then writes raw_text + paragraphs back.

Worker discipline:
  - Claims one document per tick (the oldest pending) so two workers can't
    race. Uses an UPDATE...RETURNING with a status flip on documents.
  - One page at a time → flat memory regardless of doc size.
  - On each page: write raw_text, ocr_confidence, extraction_method, and
    split into paragraphs via core.documents.ingester.persist_paragraph.
  - After all pages OCR'd: aggregate ocr_quality on the document row.
  - On failure: status flips to 'ocr_failed' with the error captured in
    metadata. Operator can retry by flipping back to 'pending_ocr'.

Tier-3 worker (heavy, GPU-friendly). Pauses cleanly under pressure via the
manager. Single-threaded — Paddle's own batching is fine for one process,
and Ollama is single-slot anyway.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger("enki.ocr_worker")

POLL_INTERVAL = int(os.getenv("ENKI_OCR_POLL_INTERVAL", "20"))
MAX_PAGES_PER_DOC = int(os.getenv("ENKI_OCR_MAX_PAGES_PER_DOC", "0"))  # 0 = no cap

_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_last_status: dict = {
    "running": False,
    "last_tick": None,
    "docs_processed": 0,
    "pages_processed": 0,
    "pages_failed": 0,
    "current_document_id": None,
    "current_page_number": None,
    "last_error": None,
}


# ─── DB helpers ─────────────────────────────────────────────────────────

def _claim_next_document(tenant_id: str) -> Optional[dict]:
    """Pick the oldest pending document and flip it to 'ocr_in_progress' atomically.
    Returns the document row (id, path, page_count) or None if nothing pending."""
    from storage.postgres.db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE documents
                   SET extraction_method = 'ocr_in_progress'
                 WHERE document_id = (
                       SELECT document_id FROM documents
                        WHERE tenant_id = %s
                          AND extraction_method = 'pending_ocr'
                          AND on_disk_path IS NOT NULL
                        ORDER BY created_at ASC
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                   )
                RETURNING document_id, on_disk_path, page_count, dataset_id
                """,
                (tenant_id,),
            )
            row = cur.fetchone()
        conn.commit()
    if not row:
        return None
    return {
        "document_id": row[0],
        "on_disk_path": row[1],
        "page_count": row[2],
        "dataset_id": row[3],
    }


def _list_pending_pages(document_id: int) -> list[dict]:
    """Pages on this doc that still need OCR."""
    from storage.postgres.db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT page_id, page_number
                  FROM document_pages
                 WHERE document_id = %s
                   AND extraction_method = 'pending_ocr'
                 ORDER BY page_number ASC
                """,
                (document_id,),
            )
            return [
                {"page_id": r[0], "page_number": r[1]}
                for r in cur.fetchall()
            ]


def _save_page_result(
    page_id: int, document_id: int, raw_text: str,
    confidence: float, engine: str, has_redactions: bool,
) -> None:
    """Update the page row + write paragraphs. One transaction."""
    from core.documents.ingester import split_paragraphs, persist_paragraph
    from core.documents.simhash import simhash, signed_64
    from storage.postgres.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE document_pages
                   SET raw_text = %s,
                       ocr_confidence = %s,
                       extraction_method = %s,
                       has_redactions = %s
                 WHERE page_id = %s
                """,
                (raw_text, confidence, engine, has_redactions, page_id),
            )
            # Drop any stale paragraphs that might have been written for this
            # page before (re-OCR case). Paragraphs are cheap to rewrite.
            cur.execute(
                "DELETE FROM document_paragraphs WHERE page_id = %s",
                (page_id,),
            )
            for order, paragraph_text in enumerate(split_paragraphs(raw_text)):
                sig = signed_64(simhash(paragraph_text))
                cur.execute(
                    """
                    INSERT INTO document_paragraphs
                        (page_id, document_id, order_index, raw_text,
                         simhash, is_redacted, word_count)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (page_id, document_id, order, paragraph_text,
                     sig, False, len(paragraph_text.split())),
                )
        conn.commit()


def _finalize_document(document_id: int, ok: bool, error: Optional[str] = None) -> None:
    """After all pages handled: compute aggregate ocr_quality and flip
    the document's extraction_method to its final value."""
    from storage.postgres.db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            if not ok:
                cur.execute(
                    """
                    UPDATE documents
                       SET extraction_method = 'ocr_failed',
                           metadata = jsonb_set(
                              COALESCE(metadata, '{}'::jsonb),
                              '{ocr_error}', to_jsonb(%s::text)
                           )
                     WHERE document_id = %s
                    """,
                    (error or "unknown", document_id),
                )
                conn.commit()
                return

            # Aggregate per-page confidence + engine fingerprint.
            cur.execute(
                """
                SELECT AVG(ocr_confidence)::REAL,
                       array_agg(DISTINCT extraction_method)
                  FROM document_pages
                 WHERE document_id = %s
                   AND ocr_confidence IS NOT NULL
                """,
                (document_id,),
            )
            avg_conf, methods = cur.fetchone()
            # Use the highest-fidelity method that fired as the doc-level marker.
            method_priority = [
                "llm_vision", "ocr_paddle+llm_vision", "ocr_paddle",
            ]
            doc_method = "ocr_paddle"
            for m in method_priority:
                if methods and m in methods:
                    doc_method = m
                    break
            cur.execute(
                """
                UPDATE documents
                   SET extraction_method = %s,
                       ocr_quality = %s
                 WHERE document_id = %s
                """,
                (doc_method, avg_conf, document_id),
            )
        conn.commit()


# ─── Page processing ────────────────────────────────────────────────────

def _process_page(
    pdf_path: str, page_id: int, page_number: int, document_id: int,
) -> tuple[bool, Optional[str]]:
    """Render + OCR one page; persist result. Returns (ok, error_or_none)."""
    from core.ocr.engines import extract_page
    from core.ocr.pdf_renderer import render_page

    png = render_page(pdf_path, page_number)
    if png is None:
        return False, f"render failed for page {page_number}"

    try:
        result = extract_page(png)
    except Exception as e:
        return False, f"extract_page raised: {e}"

    _save_page_result(
        page_id=page_id,
        document_id=document_id,
        raw_text=result.get("text", ""),
        confidence=result.get("confidence", 0.0),
        engine=result.get("engine", "ocr_paddle"),
        has_redactions=result.get("redactions_present", False),
    )
    return True, None


def _process_document(doc: dict) -> dict:
    """Walk every pending page for a document. Returns a summary."""
    pdf_path = doc["on_disk_path"]
    document_id = doc["document_id"]

    pages = _list_pending_pages(document_id)
    if not pages:
        # Pages were never written (corrupt state) — mark the doc failed so
        # operators can investigate rather than silently re-claiming.
        _finalize_document(document_id, ok=False, error="no pending pages")
        return {"document_id": document_id, "pages_ok": 0, "pages_failed": 0,
                "status": "no_pending_pages"}

    if MAX_PAGES_PER_DOC > 0:
        pages = pages[:MAX_PAGES_PER_DOC]

    pages_ok = 0
    pages_failed = 0
    first_error: Optional[str] = None

    for p in pages:
        if _stop_event.is_set():
            # Graceful shutdown: leave the doc in 'ocr_in_progress' so the
            # next start picks up where we left off — pages already saved
            # are no longer 'pending_ocr' so they won't be reprocessed.
            return {
                "document_id": document_id, "pages_ok": pages_ok,
                "pages_failed": pages_failed, "status": "interrupted",
            }
        _last_status["current_document_id"] = document_id
        _last_status["current_page_number"] = p["page_number"]

        ok, err = _process_page(
            pdf_path=pdf_path,
            page_id=p["page_id"],
            page_number=p["page_number"],
            document_id=document_id,
        )
        if ok:
            pages_ok += 1
            _last_status["pages_processed"] += 1
        else:
            pages_failed += 1
            _last_status["pages_failed"] += 1
            if first_error is None:
                first_error = err
            logger.warning("OCR page failure doc=%s page=%s: %s",
                           document_id, p["page_number"], err)

    # Only mark the whole document failed if EVERY page failed. Partial
    # progress on a 442-pager is still useful.
    if pages_ok == 0:
        _finalize_document(document_id, ok=False, error=first_error)
        status = "failed"
    else:
        _finalize_document(document_id, ok=True)
        status = "complete" if pages_failed == 0 else "partial"

    _last_status["current_document_id"] = None
    _last_status["current_page_number"] = None
    return {"document_id": document_id, "pages_ok": pages_ok,
            "pages_failed": pages_failed, "status": status}


# ─── Loop ───────────────────────────────────────────────────────────────

def _tick(tenant_id: str) -> None:
    doc = _claim_next_document(tenant_id)
    if doc is None:
        return
    logger.info("OCR worker claimed document_id=%s (%s)",
                doc["document_id"], doc["on_disk_path"])
    try:
        summary = _process_document(doc)
        _last_status["docs_processed"] += 1
        logger.info("OCR done: %s", json.dumps(summary))
    except Exception as e:
        logger.exception("OCR worker crashed on doc=%s", doc["document_id"])
        _last_status["last_error"] = str(e)
        _finalize_document(doc["document_id"], ok=False, error=str(e))


def _loop(tenant_id: str) -> None:
    _last_status["running"] = True
    logger.info("OCR worker started (poll=%ds)", POLL_INTERVAL)
    while not _stop_event.is_set():
        _last_status["last_tick"] = time.time()
        try:
            _tick(tenant_id)
        except Exception as e:
            logger.error("OCR tick error: %s", e)
            _last_status["last_error"] = str(e)
        _stop_event.wait(POLL_INTERVAL)
    _last_status["running"] = False
    logger.info("OCR worker stopped")


# ─── Public API (start/stop/status, matching the supervisor contract) ──

def start_worker(tenant_id: str = "default") -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(
        target=_loop, args=(tenant_id,), daemon=True, name="ocr-worker",
    )
    _thread.start()


def stop_worker() -> None:
    global _thread
    _stop_event.set()
    if _thread:
        _thread.join(timeout=10)
        _thread = None


def get_status() -> dict:
    from core.ocr.engines import get_engine_status
    snapshot = dict(_last_status)
    snapshot["running"] = _thread is not None and _thread.is_alive()
    snapshot["poll_interval"] = POLL_INTERVAL
    snapshot["engines"] = get_engine_status()
    return snapshot
