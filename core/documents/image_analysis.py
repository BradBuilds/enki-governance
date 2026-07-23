"""
Document image analyzer — Phase 3.2.

For each image in document_images:
  • Vision LLM (Ollama gemma4 vision) → caption, classification,
    is_intelligence flag, objects[], tags[]
  • Tesseract OCR → text within the image
  • MediaPipe face detection → faces[] with bounding boxes

Each engine's result is one row in document_image_analyses
(UNIQUE (image_id, engine)). Idempotent — re-running with force=True
overwrites; without force skips already-analyzed engine rows.

Reuses the engines from core/workers/image_analyst.py (the signal-side
worker). The difference: we read bytes from document_images.on_disk_path
and write to document_image_analyses instead of knowledge_nodes.

Public surface:
    analyze_image(image_id, *, engines=None, force=False) -> dict
    analyze_corpus(corpus_id, *, limit=None, engines=None) -> dict
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import httpx

from storage.postgres.db import get_connection

log = logging.getLogger("enki.documents.image_analysis")

OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
# Vision model: prefer gemma4 if loaded (we know it's resident on node-A),
# fall back to llava:7b which is the existing image_analyst.py default.
VISION_MODEL = os.getenv("ENKI_DOC_VISION_MODEL", "gemma4:e2b")

# Cap on how long one vision LLM call can take. Big pages can be slow.
_VISION_TIMEOUT_S = 300

# Default engines to run per image. Order matters: classifier first so
# we can skip expensive vision+OCR on decorative/redaction blocks if
# desired by the operator (not done by default — we run all).
_DEFAULT_ENGINES = ("vision", "ocr", "faces")


# ─── Vision LLM ─────────────────────────────────────────────────────────

_VISION_PROMPT = """\
You are analyzing a single page image from an investigative document corpus
(scanned PDFs from court releases, FOIA dumps, etc.). Describe what is on
this page in one sentence, then classify it.

Return ONLY a JSON object of the shape:
{
  "caption": "One sentence describing what is visible on this page",
  "classification": "photo|diagram|signature|seal|map|chart|id_card|redaction|decorative|other|page_scan",
  "is_intelligence": true|false,
  "objects": ["object 1", "object 2", ...],
  "tags": ["short tag 1", "short tag 2", ...],
  "confidence": 0.0
}

Guidance:
- classification='page_scan' for a typewritten/printed page-scan that's mostly text
- classification='signature' for a page that is primarily a signature block
- classification='photo' only for actual photographs of people/places/objects
- classification='diagram' for hand-drawn or technical drawings
- classification='redaction' for pages that are mostly black redaction bars
- classification='decorative' for letterheads, seals, page-numbering elements
- is_intelligence=true if the page contains identifiable people, signatures,
  documents-with-text, aircraft, vessels, vehicles, evidence, or other
  named-entity-bearing content
- is_intelligence=false for blank pages, pure redaction, decorative-only
"""


def _vision_analyze(image_bytes: bytes) -> dict:
    """Call Ollama vision model on the image. Returns parsed JSON dict
    plus the raw response on error."""
    image_b64 = base64.b64encode(image_bytes).decode()
    try:
        r = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": VISION_MODEL,
                "prompt": _VISION_PROMPT,
                "images": [image_b64],
                "stream": False,
                "options": {"temperature": 0.0, "num_predict": 800},
            },
            timeout=_VISION_TIMEOUT_S,
        )
    except Exception as e:
        return {"_error": f"vision_call_failed: {e}"}

    if r.status_code != 200:
        return {"_error": f"vision_http_{r.status_code}", "body": r.text[:300]}

    raw = (r.json().get("response") or "").strip()
    # Strip markdown fences
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    if raw.startswith("json"):
        raw = raw[4:].lstrip()

    try:
        parsed = json.loads(raw)
    except Exception:
        # Salvage first {...} block
        try:
            start = raw.index("{")
            end = raw.rindex("}") + 1
            parsed = json.loads(raw[start:end])
        except Exception:
            return {"_error": "json_parse_failed", "raw": raw[:500]}

    if not isinstance(parsed, dict):
        return {"_error": "non_dict_response", "raw": raw[:300]}

    return {
        "caption": (parsed.get("caption") or "")[:1000],
        "classification": (parsed.get("classification") or "other").lower(),
        "is_intelligence": bool(parsed.get("is_intelligence", False)),
        "objects": list(parsed.get("objects") or [])[:30],
        "tags": list(parsed.get("tags") or [])[:30],
        "confidence": float(parsed.get("confidence") or 0.5),
        "model": VISION_MODEL,
    }


# ─── Tesseract OCR (image-level) ────────────────────────────────────────

def _ocr_analyze(image_bytes: bytes) -> dict:
    """Run Tesseract on this image's bytes. Returns the text + mean
    word-confidence."""
    try:
        import pytesseract
        from PIL import Image
        import io
    except ImportError as e:
        return {"_error": f"ocr_import_failed: {e}"}

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as e:
        return {"_error": f"image_decode_failed: {e}"}

    try:
        text = pytesseract.image_to_string(img) or ""
    except Exception as e:
        return {"_error": f"tesseract_failed: {e}"}

    # Per-word confidence (cheap, same model load as the text call)
    confs: list[float] = []
    try:
        data = pytesseract.image_to_data(
            img, output_type=pytesseract.Output.DICT,
        )
        for c in data.get("conf", []):
            try:
                cf = float(c)
                if cf >= 0:
                    confs.append(cf / 100.0)
            except Exception:
                continue
    except Exception:
        pass

    return {
        "ocr_text": text.strip(),
        "confidence": (sum(confs) / len(confs)) if confs else 0.0,
        "word_count": len(text.split()),
        "char_count": len(text.strip()),
    }


# ─── Face detection (MediaPipe) ─────────────────────────────────────────

def _faces_analyze(image_bytes: bytes) -> dict:
    """Detect faces with MediaPipe. Returns count + bounding boxes."""
    try:
        import mediapipe as mp
        from PIL import Image
        import numpy as np
        import io
    except ImportError as e:
        return {"_error": f"mediapipe_import_failed: {e}"}

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img_array = np.array(img)
    except Exception as e:
        return {"_error": f"image_decode_failed: {e}"}

    try:
        face_detection = mp.solutions.face_detection.FaceDetection(
            model_selection=1, min_detection_confidence=0.5,
        )
        results = face_detection.process(img_array)
    except Exception as e:
        return {"_error": f"face_detect_failed: {e}"}
    finally:
        try:
            face_detection.close()
        except Exception:
            pass

    faces: list[dict] = []
    if results.detections:
        h, w = img_array.shape[:2]
        for d in results.detections:
            bbox = d.location_data.relative_bounding_box
            faces.append({
                "bbox": {
                    "x": int(bbox.xmin * w), "y": int(bbox.ymin * h),
                    "w": int(bbox.width * w), "h": int(bbox.height * h),
                },
                "confidence": round(float(d.score[0]), 3),
            })

    return {"faces_count": len(faces), "faces": faces}


# ─── Persistence ────────────────────────────────────────────────────────

def _save_analysis(image_id: int, engine: str, payload: dict) -> None:
    """UPSERT one analysis row keyed on (image_id, engine)."""
    err = payload.get("_error")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO document_image_analyses
                    (image_id, engine, classification, caption,
                     is_intelligence, confidence,
                     ocr_text, objects, tags,
                     faces_count, faces, metadata, error)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
                ON CONFLICT (image_id, engine) DO UPDATE SET
                    classification    = EXCLUDED.classification,
                    caption           = EXCLUDED.caption,
                    is_intelligence   = EXCLUDED.is_intelligence,
                    confidence        = EXCLUDED.confidence,
                    ocr_text          = EXCLUDED.ocr_text,
                    objects           = EXCLUDED.objects,
                    tags              = EXCLUDED.tags,
                    faces_count       = EXCLUDED.faces_count,
                    faces             = EXCLUDED.faces,
                    metadata          = EXCLUDED.metadata,
                    error             = EXCLUDED.error,
                    created_at        = NOW()
                """,
                (
                    image_id, engine,
                    payload.get("classification"),
                    payload.get("caption"),
                    bool(payload.get("is_intelligence", False)),
                    payload.get("confidence"),
                    payload.get("ocr_text"),
                    payload.get("objects"),
                    payload.get("tags"),
                    payload.get("faces_count", 0),
                    json.dumps(payload.get("faces") or []),
                    json.dumps({k: v for k, v in payload.items()
                                if k not in {"classification", "caption",
                                             "is_intelligence", "confidence",
                                             "ocr_text", "objects", "tags",
                                             "faces_count", "faces", "_error"}}),
                    err,
                ),
            )
        conn.commit()


def _fetch_image(image_id: int) -> Optional[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT image_id, document_id, page_id, on_disk_path,
                       sha256, is_redacted_block
                  FROM document_images
                 WHERE image_id = %s
                """,
                (image_id,),
            )
            r = cur.fetchone()
            if not r:
                return None
            return {
                "image_id": r[0], "document_id": r[1], "page_id": r[2],
                "on_disk_path": r[3], "sha256": r[4],
                "is_redacted_block": r[5],
            }


def _engines_already_done(image_id: int) -> set[str]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT engine FROM document_image_analyses WHERE image_id = %s",
                (image_id,),
            )
            return {r[0] for r in cur.fetchall()}


# ─── Public ─────────────────────────────────────────────────────────────

# Map engine label → (db_engine_name, runner)
_ENGINES = {
    "vision": ("vision_gemma4", _vision_analyze),
    "ocr":    ("tesseract_ocr",  _ocr_analyze),
    "faces":  ("face_mediapipe", _faces_analyze),
}


def analyze_image(
    image_id: int, *,
    engines: Optional[tuple[str, ...]] = None,
    force: bool = False,
) -> dict:
    """Run the selected engines against one image. Returns a per-engine
    result summary."""
    engines = engines or _DEFAULT_ENGINES
    img = _fetch_image(image_id)
    if not img:
        return {"ok": False, "error": "image_not_found", "image_id": image_id}

    path = img["on_disk_path"]
    if not path or not Path(path).exists():
        return {"ok": False, "error": "image_file_missing",
                "image_id": image_id, "on_disk_path": path}

    try:
        with open(path, "rb") as f:
            image_bytes = f.read()
    except Exception as e:
        return {"ok": False, "error": f"read_failed: {e}",
                "image_id": image_id}

    already = set() if force else _engines_already_done(image_id)
    out: dict[str, dict] = {}
    elapsed: dict[str, float] = {}
    for short in engines:
        if short not in _ENGINES:
            continue
        db_name, runner = _ENGINES[short]
        if db_name in already:
            out[db_name] = {"skipped": "already_done"}
            continue
        t0 = time.time()
        try:
            result = runner(image_bytes)
        except Exception as e:
            result = {"_error": f"runner_crashed: {e}"}
        elapsed[db_name] = round(time.time() - t0, 2)
        _save_analysis(image_id, db_name, result)
        out[db_name] = {
            "ok": "_error" not in result,
            "error": result.get("_error"),
            "elapsed_s": elapsed[db_name],
            "snapshot": {k: v for k, v in result.items()
                          if k in {"classification", "is_intelligence",
                                   "faces_count", "char_count",
                                   "confidence"}},
        }

    return {
        "ok": True, "image_id": image_id, "engines": out,
        "total_elapsed_s": round(sum(elapsed.values()), 2),
    }


def analyze_corpus(
    corpus_id: str, *,
    limit: Optional[int] = None,
    engines: Optional[tuple[str, ...]] = None,
    force: bool = False,
    skip_redacted: bool = True,
) -> dict:
    """Run analyzers across every image in the corpus.
    skip_redacted: drop is_redacted_block=true images (pure-black redaction)."""
    sql = """
        SELECT i.image_id
          FROM document_images i
          JOIN documents d ON d.document_id = i.document_id
         WHERE d.dataset_id = %s
    """
    params: list = [corpus_id]
    if skip_redacted:
        sql += " AND NOT i.is_redacted_block"
    sql += " ORDER BY i.image_id"
    if limit:
        sql += f" LIMIT {int(limit)}"

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            ids = [r[0] for r in cur.fetchall()]

    if not ids:
        return {"ok": True, "corpus_id": corpus_id, "images": 0,
                "reason": "no_images"}

    totals = {"analyzed": 0, "skipped": 0, "errors": 0}
    sample: list[dict] = []
    for iid in ids:
        r = analyze_image(iid, engines=engines, force=force)
        if r.get("ok"):
            totals["analyzed"] += 1
            if len(sample) < 5:
                sample.append({"image_id": iid, **r.get("engines", {})})
        else:
            totals["errors"] += 1

    return {
        "ok": True, "corpus_id": corpus_id, "images": len(ids),
        "totals": totals, "sample": sample,
    }
