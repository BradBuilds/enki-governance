"""
Image-mention extractor — Phase 3.3.

For each document_image_analyses row with non-empty ocr_text, run the
existing LLM/rule mention extractor to pull out named entities, then
persist them as document_mentions rows with:
  • extraction_method = 'image_ocr' (or 'image_vision_caption')
  • image_id          = the source image
  • paragraph_id      = NULL (not a paragraph-sourced mention)
  • page_id           = the page the image lives on
  • document_id       = the source document

These mentions flow through the same downstream pipeline as paragraph
mentions: corroboration scoring, context-bag clustering, source
validation, canonical entity promotion. The image_id FK lets the entity
dossier query surface "this entity came from a photo / signature /
specific image" with full provenance.

Public surface:
    extract_from_image(image_id, llm=...) -> dict
    extract_from_corpus(corpus_id, llm=...) -> dict
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.documents.image_mention_extractor")


def _fetch_image_with_analyses(image_id: int) -> Optional[dict]:
    """Pull one image + all its analyses + document context."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.image_id, i.document_id, i.page_id, i.on_disk_path,
                       d.tenant_id, d.dataset_id, p.page_number
                  FROM document_images i
                  JOIN documents d ON d.document_id = i.document_id
                  LEFT JOIN document_pages p ON p.page_id = i.page_id
                 WHERE i.image_id = %s
                """,
                (image_id,),
            )
            r = cur.fetchone()
            if not r:
                return None
            img = {
                "image_id": r[0], "document_id": r[1], "page_id": r[2],
                "on_disk_path": r[3], "tenant_id": r[4],
                "dataset_id": r[5], "page_number": r[6],
            }
            cur.execute(
                """
                SELECT engine, classification, caption, ocr_text
                  FROM document_image_analyses
                 WHERE image_id = %s
                """,
                (image_id,),
            )
            img["analyses"] = [
                {"engine": rr[0], "classification": rr[1],
                 "caption": rr[2], "ocr_text": rr[3]}
                for rr in cur.fetchall()
            ]
    return img


def _existing_image_mentions(image_id: int) -> int:
    """How many mentions already linked to this image (so we can skip)?"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM document_mentions WHERE image_id = %s",
                (image_id,),
            )
            return cur.fetchone()[0]


def _persist_image_mention(
    img: dict, surface_form: str, suggested_type: str,
    char_start: int, char_end: int, confidence: float,
    extraction_method: str,
) -> int:
    """Insert one image-sourced mention. Returns mention_id."""
    from core.documents.mentions import persist_mention
    return persist_mention(
        tenant_id=img["tenant_id"],
        document_id=img["document_id"],
        page_id=img["page_id"],
        paragraph_id=None,            # image-sourced, no paragraph
        char_start=char_start,
        char_end=char_end,
        surface_form=surface_form,
        suggested_type=suggested_type,
        candidate_entity_id=None,
        confidence=confidence,
        resolution_status="unresolved",
        extraction_method=extraction_method,
        extraction_engines={"image_id": img["image_id"]},
    )


def _link_image_id_after_persist(mention_id: int, image_id: int) -> None:
    """persist_mention doesn't take image_id yet. Set it after."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE document_mentions SET image_id = %s WHERE mention_id = %s",
                (image_id, mention_id),
            )
        conn.commit()


# ─── Public ─────────────────────────────────────────────────────────────

def extract_from_image(
    image_id: int, *,
    llm: Optional[Any] = None,
    rule_fallback: bool = True,
    skip_existing: bool = True,
    sources: tuple[str, ...] = ("ocr_text", "caption"),
) -> dict:
    """Extract mentions from one image's analyses (OCR text + caption).
    Each surface (ocr_text or caption) is fed through the mention extractor
    separately so we can attribute via extraction_method."""
    img = _fetch_image_with_analyses(image_id)
    if not img:
        return {"ok": False, "error": "image_not_found", "image_id": image_id}

    if skip_existing and _existing_image_mentions(image_id) > 0:
        return {"ok": True, "image_id": image_id, "skipped": "already_extracted",
                "mentions_extracted": 0}

    from core.documents.extractor import _llm_extract, _rule_extract

    total = 0
    by_source: dict[str, int] = {}

    for analysis in img["analyses"]:
        if not analysis.get("ocr_text") and not analysis.get("caption"):
            continue

        for src in sources:
            text = analysis.get(src)
            if not text or len(text.strip()) < 4:
                continue

            method_label = f"image_{src}"

            # Use LLM if available, otherwise regex
            raw_mentions: list[dict] = []
            if llm is not None:
                try:
                    raw_mentions = _llm_extract(text, llm)
                except Exception as e:
                    log.debug("llm_extract on %s failed: %s", method_label, e)

            if not raw_mentions and rule_fallback:
                raw_mentions = _rule_extract(text)

            for m in raw_mentions:
                surface = m.get("surface_form") or ""
                if not surface or len(surface) < 2:
                    continue
                try:
                    mid = _persist_image_mention(
                        img=img,
                        surface_form=surface,
                        suggested_type=(m.get("suggested_type") or "unknown").lower(),
                        char_start=int(m.get("char_start", 0) or 0),
                        char_end=int(m.get("char_end", 0) or 0),
                        confidence=float(m.get("confidence", 0.6) or 0.6),
                        extraction_method=method_label,
                    )
                    _link_image_id_after_persist(mid, image_id)
                    total += 1
                    by_source[method_label] = by_source.get(method_label, 0) + 1
                except Exception as e:
                    log.debug("persist failed: %s", e)

    return {
        "ok": True, "image_id": image_id,
        "mentions_extracted": total,
        "by_source": by_source,
    }


def extract_from_corpus(
    corpus_id: str, *,
    llm: Optional[Any] = None,
    rule_fallback: bool = True,
    limit: Optional[int] = None,
    skip_existing: bool = True,
) -> dict:
    """Walk every image in the corpus and run mention extraction on its
    OCR + caption."""
    sql = """
        SELECT i.image_id
          FROM document_images i
          JOIN documents d ON d.document_id = i.document_id
         WHERE d.dataset_id = %s
           AND EXISTS (
             SELECT 1 FROM document_image_analyses a
              WHERE a.image_id = i.image_id
                AND (a.ocr_text IS NOT NULL OR a.caption IS NOT NULL)
           )
         ORDER BY i.image_id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (corpus_id,))
            ids = [r[0] for r in cur.fetchall()]

    if not ids:
        return {"ok": True, "corpus_id": corpus_id, "reason": "no_analyzed_images"}

    totals = {"images_processed": 0, "images_skipped": 0,
              "total_mentions": 0, "errors": 0}
    sample: list[dict] = []

    for iid in ids:
        try:
            r = extract_from_image(iid, llm=llm, rule_fallback=rule_fallback,
                                   skip_existing=skip_existing)
        except Exception:
            log.exception("extract_from_image crashed for %d", iid)
            totals["errors"] += 1
            continue
        if r.get("skipped"):
            totals["images_skipped"] += 1
        else:
            totals["images_processed"] += 1
            totals["total_mentions"] += r.get("mentions_extracted", 0)
            if r.get("mentions_extracted", 0) > 0 and len(sample) < 5:
                sample.append(r)

    return {"ok": True, "corpus_id": corpus_id,
            "totals": totals, "sample": sample}
