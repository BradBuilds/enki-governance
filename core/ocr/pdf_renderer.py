"""
Per-page PDF rendering for the OCR worker.

Wraps pdf2image so the worker can grab one page at a time as PNG bytes
rather than loading the whole document into memory. Large court releases
(one 2024 release ≈ 5GB across thousands of pages) would otherwise OOM the
process; one page at a time keeps memory flat.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger("enki.ocr.pdf_renderer")

# 200 DPI is the sweet spot for PaddleOCR on legal scans — high enough for
# typewriter and 8pt footnotes, low enough that a 442-page exhibit fits in
# RAM one page at a time. Override via env if needed.
import os
RENDER_DPI = int(os.getenv("ENKI_OCR_RENDER_DPI", "200"))


def render_page(pdf_path: str, page_number: int) -> Optional[bytes]:
    """Render one 1-indexed PDF page to PNG bytes. Returns None on failure.

    Uses pdf2image (which shells out to poppler). The page is converted in
    isolation — the rest of the PDF is not touched — so the cost is one
    page worth of memory regardless of document size.
    """
    try:
        from pdf2image import convert_from_path
    except ImportError:
        log.warning("pdf2image not installed — cannot render PDF pages")
        return None

    p = Path(pdf_path)
    if not p.exists():
        log.warning("PDF not found: %s", pdf_path)
        return None

    # If the path is a .enki envelope, extract the embedded body to a
    # temp file and render that. Case-file docs ingested via
    # reacquire_case_files.py live as .enki on disk; the OCR pipeline
    # needs raw PDF bytes to render.
    render_path: Path = p
    tmp_holder = None
    if p.suffix == ".enki":
        try:
            from core.enki.format import load_enki
            import tempfile
            obj = load_enki(p, verify=False)
            body = obj.content if hasattr(obj, "content") else obj.body
            if body and body[:4] == b"%PDF":
                tmp_holder = tempfile.NamedTemporaryFile(
                    suffix=".pdf", delete=False
                )
                tmp_holder.write(body)
                tmp_holder.flush()
                tmp_holder.close()
                render_path = Path(tmp_holder.name)
            else:
                log.warning("enki body is not PDF: %s", pdf_path)
                return None
        except Exception as e:
            log.warning("failed to extract PDF from enki %s: %s", pdf_path, e)
            return None

    try:
        images = convert_from_path(
            str(render_path),
            dpi=RENDER_DPI,
            first_page=page_number,
            last_page=page_number,
            fmt="png",
        )
    except Exception as e:
        log.warning("pdf2image failed for %s page %d: %s",
                    pdf_path, page_number, e)
        return None
    finally:
        if tmp_holder is not None:
            try: Path(tmp_holder.name).unlink(missing_ok=True)
            except Exception: pass

    if not images:
        return None

    buf = io.BytesIO()
    images[0].save(buf, format="PNG")
    return buf.getvalue()


def render_pages(pdf_path: str, page_numbers: list[int]):
    """Generator: yield (page_number, png_bytes) for each page in `page_numbers`.
    Renders one at a time so memory stays flat."""
    for n in page_numbers:
        png = render_page(pdf_path, n)
        if png is not None:
            yield n, png


# Persistent page-image store (NOT /tmp — that clears and takes the source with
# it). Served read-only so every extraction can be viewed on its source page.
PAGE_IMAGE_ROOT = os.getenv("ENKI_PAGE_IMAGE_ROOT", "/app/storage/page_images")


def save_page_image(pdf_path: str, page_number: int, document_id: int,
                    quality: int = 72) -> Optional[str]:
    """Render one page and persist it as a JPEG under PAGE_IMAGE_ROOT/<doc>/<n>.jpg.
    Returns the absolute path (to store in document_pages.on_disk_image) or None.
    JPEG keeps a 200-DPI legal page at ~100-200 KB so a whole corpus fits on disk."""
    png = render_page(pdf_path, page_number)
    if not png:
        return None
    try:
        from PIL import Image
        out_dir = Path(PAGE_IMAGE_ROOT) / str(document_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{page_number}.jpg"
        Image.open(io.BytesIO(png)).convert("RGB").save(
            out_path, "JPEG", quality=quality, optimize=True)
        return str(out_path)
    except Exception as e:
        log.warning("save_page_image failed for %s p%d: %s", pdf_path, page_number, e)
        return None
