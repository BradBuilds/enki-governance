"""
Documents API — ingest PDFs, list documents, fetch page content + mentions.

GET    /documents                       — list documents (paginated, filterable by dataset)
GET    /documents/{document_id}         — single document detail
GET    /documents/{document_id}/pages   — page list with summaries
GET    /documents/{document_id}/page/{page_no}  — full page text + mentions on that page
GET    /documents/{document_id}/mentions       — all mentions in document (paginated)
POST   /documents/ingest-pdf            — kick off ingestion of a PDF on disk
POST   /documents/ingest-directory      — bulk ingest a directory of PDFs
POST   /documents/{document_id}/extract — run mention extraction on a doc
POST   /documents/boilerplate/{corpus_id}  — run boilerplate clustering for a corpus
GET    /documents/templates/{corpus_id} — list boilerplate / recurring template phrases
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from storage.postgres.db import get_connection

logger = logging.getLogger("enki.api.documents")

router = APIRouter(prefix="/documents", tags=["documents"])


# ─── List / detail ──────────────────────────────────────────────────────

@router.get("")
async def list_documents(
    dataset_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """List documents in the corpus, newest first. Filterable by dataset."""
    where = ["tenant_id = %s"]
    params: list = [auth.tenant_id]
    if dataset_id:
        where.append("dataset_id = %s")
        params.append(dataset_id)
    params.extend([limit, offset])

    sql = f"""
        SELECT document_id, dataset_id, source_type, title, docket_number,
               release_date, page_count, word_count, extraction_method,
               trust_class, created_at, last_refreshed_at
        FROM documents
        WHERE {' AND '.join(where)}
        ORDER BY created_at DESC
        LIMIT %s OFFSET %s
    """
    out = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for r in cur.fetchall():
                out.append({
                    "document_id": r[0], "dataset_id": r[1],
                    "source_type": r[2], "title": r[3],
                    "docket_number": r[4],
                    "release_date": r[5].isoformat() if r[5] else None,
                    "page_count": r[6], "word_count": r[7],
                    "extraction_method": r[8], "trust_class": r[9],
                    "created_at": r[10].isoformat() if r[10] else None,
                    "last_refreshed_at": r[11].isoformat() if r[11] else None,
                })
    return {"documents": out, "limit": limit, "offset": offset}


@router.get("/{document_id}")
async def get_document(
    document_id: int,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Full detail on one document."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT document_id, tenant_id, dataset_id, source_type, hash,
                       title, docket_number, bates_range, jurisdiction,
                       publisher, author, release_date, document_date,
                       page_count, word_count, ocr_quality, extraction_method,
                       source_url, on_disk_path, metadata, trust_class,
                       created_at, last_refreshed_at,
                       extraction_quality_class, triage_evidence,
                       triage_decided_at
                FROM documents WHERE document_id = %s AND tenant_id = %s
                """,
                (document_id, auth.tenant_id),
            )
            r = cur.fetchone()
            if not r:
                raise HTTPException(404, "Document not found")

            # Mention + statement counters
            cur.execute(
                "SELECT COUNT(*) FROM document_mentions WHERE document_id = %s",
                (document_id,),
            )
            mention_count = cur.fetchone()[0]
            cur.execute(
                """
                SELECT COUNT(*) FROM document_mentions
                WHERE document_id = %s AND resolution_status != 'bound'
                """,
                (document_id,),
            )
            unresolved_count = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM statements WHERE document_id = %s",
                (document_id,),
            )
            statement_count = cur.fetchone()[0]

    return {
        "document_id": r[0], "tenant_id": r[1], "dataset_id": r[2],
        "source_type": r[3], "hash": r[4], "title": r[5],
        "docket_number": r[6], "bates_range": r[7], "jurisdiction": r[8],
        "publisher": r[9], "author": r[10],
        "release_date": r[11].isoformat() if r[11] else None,
        "document_date": r[12].isoformat() if r[12] else None,
        "page_count": r[13], "word_count": r[14],
        "ocr_quality": r[15], "extraction_method": r[16],
        "source_url": r[17], "on_disk_path": r[18],
        "metadata": r[19], "trust_class": r[20],
        "created_at": r[21].isoformat() if r[21] else None,
        "last_refreshed_at": r[22].isoformat() if r[22] else None,
        "extraction_quality_class": r[23],
        "triage_evidence": r[24],
        "triage_decided_at": r[25].isoformat() if r[25] else None,
        "counters": {
            "mentions_total": mention_count,
            "mentions_unresolved": unresolved_count,
            "statements_total": statement_count,
        },
    }


# ─── Source bytes (the original file the extractor saw) ───────────────────

# Default-deny mime fallback used when we can't sniff a real one. The viewer
# treats octet-stream as "download only" rather than "render unsafely".
_OCTET = "application/octet-stream"

# Known-safe extension → mime overrides. mimetypes.guess_type misses some
# (.docx/.xlsx on older Pythons) and gets others wrong, so this is the
# authoritative list the viewer will dispatch on.
_EXT_MIME = {
    ".pdf":   "application/pdf",
    ".txt":   "text/plain",
    ".md":    "text/markdown",
    ".html":  "text/html",
    ".htm":   "text/html",
    ".json":  "application/json",
    ".csv":   "text/csv",
    ".doc":   "application/msword",
    ".docx":  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls":   "application/vnd.ms-excel",
    ".xlsx":  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".png":   "image/png",
    ".jpg":   "image/jpeg",
    ".jpeg":  "image/jpeg",
    ".gif":   "image/gif",
    ".webp":  "image/webp",
    ".svg":   "image/svg+xml",
}


def _resolve_mime(stored_mime: Optional[str], on_disk_path: Optional[str]) -> str:
    """Pick the mime to send. Trust the stored value first, fall back to
    the extension map, then to mimetypes.guess_type, then to octet-stream."""
    if stored_mime:
        return stored_mime
    if on_disk_path:
        ext = Path(on_disk_path).suffix.lower()
        if ext in _EXT_MIME:
            return _EXT_MIME[ext]
        guess, _ = mimetypes.guess_type(on_disk_path)
        if guess:
            return guess
    return _OCTET


def _filename_for_download(stored_name: Optional[str], on_disk_path: Optional[str],
                            document_id: int, mime: str) -> str:
    """What to put in Content-Disposition. Prefer the stored source filename;
    fall back to the on-disk basename; last resort, synthesize."""
    if stored_name:
        return stored_name
    if on_disk_path:
        name = Path(on_disk_path).name
        if name:
            return name
    # Synthesize using the mime's standard extension
    ext = mimetypes.guess_extension(mime) or ".bin"
    return f"document-{document_id}{ext}"


@router.get("/{document_id}/source")
async def get_document_source(
    document_id: int,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Serve the original source bytes the extractor ingested.

    Resolution order:
      1. enki_path — the signed, content-hashed `.enki` container. This is
         the canonical, durable copy on /data/enki_storage, survives across
         ingest restarts, and lets us return the verified payload bytes
         with a trustworthy X-ENKI-Content-Hash + X-ENKI-Signer header.
      2. on_disk_path — the raw file as ingested. Fast path when present,
         but commonly absent after a /tmp wipe.

    Either way the bytes are content-addressable so the browser can verify
    integrity against the X-ENKI-Content-Hash header.

    Auth-scoped to the caller's tenant — no cross-tenant document peek even
    when the ID is guessed.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT on_disk_path, enki_path, mime_type, source_filename,
                       content_sha256, enki_signed_at, enki_signer_pubkey, title
                  FROM documents
                 WHERE document_id = %s AND tenant_id = %s
                """,
                (document_id, auth.tenant_id),
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Document not found")

    on_disk_path, enki_path, stored_mime, stored_name, sha256, \
        signed_at, signer_pubkey, title = row

    # ── 1. Prefer the signed .enki container ────────────────────────────
    if enki_path and Path(enki_path).exists():
        try:
            from core.enki.format import load_enki
            # verify=False: we trust the disk copy (admitted via GDU at ingest
            # already). Re-verifying every request would re-do an Ed25519
            # check on every byte read, expensive for the doc viewer's
            # progressive load. The X-ENKI-Content-Hash we send back is
            # the stored hash, which the client can match against the
            # body it received.
            obj = load_enki(enki_path, verify=False)
        except Exception as e:
            logger.warning(
                "documents.source: failed to decode .enki for doc_id=%s path=%s: %s",
                document_id, enki_path, e,
            )
            obj = None
        if obj is not None and isinstance(obj.content, (bytes, bytearray)):
            mime = (
                stored_mime
                or obj.metadata.get("mime_type")
                or _resolve_mime(None, obj.metadata.get("source_url"))
            )
            filename = _filename_for_download(
                stored_name or obj.metadata.get("source_title"),
                on_disk_path,
                document_id,
                mime,
            )
            from fastapi.responses import Response
            headers = _integrity_headers(filename, sha256, signed_at, signer_pubkey)
            return Response(
                content=bytes(obj.content),
                media_type=mime,
                headers=headers,
            )

    # ── 2. Fall back to raw on_disk_path ────────────────────────────────
    if on_disk_path and Path(on_disk_path).exists():
        mime = _resolve_mime(stored_mime, on_disk_path)
        filename = _filename_for_download(stored_name, on_disk_path, document_id, mime)
        headers = _integrity_headers(filename, sha256, signed_at, signer_pubkey)
        return FileResponse(
            on_disk_path, media_type=mime, filename=filename, headers=headers,
        )

    logger.warning(
        "documents.source: no readable source for doc_id=%s "
        "(on_disk_path=%s, enki_path=%s)",
        document_id, on_disk_path, enki_path,
    )
    # HTTP 410 is heuristically CACHEABLE — without this a transient miss (e.g.
    # during evidence recovery) gets cached by the browser and keeps showing the
    # error even after the original is restored. no-store forces a fresh check.
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=410,
        content={"detail": "Source bytes not available yet for this document "
                           "(evidence recovery may be in progress)."},
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


def _integrity_headers(filename: str, sha256: Optional[str],
                       signed_at, signer_pubkey: Optional[str]) -> dict:
    """Build the integrity + caching headers attached to /source responses."""
    headers = {
        "Content-Disposition": f'inline; filename="{filename}"',
        "Cache-Control": "private, max-age=86400, immutable",
    }
    if sha256:
        headers["ETag"] = f'"sha256:{sha256}"'
        headers["X-ENKI-Content-Hash"] = f"sha256:{sha256}"
    if signed_at:
        headers["X-ENKI-Signed-At"] = signed_at.isoformat()
    if signer_pubkey:
        headers["X-ENKI-Signer"] = signer_pubkey
    return headers


# ─── Statement → span lookup (lets the dossier link to source) ─────────


@router.get("/statement/{statement_id}/source-span")
async def get_statement_source_span(
    statement_id: int,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Resolve a statement to its position in the source document. The
    dossier UI calls this so 'view source →' on any fact / event lands
    on the document viewer at the right page + span. Returns 404 when
    the statement is missing or has no document binding (e.g. signals
    extracted from RSS that never produced a document)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.statement_id, s.document_id, s.page_id, s.paragraph_id,
                       s.char_start, s.char_end, s.raw_text,
                       d.title, d.mime_type, d.source_url,
                       p.order_index AS page_order
                  FROM statements s
                  LEFT JOIN documents d           ON d.document_id = s.document_id
                  LEFT JOIN document_paragraphs p ON p.paragraph_id = s.paragraph_id
                 WHERE s.statement_id = %s
                   AND (s.tenant_id = %s OR s.tenant_id IS NULL)
                """,
                (statement_id, auth.tenant_id),
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(404, "statement not found")
    (sid, doc_id, page_id, para_id,
     char_start, char_end, raw_text,
     doc_title, mime_type, source_url, page_order) = row
    if not doc_id:
        raise HTTPException(404, "statement has no source document binding")
    return {
        "statement_id":  sid,
        "document_id":   doc_id,
        "document_title": doc_title,
        "mime_type":     mime_type,
        "source_url":    source_url,
        "page_id":       page_id,
        "page_order":    page_order,
        "paragraph_id":  para_id,
        "char_start":    char_start,
        "char_end":      char_end,
        "raw_text":      raw_text,
        # Convenience: the path the UI should navigate to.
        "viewer_path":   f"/document/{doc_id}?statement={sid}",
    }


@router.get("/{document_id}/pages")
async def list_pages(
    document_id: int,
    limit: int = 100,
    offset: int = 0,
    include_text: bool = True,
    text_chars: int = 2000,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Page list with text excerpts + mention count per page.

    When include_text=true, returns the first `text_chars` characters of
    each page's raw_text so the document view can show actual content
    rather than just metadata. Pass include_text=false for legacy
    metadata-only responses.
    """
    text_select = ", LEFT(COALESCE(p.raw_text, ''), %s) AS text" if include_text else ""
    params: list = [document_id, auth.tenant_id, limit, offset]
    if include_text:
        params = [text_chars, document_id, auth.tenant_id, limit, offset]

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT p.page_id, p.page_number,
                       LENGTH(COALESCE(p.raw_text, '')) as text_len,
                       p.ocr_confidence, p.extraction_method, p.has_redactions,
                       (SELECT COUNT(*) FROM document_mentions m
                        WHERE m.page_id = p.page_id) as mention_count
                       {text_select}
                FROM document_pages p
                JOIN documents d ON d.document_id = p.document_id
                WHERE p.document_id = %s AND d.tenant_id = %s
                ORDER BY p.page_number
                LIMIT %s OFFSET %s
                """,
                params,
            )
            rows = cur.fetchall()
            pages = []
            for r in rows:
                page = {
                    "page_id": r[0], "page_number": r[1],
                    "text_length": r[2], "ocr_confidence": r[3],
                    "extraction_method": r[4], "has_redactions": r[5],
                    "mention_count": r[6],
                }
                if include_text:
                    page["text"] = r[7] or ""
                    page["text_truncated"] = (r[2] or 0) > text_chars
                pages.append(page)

            # If pages have no text at all (PDF-extracted via paragraphs only),
            # synthesise text by concatenating paragraphs. Common for case-file
            # docs ingested via the bulk re-acquisition path.
            if include_text:
                empty_page_ids = [p["page_id"] for p in pages
                                  if not p["text"] and p["mention_count"]]
                if empty_page_ids:
                    cur.execute(
                        """SELECT page_id,
                                  string_agg(raw_text, E'\n\n' ORDER BY order_index) AS text
                             FROM document_paragraphs
                            WHERE page_id = ANY(%s)
                              AND raw_text IS NOT NULL
                            GROUP BY page_id""",
                        (empty_page_ids,),
                    )
                    para_text = {row[0]: (row[1] or "") for row in cur.fetchall()}
                    for p in pages:
                        if not p["text"] and p["page_id"] in para_text:
                            t = para_text[p["page_id"]]
                            p["text"] = t[:text_chars]
                            p["text_truncated"] = len(t) > text_chars
                            p["text_source"] = "paragraphs"

    return {"document_id": document_id, "pages": pages}


class MentionReview(BaseModel):
    action: str  # 'accept' | 'reject' | 'correct'
    corrected_form: Optional[str] = None
    corrected_type: Optional[str] = None
    note: Optional[str] = None


@router.post("/{document_id}/mentions/{mention_id}/review")
async def review_mention(
    document_id: int,
    mention_id: int,
    body: MentionReview,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Accept, reject, or correct an extracted mention.

    - accept: review_status='accepted' (mention stays as-is, blessed)
    - reject: review_status='rejected' (mention hidden from downstream views;
      collapse_worker skips on next pass)
    - correct: review_status='corrected' + new surface_form / suggested_type
    """
    if body.action not in {"accept", "reject", "correct"}:
        raise HTTPException(400, "action must be accept|reject|correct")

    status_map = {"accept": "accepted", "reject": "rejected", "correct": "corrected"}
    new_status = status_map[body.action]

    updates = ["review_status = %s", "reviewed_by = %s", "reviewed_at = NOW()"]
    params: list = [new_status, auth.tenant_id or "operator"]
    if body.action == "correct":
        if body.corrected_form:
            updates.append("surface_form = %s")
            params.append(body.corrected_form)
        if body.corrected_type:
            updates.append("suggested_type = %s")
            params.append(body.corrected_type)
    params.extend([document_id, mention_id])

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""UPDATE document_mentions
                       SET {', '.join(updates)}
                     WHERE document_id = %s AND mention_id = %s
                 RETURNING mention_id, surface_form, review_status""",
                params,
            )
            row = cur.fetchone()
        conn.commit()
    if not row:
        raise HTTPException(404, "mention not found")
    return {"mention_id": row[0], "surface_form": row[1], "review_status": row[2]}


@router.get("/{document_id}/page/{page_no}")
async def get_page(
    document_id: int,
    page_no: int,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Single page with full text + all mentions on that page."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.page_id, p.raw_text, p.raw_text_llm,
                       p.ocr_confidence, p.extraction_method, p.has_redactions,
                       p.on_disk_image
                FROM document_pages p
                JOIN documents d ON d.document_id = p.document_id
                WHERE p.document_id = %s AND p.page_number = %s
                  AND d.tenant_id = %s
                """,
                (document_id, page_no, auth.tenant_id),
            )
            r = cur.fetchone()
            if not r:
                raise HTTPException(404, "Page not found")
            page_id = r[0]
            cur.execute(
                """
                SELECT mention_id, surface_form, char_start, char_end,
                       suggested_type, confidence, resolution_status,
                       candidate_entity_id
                FROM document_mentions
                WHERE page_id = %s
                ORDER BY char_start
                """,
                (page_id,),
            )
            mentions = [
                {
                    "mention_id": m[0], "surface_form": m[1],
                    "char_start": m[2], "char_end": m[3],
                    "suggested_type": m[4], "confidence": m[5],
                    "resolution_status": m[6], "candidate_entity_id": m[7],
                }
                for m in cur.fetchall()
            ]
    return {
        "document_id": document_id, "page_id": page_id, "page_number": page_no,
        "raw_text": r[1], "raw_text_llm": r[2],
        "ocr_confidence": r[3], "extraction_method": r[4],
        "has_redactions": r[5],
        "has_image": bool(r[6]),
        "image_url": f"/api/documents/{document_id}/page/{page_no}/image" if r[6] else None,
        "mentions": mentions,
    }


@router.get("/{document_id}/page/{page_no}/image")
async def get_page_image(document_id: int, page_no: int):
    """Serve the rendered source-page image (JPEG). This is what lets any
    extraction be seen IN CONTEXT on its original page — the deposition caption,
    the redaction bars, the exact line a statement came from. No auth wrapper so
    an <img> tag can load it directly (behind the same proxy as the app)."""
    from fastapi.responses import FileResponse
    import os
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT on_disk_image FROM document_pages "
                "WHERE document_id = %s AND page_number = %s",
                (document_id, page_no),
            )
            r = cur.fetchone()
    if not r or not r[0] or not os.path.exists(r[0]):
        raise HTTPException(404, "Page image not available")
    return FileResponse(r[0], media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


# ─── Ingest ────────────────────────────────────────────────────────────

class IngestPdfRequest(BaseModel):
    path: str
    dataset_id: str
    title: Optional[str] = None
    docket_number: Optional[str] = None
    bates_range: Optional[str] = None
    jurisdiction: Optional[str] = None
    publisher: Optional[str] = None
    author: Optional[str] = None
    release_date: Optional[str] = None
    source_url: Optional[str] = None
    trust_class: str = "mentioned"


def _do_ingest_pdf(req: dict, tenant_id: str):
    try:
        from core.documents.pdf_ingester import ingest_pdf
        result = ingest_pdf(tenant_id=tenant_id, **req)
        logger.info("pdf ingest done: %s", result)
    except Exception:
        logger.exception("pdf ingest crashed")


@router.post("/ingest-pdf")
async def ingest_pdf_endpoint(
    body: IngestPdfRequest,
    background_tasks: BackgroundTasks,
    background: bool = True,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Ingest a PDF that's already on disk on the server. For uploaded
    files, see the 'upload' endpoint (future). Pass background=false to
    wait for completion (small docs / testing)."""
    if not os.path.exists(body.path):
        raise HTTPException(400, f"File not found: {body.path}")

    payload = body.model_dump()
    if background:
        background_tasks.add_task(_do_ingest_pdf, payload, auth.tenant_id)
        return {"queued": True, "path": body.path, "background": True}

    from core.documents.pdf_ingester import ingest_pdf
    result = await asyncio.to_thread(ingest_pdf, tenant_id=auth.tenant_id, **payload)
    if not result.get("ok"):
        return JSONResponse(status_code=500, content=result)
    return result


class IngestDirectoryRequest(BaseModel):
    directory: str
    dataset_id: str
    trust_class: str = "mentioned"
    limit: Optional[int] = None


def _do_ingest_directory(req: dict, tenant_id: str):
    try:
        from core.documents.pdf_ingester import ingest_pdf_directory
        result = ingest_pdf_directory(tenant_id=tenant_id, **req)
        logger.info("directory ingest done: totals=%s", result.get("totals"))
    except Exception:
        logger.exception("directory ingest crashed")


@router.post("/ingest-directory")
async def ingest_directory(
    body: IngestDirectoryRequest,
    background_tasks: BackgroundTasks,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Bulk-ingest every PDF in a directory tree. Always background — these
    runs can be long. Result lands in logs + the document list."""
    if not os.path.exists(body.directory):
        raise HTTPException(400, f"Directory not found: {body.directory}")
    background_tasks.add_task(_do_ingest_directory, body.model_dump(), auth.tenant_id)
    return {"queued": True, "directory": body.directory}


# ─── Extract / boilerplate ──────────────────────────────────────────────

@router.post("/{document_id}/extract")
async def extract_document(
    document_id: int,
    background_tasks: BackgroundTasks,
    background: bool = True,
    use_llm: bool = True,
    paragraph_limit: Optional[int] = None,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Run mention extraction over every non-boilerplate paragraph in the
    document. Uses the configured LLM provider if available, falls back to
    rule-based regex otherwise.

    Query params:
      use_llm=false        Force regex-only extraction (fast, lower quality).
                           Useful for baseline runs while the LLM is busy on
                           higher-priority work (dual-extraction judge, etc.).
      paragraph_limit=N    Cap paragraphs processed this call (test runs).
    """
    def _run():
        try:
            from core.documents.extractor import extract_from_document
            llm = None
            if use_llm:
                try:
                    from core.ai.config import get_tenant_provider
                    llm = get_tenant_provider(auth.tenant_id)
                except Exception:
                    logger.exception("get_tenant_provider failed")
            return extract_from_document(
                tenant_id=auth.tenant_id,
                document_id=document_id,
                llm=llm,
                rule_fallback=True,
                paragraph_limit=paragraph_limit,
            )
        except Exception:
            logger.exception("extract crashed")
            return {"ok": False}

    if background:
        background_tasks.add_task(_run)
        return {"queued": True, "document_id": document_id, "use_llm": use_llm}
    result = await asyncio.to_thread(_run)
    return result


@router.post("/{document_id}/compare-extraction")
async def compare_extraction(
    document_id: int,
    background_tasks: BackgroundTasks,
    background: bool = False,
    force: bool = False,
    page_limit: Optional[int] = None,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Run dual-extraction comparator over a document: native pdfplumber +
    rendered-page PaddleOCR, then LLM-as-judge picks/merges. Writes verdict
    rows to document_page_readings and updates document_pages.raw_text with
    the chosen text. Synchronous by default so the UI can see the verdict."""
    def _run():
        try:
            from core.documents.comparator import compare_document
            llm = None
            try:
                from core.ai.config import get_tenant_provider
                llm = get_tenant_provider(auth.tenant_id)
            except Exception:
                logger.exception("get_tenant_provider failed")
            return compare_document(
                document_id=document_id, llm=llm, force=force,
                page_limit=page_limit,
            )
        except Exception:
            logger.exception("compare-extraction crashed")
            return {"ok": False}

    if background:
        background_tasks.add_task(_run)
        return {"queued": True, "document_id": document_id}
    result = await asyncio.to_thread(_run)
    return result


@router.post("/corroboration/{corpus_id}")
async def score_corroboration(
    corpus_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Compute cross-document corroboration scores for every surface form
    that appears in this corpus' document_mentions. Writes the scorecard
    to corpus_corroboration. Idempotent. Re-run after new mention extraction."""
    def _run():
        try:
            from core.documents.corroboration import score_corpus
            return score_corpus(corpus_id)
        except Exception:
            logger.exception("corroboration scoring crashed")
            return {"ok": False}
    return await asyncio.to_thread(_run)


@router.post("/cluster-images/{corpus_id}")
async def cluster_corpus_images_route(
    corpus_id: str,
    hamming_threshold: int = 8,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Run pHash-based image clustering across a corpus. Near-duplicate
    images (same photo, different scans, decorative letterheads) collapse
    into one cluster row + N cluster_members."""
    def _run():
        try:
            from core.documents.image_clustering import cluster_corpus
            return cluster_corpus(corpus_id, hamming_threshold=hamming_threshold)
        except Exception:
            logger.exception("image clustering crashed")
            return {"ok": False}
    return await asyncio.to_thread(_run)


@router.get("/cluster-images/{corpus_id}")
async def get_image_clusters_route(
    corpus_id: str,
    only_templates: bool = False,
    only_intel: bool = False,
    limit: int = 50,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """List image clusters: only_templates=true returns decorative/
    letterhead clusters; only_intel=true returns intel-grade dup clusters."""
    def _run():
        from core.documents.image_clustering import get_clusters
        return get_clusters(corpus_id, only_templates=only_templates,
                            only_intel=only_intel, limit=limit)
    return await asyncio.to_thread(_run)


@router.post("/extract-image-mentions/{image_id}")
async def extract_image_mentions_route(
    image_id: int,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Run mention extraction on one image's OCR text + caption, producing
    document_mentions rows with extraction_method='image_ocr'/'image_caption'
    and image_id set. These flow through corroboration + clustering + promotion
    the same as paragraph mentions."""
    def _run():
        try:
            from core.documents.image_mention_extractor import extract_from_image
            from core.ai.config import get_tenant_provider
            llm = None
            try:
                llm = get_tenant_provider(auth.tenant_id)
            except Exception:
                pass
            return extract_from_image(image_id, llm=llm)
        except Exception:
            logger.exception("extract_from_image crashed")
            return {"ok": False}
    return await asyncio.to_thread(_run)


@router.post("/extract-image-mentions-corpus/{corpus_id}")
async def extract_corpus_image_mentions_route(
    corpus_id: str,
    limit: Optional[int] = None,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Walk every analyzed image in the corpus and pull mentions from its
    OCR + caption."""
    def _run():
        try:
            from core.documents.image_mention_extractor import extract_from_corpus
            from core.ai.config import get_tenant_provider
            llm = None
            try:
                llm = get_tenant_provider(auth.tenant_id)
            except Exception:
                pass
            return extract_from_corpus(corpus_id, llm=llm, limit=limit)
        except Exception:
            logger.exception("extract_from_corpus image mentions crashed")
            return {"ok": False}
    return await asyncio.to_thread(_run)


@router.post("/analyze-image/{image_id}")
async def analyze_image_route(
    image_id: int,
    force: bool = False,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Run vision LLM + OCR + face detection on one extracted image,
    writing per-engine rows to document_image_analyses."""
    def _run():
        try:
            from core.documents.image_analysis import analyze_image
            return analyze_image(image_id, force=force)
        except Exception:
            logger.exception("analyze_image crashed")
            return {"ok": False}
    return await asyncio.to_thread(_run)


@router.post("/analyze-images-corpus/{corpus_id}")
async def analyze_corpus_images_route(
    corpus_id: str,
    limit: Optional[int] = None,
    force: bool = False,
    skip_redacted: bool = True,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Walk every image in the corpus and run vision + OCR + faces. Long
    running — vision LLM is ~10-30s/image on the a GPU."""
    def _run():
        try:
            from core.documents.image_analysis import analyze_corpus
            return analyze_corpus(corpus_id, limit=limit, force=force,
                                   skip_redacted=skip_redacted)
        except Exception:
            logger.exception("analyze_corpus crashed")
            return {"ok": False}
    return await asyncio.to_thread(_run)


@router.get("/image/{image_id}/analyses")
async def get_image_analyses(
    image_id: int,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Return all engine results for one image."""
    def _run():
        from storage.postgres.db import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT a.engine, a.classification, a.caption,
                           a.is_intelligence, a.confidence,
                           LEFT(a.ocr_text, 500) AS ocr_excerpt,
                           a.objects, a.tags,
                           a.faces_count, a.faces,
                           a.error, a.created_at
                      FROM document_image_analyses a
                     WHERE a.image_id = %s
                     ORDER BY a.engine
                    """,
                    (image_id,),
                )
                return [
                    {
                        "engine": r[0], "classification": r[1], "caption": r[2],
                        "is_intelligence": r[3],
                        "confidence": float(r[4] or 0),
                        "ocr_excerpt": r[5], "objects": r[6], "tags": r[7],
                        "faces_count": r[8], "faces": r[9],
                        "error": r[10],
                        "created_at": r[11].isoformat() if r[11] else None,
                    }
                    for r in cur.fetchall()
                ]
    return await asyncio.to_thread(_run)


@router.post("/extract-images/{document_id}")
async def extract_images_route(
    document_id: int,
    force: bool = False,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Extract every embedded raster image from one PDF and persist to
    document_images with full provenance (page, image_index, bbox, sha256,
    phash). Idempotent unless force=true."""
    def _run():
        try:
            from core.documents.image_extractor import extract_images_from_pdf
            return extract_images_from_pdf(document_id, force=force)
        except Exception:
            logger.exception("extract_images_from_pdf crashed")
            return {"ok": False}
    return await asyncio.to_thread(_run)


@router.post("/extract-images-corpus/{corpus_id}")
async def extract_corpus_images_route(
    corpus_id: str,
    force: bool = False,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Run image extraction across every PDF in the corpus."""
    def _run():
        try:
            from core.documents.image_extractor import extract_corpus
            return extract_corpus(corpus_id, force=force)
        except Exception:
            logger.exception("extract_corpus images crashed")
            return {"ok": False}
    return await asyncio.to_thread(_run)


@router.get("/documents/{document_id}/images")
async def list_document_images(
    document_id: int,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """List every extracted image for a document with its provenance."""
    def _run():
        from storage.postgres.db import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT i.image_id, p.page_number, i.image_index,
                           i.on_disk_path, i.sha256, i.phash,
                           i.width, i.height, i.bytes, i.mime,
                           i.is_redacted_block, i.extracted_at
                      FROM document_images i
                      JOIN document_pages p ON p.page_id = i.page_id
                     WHERE i.document_id = %s
                     ORDER BY p.page_number, i.image_index
                    """,
                    (document_id,),
                )
                return [
                    {
                        "image_id": r[0], "page": r[1], "index": r[2],
                        "path": r[3], "sha256": r[4], "phash": r[5],
                        "width": r[6], "height": r[7], "bytes": r[8],
                        "mime": r[9], "is_redacted_block": r[10],
                        "extracted_at": r[11].isoformat() if r[11] else None,
                    }
                    for r in cur.fetchall()
                ]
    return await asyncio.to_thread(_run)


@router.post("/promote-clusters/{corpus_id}")
async def promote_corpus_clusters_route(
    corpus_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Walk every cluster in this corpus and promote validated ones to
    canonical entities. Backlinks every mention to its entity, records
    the promotion rule + evidence to document_cluster_promotions.

    Priority order:
      1. cluster with Wikipedia QID match → find-or-create canonical entity
      2. cluster with internal-node match → link mentions to existing entity
      3. multi-mention cluster, no external match → create candidate entity
      4. singleton, no external match → don't promote (wait for corroboration)
    """
    def _run():
        try:
            from core.documents.entity_promotion import promote_corpus
            return promote_corpus(corpus_id, tenant_id=auth.tenant_id)
        except Exception:
            logger.exception("promote_corpus crashed")
            return {"ok": False}
    return await asyncio.to_thread(_run)


@router.get("/entities/{entity_id}/dossier")
async def get_entity_dossier_route(
    entity_id: int,
    include_surrounding_text: bool = True,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Return the full provenance bundle for one canonical entity:
    canonical row + every mention (with doc/page/paragraph/char span +
    surrounding text) + clusters + external validations + promotion
    record + collapse decisions. This is the 'node file' content."""
    def _run():
        from core.documents.entity_dossier import get_entity_dossier
        d = get_entity_dossier(entity_id, include_surrounding_text=include_surrounding_text)
        return d if d else {"error": "entity_not_found"}
    return await asyncio.to_thread(_run)


@router.get("/entities/by-qid/{qid}/dossier")
async def get_dossier_by_qid_route(
    qid: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Dossier lookup by Wikidata QID (e.g., Q2222222 for Alex Acme)."""
    def _run():
        from core.documents.entity_dossier import get_dossier_by_qid
        d = get_dossier_by_qid(qid, tenant_id=auth.tenant_id)
        return d if d else {"error": "no_entity_with_qid", "qid": qid}
    return await asyncio.to_thread(_run)


@router.post("/cluster-validation/{corpus_id}")
async def validate_clusters_route(
    corpus_id: str,
    min_mentions: int = 1,
    limit: int = 200,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Cluster-aware source validation: route each document_mention_cluster
    through Wikipedia with context-score disambiguation. Each cluster
    resolves independently, so 'John #1' (one context) and
    'John #2' (different context) can pick different Wikidata QIDs.

    Runs strict_unknown=True automatically for review_required corpora —
    rejects matches with zero context overlap when type is unknown."""
    def _run():
        try:
            from core.documents.cluster_validation import validate_clusters
            return validate_clusters(
                corpus_id, min_mentions=min_mentions, limit=limit,
            )
        except Exception:
            logger.exception("cluster_validation crashed")
            return {"ok": False}
    return await asyncio.to_thread(_run)


@router.post("/cluster-mentions/{corpus_id}")
async def cluster_mentions(
    corpus_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Re-cluster every mention in this corpus by paragraph-context overlap.

    Mentions with the same surface_form_norm cluster together iff their
    paragraphs share at least one other surface form. This is the
    multi-signal disambiguation: 'Doe' in Acme context is a
    different cluster than 'Doe' in Roswell context. Each cluster
    becomes a candidate entity for the resolver.

    Idempotent. Re-run after new mention extraction."""
    def _run():
        try:
            from core.documents.context_resolver import cluster_corpus
            return cluster_corpus(corpus_id)
        except Exception:
            logger.exception("cluster_corpus crashed")
            return {"ok": False}
    return await asyncio.to_thread(_run)


@router.get("/cluster-mentions/{corpus_id}")
async def get_mention_clusters(
    corpus_id: str,
    surface_form_norm: Optional[str] = None,
    limit: int = 50,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Inspection: list the mention clusters for a corpus, optionally
    filtered to one surface form."""
    def _run():
        from core.documents.context_resolver import get_clusters
        return get_clusters(corpus_id, surface_form_norm, limit)
    return await asyncio.to_thread(_run)


@router.post("/source-validation/{corpus_id}")
async def validate_sources(
    corpus_id: str,
    limit: int = 50,
    min_documents: int = 2,
    skip_existing: bool = True,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Look up the top-N corroborated entities in Wikipedia/Wikidata.
    Idempotent — re-running with skip_existing=true picks up only new forms."""
    def _run():
        try:
            from core.documents.source_validation import validate_corpus
            return validate_corpus(
                corpus_id, limit=limit, min_documents=min_documents,
                skip_existing=skip_existing,
            )
        except Exception:
            logger.exception("source validation crashed")
            return {"ok": False}
    return await asyncio.to_thread(_run)


@router.get("/source-validation/{corpus_id}/validated")
async def get_validated_entities(
    corpus_id: str,
    limit: int = 50,
    matched_only: bool = True,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Return top corroborated entities ENRICHED with their Wikipedia / Wikidata
    lookups. The 'validated' result for the corpus."""
    def _run():
        from core.documents.source_validation import get_validated
        return get_validated(corpus_id, limit=limit, matched_only=matched_only)
    return await asyncio.to_thread(_run)


@router.get("/corroboration/{corpus_id}/top")
async def get_top_corroborated(
    corpus_id: str,
    limit: int = 50,
    include_fragments: bool = False,
    min_documents: int = 1,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Return the highest-corroboration surface forms for this corpus.

    Query params:
      include_fragments=true  Include regex-fallback false positives.
      min_documents=N         Filter to forms in at least N distinct docs.
    """
    def _run():
        from core.documents.corroboration import top_corroborated
        return top_corroborated(
            corpus_id, limit=limit,
            include_fragments=include_fragments,
            min_documents=min_documents,
        )
    return await asyncio.to_thread(_run)


@router.post("/boilerplate/{corpus_id}")
async def classify_corpus_boilerplate(
    corpus_id: str,
    background_tasks: BackgroundTasks,
    background: bool = True,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Run the boilerplate-detection clustering job for a corpus. After this
    runs, boilerplate paragraphs are flagged + future mention extraction
    skips them."""
    def _run():
        try:
            from core.documents.boilerplate import classify_corpus
            return classify_corpus(corpus_id)
        except Exception:
            logger.exception("classify_corpus crashed")
            return {"ok": False}

    if background:
        background_tasks.add_task(_run)
        return {"queued": True, "corpus_id": corpus_id}
    return await asyncio.to_thread(_run)


@router.get("/templates/{corpus_id}")
async def list_templates_endpoint(
    corpus_id: str,
    classification: Optional[str] = None,
    limit: int = 100,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """List boilerplate / recurring / signal template phrases for a corpus."""
    from core.documents.boilerplate import list_templates
    items = list_templates(corpus_id, classification=classification, limit=limit)
    return {"corpus_id": corpus_id, "templates": items}


# ─── Shadow re-extraction (dry-run for domain-aware extractors) ─────────


@router.post("/{document_id}/re-extract")
async def re_extract_document(
    document_id: int,
    mode: str = "shadow",
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Run the domain-aware extractor for this doc's classified class
    and return what it WOULD emit, optionally diff'd against the live
    mentions/statements already in the catalog.

    mode=shadow  (default) — never writes. Returns a diff payload so the
                              operator can eyeball quality before committing.
    mode=commit  — coming next; reserved for the actual catalog rebuild.

    Only documents with a known document_class get domain-specific
    treatment; everything else returns 'no-op: would fall back to generic
    extractor' so the caller sees the explicit gap.
    """
    if mode not in {"shadow", "commit"}:
        raise HTTPException(400, "mode must be 'shadow' or 'commit'")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT document_id, title, document_class,
                       classification_confidence, mime_type
                  FROM documents
                 WHERE document_id = %s AND tenant_id = %s
                """,
                (document_id, auth.tenant_id),
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(404, "document not found")
    document_id, title, document_class, conf, mime_type = row

    if not document_class or document_class == "unknown":
        return {
            "mode":           mode,
            "document_id":    document_id,
            "title":          title,
            "document_class": document_class,
            "classification_confidence": conf,
            "extractor_dispatched": None,
            "would_fall_back_to_generic": True,
            "diff":           None,
            "note":           "no domain extractor for this class; live data unchanged",
        }

    # Pull the page text. Same join convention the classifier used so
    # char offsets line up with the live mention table.
    PAGE_TEXT_CAP = 4000
    DOC_TEXT_CAP  = 60000
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT page_number, raw_text FROM document_pages
                    WHERE document_id = %s AND raw_text IS NOT NULL
                    ORDER BY page_number""",
                (document_id,),
            )
            pages = cur.fetchall()
    text = "\n\n".join((p or "")[:PAGE_TEXT_CAP] for _, p in pages)[:DOC_TEXT_CAP]

    from core.documents.extractors import extract
    result = extract(document_id=document_id, text=text, document_class=document_class)
    if result is None:
        return {
            "mode":           mode,
            "document_id":    document_id,
            "title":          title,
            "document_class": document_class,
            "extractor_dispatched": None,
            "would_fall_back_to_generic": True,
            "diff":           None,
            "note":           f"no extractor registered for class {document_class!r}",
        }

    # Pull the live counts so we can show a side-by-side
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM document_mentions WHERE document_id = %s",
                (document_id,),
            )
            live_mentions = cur.fetchone()[0]
            cur.execute(
                """SELECT validation_status, COUNT(*) FROM statements
                    WHERE document_id = %s GROUP BY validation_status""",
                (document_id,),
            )
            live_stmt_by_status = {r[0]: r[1] for r in cur.fetchall()}
            cur.execute(
                """SELECT surface_form, suggested_type, COUNT(*) AS n
                     FROM document_mentions
                    WHERE document_id = %s
                    GROUP BY surface_form, suggested_type
                    ORDER BY n DESC LIMIT 20""",
                (document_id,),
            )
            live_top_mentions = [
                {"surface_form": r[0], "type": r[1], "count": r[2]}
                for r in cur.fetchall()
            ]

    # Format the proposed output for human consumption
    proposed_mentions_preview = [
        {
            "surface_form":   m.surface_form,
            "type":           m.suggested_type,
            "confidence":     m.confidence,
            "char_start":     m.char_start,
            "char_end":       m.char_end,
            "metadata":       m.metadata,
        }
        for m in result.mentions
    ]
    proposed_statements_preview = [
        {
            "predicate":      s.predicate,
            "speaker":        s.speaker_surface,
            "object":         s.object_surface,
            "event_time":     s.event_time_iso,
            "confidence":     s.confidence,
            "metadata":       s.metadata,
        }
        for s in result.statements
    ]

    # ── Commit path — wipe live mentions/statements for this doc, insert new
    committed = None
    if mode == "commit":
        committed = _commit_extraction(
            document_id=document_id,
            tenant_id=auth.tenant_id,
            result=result,
        )

    return {
        "mode":                       mode,
        "document_id":                document_id,
        "title":                      title,
        "document_class":             document_class,
        "classification_confidence":  conf,
        "extractor_dispatched":       result.extractor,
        "live_state": {
            "mentions_total":            live_mentions,
            "statements_by_status":      live_stmt_by_status,
            "top_mentions_sample":       live_top_mentions,
        },
        "proposed_output": {
            "summary":         result.summary(),
            "mentions":        proposed_mentions_preview,
            "statements":      proposed_statements_preview,
            "redaction_spans": [
                {"char_start": r.char_start, "char_end": r.char_end,
                 "exemption":  r.exemption}
                for r in result.redactions
            ],
            "parser_notes":    result.parser_notes,
        },
        "diff_summary": {
            "live_mentions":           live_mentions,
            "proposed_mentions":       len(result.mentions),
            "delta_mentions":          len(result.mentions) - live_mentions,
            "live_promoted_statements": live_stmt_by_status.get("promoted", 0),
            "proposed_statements":     len(result.statements),
        },
        "committed":  committed,
        "note": (
            "shadow mode — no live tables modified. Review the diff_summary "
            "and proposed_output. If the new emissions look right, repeat "
            "with ?mode=commit to replace the live mentions + statements."
            if mode == "shadow"
            else (
                f"commit mode — live data replaced. "
                f"Deleted {committed['deleted_mentions']} mentions + "
                f"{committed['deleted_statements']} statements; "
                f"inserted {committed['inserted_mentions']} mentions + "
                f"{committed['inserted_statements']} statements. "
                "Entity resolver will rebind on next worker pass."
            )
        ),
    }


def _commit_extraction(*, document_id: int, tenant_id: str, result) -> dict:
    """Replace the live document_mentions + statements for one document
    with the proposed extraction. Run in a single transaction; nothing
    half-applied on failure.

    Leaves all the immutable substrate alone — documents row, .enki
    container, page text. Only derived extraction tables change. Entity
    resolution is downstream: the new mentions land with entity_id NULL
    and the resolver worker binds them on its next sweep.
    """
    deleted_mentions = deleted_statements = 0
    inserted_mentions = inserted_statements = 0
    redaction_spans = len(result.redactions)
    with get_connection() as conn:
        with conn.cursor() as cur:
            # 1. Wipe live extraction for this doc
            cur.execute(
                "DELETE FROM statements WHERE document_id = %s AND tenant_id = %s",
                (document_id, tenant_id),
            )
            deleted_statements = cur.rowcount
            cur.execute(
                "DELETE FROM document_mentions WHERE document_id = %s AND tenant_id = %s",
                (document_id, tenant_id),
            )
            deleted_mentions = cur.rowcount

            # 2. Insert proposed mentions. We index by (char_start, char_end)
            #    so the statements pass can reference the freshly-minted
            #    mention_ids for speaker_mention_id linkage.
            mention_id_by_offset: dict[tuple[int, int, str], int] = {}
            for m in result.mentions:
                cur.execute(
                    """
                    INSERT INTO document_mentions
                        (tenant_id, document_id, char_start, char_end,
                         surface_form, surface_form_norm, suggested_type,
                         confidence, resolution_status, extraction_method,
                         created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                            'unresolved', %s, NOW())
                    RETURNING mention_id
                    """,
                    (
                        tenant_id, document_id, m.char_start, m.char_end,
                        m.surface_form, m.surface_form_norm, m.suggested_type,
                        m.confidence, m.extraction_method,
                    ),
                )
                mid = cur.fetchone()[0]
                mention_id_by_offset[(m.char_start, m.char_end, m.surface_form)] = mid
                inserted_mentions += 1

            # 3. Insert proposed statements. PNR-source statements arrive
            #    with validation_status='promoted' because the parser is
            #    deterministic — there's no LLM hallucination to gate
            #    against. Generic-LLM-sourced future statements would
            #    still go through the existing validator.
            for s in result.statements:
                cur.execute(
                    """
                    INSERT INTO statements
                        (tenant_id, document_id, predicate, raw_text,
                         char_start, char_end, surrounding_text,
                         confidence, event_time, validation_status,
                         extraction_method, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                            %s, 'promoted', %s, NOW())
                    """,
                    (
                        tenant_id, document_id, s.predicate, s.raw_text,
                        s.char_start, s.char_end, s.raw_text[:500],
                        s.confidence, s.event_time_iso, s.extraction_method,
                    ),
                )
                inserted_statements += 1

            # 4. Touch the doc so downstream consumers know it changed.
            cur.execute(
                """UPDATE documents
                      SET extraction_state = 're_extracted',
                          last_refreshed_at = NOW()
                    WHERE document_id = %s""",
                (document_id,),
            )
        conn.commit()

    return {
        "deleted_mentions":    deleted_mentions,
        "deleted_statements":  deleted_statements,
        "inserted_mentions":   inserted_mentions,
        "inserted_statements": inserted_statements,
        "redaction_spans":     redaction_spans,
    }
