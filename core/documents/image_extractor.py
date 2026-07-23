"""
PDF image extractor — pulls every embedded raster image out of a PDF and
persists it with full provenance back to the source document + page.

Uses pdfimages (poppler tool, already installed in our Dockerfile). For
each image:
  • Computes sha256 + perceptual hash (pHash for near-dup clustering)
  • Stores on disk at /opt/enki/storage/doc_images/{doc_id}/p{page}_i{idx}.{ext}
  • Writes a document_images row with bbox (when available) + sizes
  • Skips re-extraction if (document_id, page, image_index) row exists

This is the substrate; analysis (vision LLM, OCR, faces) runs separately
via core/documents/image_analysis.py.

Public surface:
    extract_images_from_pdf(document_id, *, force=False) -> dict
    extract_corpus(corpus_id, *, force=False) -> dict
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.documents.image_extractor")

# Storage layout: <STORAGE_ROOT>/{document_id}/p{page}_i{idx}.{ext}
#
# We probe two candidate roots in order:
#   1. ENKI_IMAGE_STORAGE_ROOT (operator override)
#   2. /opt/enki/storage/doc_images (production, when /opt/enki is writable)
#   3. /tmp/enki_images (container fallback — /opt isn't writable for the
#      enki user in our prod docker image, so this is the working default)
# We pick the first one we can actually create + write to.

def _pick_storage_root() -> Path:
    override = os.environ.get("ENKI_IMAGE_STORAGE_ROOT")
    candidates = []
    if override:
        candidates.append(Path(override))
    candidates.append(Path("/opt/enki/storage/doc_images"))
    candidates.append(Path("/tmp/enki_images"))
    for c in candidates:
        try:
            c.mkdir(parents=True, exist_ok=True)
            if os.access(c, os.W_OK):
                return c
        except Exception:
            continue
    # Last resort — even if mkdir failed everywhere, return /tmp and let
    # the first write attempt blow up loudly.
    return Path("/tmp/enki_images")


_STORAGE_ROOT = _pick_storage_root()

# pHash bit width — 64 bits is the standard for imagehash.phash. We store
# it as signed BIGINT in postgres so we can do Hamming-distance queries.
_PHASH_SIZE = 8   # 8x8 = 64 bits


def _ensure_storage_dir(document_id: int) -> Path:
    d = _STORAGE_ROOT / str(document_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _compute_phash(image_bytes: bytes) -> Optional[int]:
    """Compute the perceptual hash. Returns signed 64-bit int or None."""
    try:
        import imagehash
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(image_bytes))
        # imagehash returns a 64-bit hash for default size. Convert to int.
        h = imagehash.phash(img, hash_size=_PHASH_SIZE)
        # imagehash hash is an array; pack to int
        bits = "".join("1" if b else "0" for b in h.hash.flatten())
        unsigned = int(bits, 2)
        # Convert to signed 64-bit for postgres BIGINT
        if unsigned >= 2**63:
            return unsigned - 2**64
        return unsigned
    except Exception as e:
        log.debug("phash failed: %s", e)
        return None


def _is_redacted_block(image_bytes: bytes) -> bool:
    """A pure-black or pure-white image is almost certainly a redaction
    block or page-divider noise. Cheap heuristic — sample pixels and check
    variance."""
    try:
        from PIL import Image, ImageStat
        import io
        img = Image.open(io.BytesIO(image_bytes)).convert("L")    # grayscale
        stat = ImageStat.Stat(img)
        # Mean very low (~0, black) AND stddev very low → solid block
        return stat.mean[0] < 5 and stat.stddev[0] < 3
    except Exception:
        return False


# ─── pdfimages output parsing ───────────────────────────────────────────

# pdfimages -list output looks like:
# page   num  type   width height color comp bpc  enc interp  object ID x-ppi y-ppi size ratio
#    1     0 image     800   600  rgb     3   8  jpeg   no       12  0   200   200 23K  4.0%
# Lines starting with "page" or "---" are header/separator.
_PDFIMAGES_LIST_RE = re.compile(
    r"^\s*(\d+)\s+(\d+)\s+image\s+(\d+)\s+(\d+)\s+(\w+)\s+(\d+)\s+(\d+)\s+(\w+)"
)


def _list_pdf_images(pdf_path: str) -> list[dict]:
    """Run `pdfimages -list` to enumerate every image in the PDF.
    Returns [{page, num, width, height, mime}, ...]."""
    try:
        result = subprocess.run(
            ["pdfimages", "-list", pdf_path],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except FileNotFoundError:
        log.error("pdfimages not installed — install poppler-utils")
        return []
    except subprocess.TimeoutExpired:
        log.warning("pdfimages -list timed out for %s", pdf_path)
        return []
    if result.returncode != 0:
        log.debug("pdfimages -list non-zero for %s: %s", pdf_path, result.stderr[:300])
        return []
    out: list[dict] = []
    for line in result.stdout.splitlines():
        m = _PDFIMAGES_LIST_RE.match(line)
        if not m:
            continue
        page = int(m.group(1))
        # m.group(2) is the per-page image index in pdfimages
        # m.group(3) width, m.group(4) height
        # m.group(8) is the encoder/mime hint
        out.append({
            "page": page,
            "pdfimages_id": int(m.group(2)),
            "width": int(m.group(3)),
            "height": int(m.group(4)),
            "color": m.group(5),
            "mime_hint": m.group(8),
        })
    return out


def _extract_images_to_dir(pdf_path: str, out_dir: Path) -> list[Path]:
    """Run `pdfimages -all` to dump every image. Returns the list of
    output file paths (in extraction order)."""
    prefix = str(out_dir / "img")
    try:
        result = subprocess.run(
            ["pdfimages", "-all", pdf_path, prefix],
            capture_output=True, text=True, timeout=600, check=False,
        )
    except FileNotFoundError:
        log.error("pdfimages not installed")
        return []
    except subprocess.TimeoutExpired:
        log.warning("pdfimages -all timed out for %s", pdf_path)
        return []
    if result.returncode != 0:
        log.debug("pdfimages -all non-zero: %s", result.stderr[:300])

    # Collect output files. pdfimages names them img-NNN.<ext>.
    out_files = sorted(p for p in out_dir.glob("img-*.*"))
    return out_files


def _existing_image_ids(document_id: int) -> set[tuple[int, int]]:
    """Return set of (page_number, image_index) already extracted for this doc."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.page_number, i.image_index
                  FROM document_images i
                  JOIN document_pages p ON p.page_id = i.page_id
                 WHERE i.document_id = %s
                """,
                (document_id,),
            )
            return {(r[0], r[1]) for r in cur.fetchall()}


def _page_id_lookup(document_id: int) -> dict[int, int]:
    """Map page_number → page_id for this document."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT page_number, page_id FROM document_pages WHERE document_id = %s",
                (document_id,),
            )
            return {r[0]: r[1] for r in cur.fetchall()}


# ─── Public ─────────────────────────────────────────────────────────────

def extract_images_from_pdf(
    document_id: int, *, force: bool = False,
) -> dict:
    """Extract every embedded image from one PDF's source file.
    Idempotent on (document_id, page, image_index) unless force=True."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT on_disk_path, dataset_id, title FROM documents WHERE document_id = %s",
                (document_id,),
            )
            row = cur.fetchone()
            if not row:
                return {"ok": False, "error": "document_not_found",
                        "document_id": document_id}
            pdf_path, dataset_id, title = row

    if not pdf_path or not os.path.exists(pdf_path):
        return {"ok": False, "error": "source_pdf_missing",
                "on_disk_path": pdf_path, "document_id": document_id}

    page_id_map = _page_id_lookup(document_id)
    existing = set() if force else _existing_image_ids(document_id)

    # List first (cheap) so we know what we'd extract
    listing = _list_pdf_images(pdf_path)
    if not listing:
        return {"ok": True, "document_id": document_id,
                "images_listed": 0, "images_extracted": 0,
                "reason": "no_images_found_or_pdfimages_failed"}

    # Bucket by page
    per_page: dict[int, list[dict]] = {}
    for img in listing:
        per_page.setdefault(img["page"], []).append(img)

    store_dir = _ensure_storage_dir(document_id)

    # Run extraction into a temp dir then move into final layout
    with tempfile.TemporaryDirectory(prefix="enki_imgex_") as tmp:
        tmp_path = Path(tmp)
        out_files = _extract_images_to_dir(pdf_path, tmp_path)
        if not out_files:
            return {"ok": True, "document_id": document_id,
                    "images_listed": len(listing),
                    "images_extracted": 0,
                    "reason": "pdfimages_produced_no_files"}

        # pdfimages output is in the same order as the listing. Match by
        # index — file 0 corresponds to listing[0].
        extracted = 0
        skipped = 0
        errors = 0

        # We need a running per-page index since pdfimages globals each img
        # but our schema is (document_id, page, image_index_within_page).
        page_running_idx: dict[int, int] = {}

        with get_connection() as conn:
            with conn.cursor() as cur:
                for i, listing_row in enumerate(listing):
                    page = listing_row["page"]
                    page_idx = page_running_idx.get(page, 0)
                    page_running_idx[page] = page_idx + 1

                    if (page, page_idx) in existing and not force:
                        skipped += 1
                        continue

                    page_id = page_id_map.get(page)
                    if not page_id:
                        # pdfimages saw a page our DB doesn't — odd
                        log.debug("no page_id for page %d in doc %d", page, document_id)
                        errors += 1
                        continue

                    # Match the output file by index
                    if i >= len(out_files):
                        errors += 1
                        continue
                    src_file = out_files[i]
                    try:
                        with open(src_file, "rb") as f:
                            image_bytes = f.read()
                    except Exception:
                        errors += 1
                        continue

                    sha = _sha256(image_bytes)
                    phash = _compute_phash(image_bytes)
                    is_red = _is_redacted_block(image_bytes)
                    ext = src_file.suffix.lstrip(".") or "bin"
                    target_name = f"p{page:04d}_i{page_idx:02d}.{ext}"
                    target_path = store_dir / target_name
                    try:
                        if force and target_path.exists():
                            target_path.unlink()
                        src_file.rename(target_path)
                    except Exception:
                        # If rename fails, copy
                        with open(target_path, "wb") as f:
                            f.write(image_bytes)

                    try:
                        cur.execute(
                            """
                            INSERT INTO document_images
                                (document_id, page_id, image_index, on_disk_path,
                                 sha256, phash, mime, width, height, bytes,
                                 is_redacted_block, extraction_engine)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pdfimages')
                            ON CONFLICT (document_id, page_id, image_index) DO UPDATE SET
                                on_disk_path = EXCLUDED.on_disk_path,
                                sha256 = EXCLUDED.sha256,
                                phash = EXCLUDED.phash,
                                mime = EXCLUDED.mime,
                                width = EXCLUDED.width,
                                height = EXCLUDED.height,
                                bytes = EXCLUDED.bytes,
                                is_redacted_block = EXCLUDED.is_redacted_block,
                                extracted_at = NOW()
                            """,
                            (
                                document_id, page_id, page_idx, str(target_path),
                                sha, phash,
                                f"image/{ext}",
                                listing_row.get("width"),
                                listing_row.get("height"),
                                len(image_bytes),
                                is_red,
                            ),
                        )
                        extracted += 1
                    except Exception as e:
                        log.exception("insert failed for img %d:%d:%d", document_id, page, page_idx)
                        errors += 1
                conn.commit()

    return {
        "ok": True,
        "document_id": document_id,
        "title": title,
        "dataset_id": dataset_id,
        "images_listed": len(listing),
        "images_extracted": extracted,
        "images_skipped": skipped,
        "errors": errors,
    }


def extract_corpus(corpus_id: str, *, force: bool = False) -> dict:
    """Run image extraction for every document in a corpus."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT document_id FROM documents WHERE dataset_id = %s",
                (corpus_id,),
            )
            doc_ids = [r[0] for r in cur.fetchall()]

    counters = {"docs": len(doc_ids), "total_listed": 0,
                "total_extracted": 0, "total_skipped": 0,
                "total_errors": 0, "doc_failures": 0}
    per_doc: list[dict] = []

    for d in doc_ids:
        try:
            r = extract_images_from_pdf(d, force=force)
        except Exception:
            log.exception("extract_images_from_pdf crashed for %d", d)
            counters["doc_failures"] += 1
            continue
        per_doc.append(r)
        counters["total_listed"] += r.get("images_listed", 0)
        counters["total_extracted"] += r.get("images_extracted", 0)
        counters["total_skipped"] += r.get("images_skipped", 0)
        counters["total_errors"] += r.get("errors", 0)

    return {"ok": True, "corpus_id": corpus_id,
            "counters": counters, "per_doc": per_doc}
