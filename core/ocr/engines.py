"""
OCR engines — Tesseract primary, Ollama vision fallback.

Public surface:

    extract_page(image_bytes, *, min_confidence=0.85, llm_fallback=True)
        → {"text": str, "confidence": float, "engine": str, "lines": [...]}

    tesseract_extract(image_bytes) -> {"text", "confidence", "lines"}
    vision_extract(image_bytes)    -> {"text", "confidence"}

    # Back-compat alias (the comparator + worker import paddle_extract).
    paddle_extract = tesseract_extract

The orchestrator runs Tesseract first; if mean word confidence is below the
threshold, it re-runs the page through the Ollama vision model and (if the
LLM reading is longer / more coherent) replaces the text.

Engine identifier strings written to `document_pages.extraction_method`:
    'ocr_tesseract'           Tesseract alone, confidence above threshold
    'ocr_tesseract+llm_vision' Tesseract low-conf, replaced/augmented by LLM
    'llm_vision'              Tesseract failed/disabled, LLM vision only

Both engines lazy-import their heavy deps so a worker that never sees a
scanned page never pays the import cost.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
from typing import Optional

log = logging.getLogger("enki.ocr.engines")

# Threshold below which we trigger the vision fallback. Mirrors the plan's
# dataset_policy.extraction.ocr_confidence_min default of 0.85.
DEFAULT_MIN_CONFIDENCE = float(os.getenv("ENKI_OCR_MIN_CONFIDENCE", "0.85"))

# Ollama vision model used for fallback. Same env var as image_analyst so
# operators pick one model for both image flows.
OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
VISION_MODEL = os.getenv("ENKI_VISION_MODEL", "llava:7b")

# ─── Tesseract ──────────────────────────────────────────────────────────

def tesseract_extract(image_bytes: bytes) -> dict:
    """Run Tesseract OCR on one page image. Returns:
        {"text": "...", "confidence": 0.0..1.0, "lines": [{text, conf}, ...]}
    Confidence is the mean per-word confidence reported by Tesseract."""
    if not image_bytes:
        return {"text": "", "confidence": 0.0, "lines": []}
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        log.warning("pytesseract/Pillow not available — OCR disabled")
        return {"text": "", "confidence": 0.0, "lines": []}

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as e:
        log.warning("Could not decode page image: %s", e)
        return {"text": "", "confidence": 0.0, "lines": []}

    # Full plain-text reading (preserves layout-ish line breaks).
    try:
        text_out = pytesseract.image_to_string(img) or ""
    except Exception as e:
        log.warning("tesseract image_to_string failed: %s", e)
        return {"text": "", "confidence": 0.0, "lines": []}

    # Per-word confidences via image_to_data (TSV). Cheap; same model run.
    confs: list[float] = []
    try:
        data = pytesseract.image_to_data(
            img, output_type=pytesseract.Output.DICT,
        )
        for conf in data.get("conf", []):
            try:
                c = float(conf)
            except Exception:
                continue
            if c >= 0:  # tesseract uses -1 for "no confidence" (blank rows)
                confs.append(c / 100.0)
    except Exception as e:
        log.debug("tesseract image_to_data failed: %s", e)

    mean_conf = sum(confs) / len(confs) if confs else 0.0
    lines = [{"text": line, "conf": mean_conf}
             for line in text_out.splitlines() if line.strip()]
    return {"text": text_out.strip(), "confidence": mean_conf, "lines": lines}


# Back-compat alias. The comparator + worker import `paddle_extract`; the
# function under that name now delegates to Tesseract. When PaddleOCR is
# fixed in the container, this can flip back without touching callers.
paddle_extract = tesseract_extract


# ─── Ollama vision ──────────────────────────────────────────────────────

_VISION_PROMPT = (
    "You are a careful OCR assistant. Read the text in this scanned document "
    "page exactly as it appears, preserving line breaks and paragraph structure. "
    "Do NOT summarize, do NOT add commentary, do NOT translate. If the page is "
    "blank or unreadable, return an empty string.\n\n"
    "Return ONLY valid JSON of the form:\n"
    '  {"text": "the full transcribed text", "legible": true/false, '
    '"redactions_present": true/false}'
)


def vision_extract(image_bytes: bytes) -> dict:
    """Run the Ollama vision model on one page image. Returns:
        {"text": "...", "confidence": 0.0..1.0, "legible": bool,
         "redactions_present": bool}
    Confidence is heuristic — 0.9 for legible+text, 0.3 for illegible."""
    if not image_bytes:
        return {"text": "", "confidence": 0.0, "legible": False,
                "redactions_present": False}
    try:
        import httpx
    except ImportError:
        log.warning("httpx not available — cannot call Ollama")
        return {"text": "", "confidence": 0.0, "legible": False,
                "redactions_present": False}

    image_b64 = base64.b64encode(image_bytes).decode()
    try:
        r = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": VISION_MODEL,
                "prompt": _VISION_PROMPT,
                "images": [image_b64],
                "stream": False,
                "options": {"temperature": 0.0, "num_predict": 4096},
            },
            timeout=180,
        )
        raw = r.json().get("response", "").strip()
    except Exception as e:
        log.warning("Vision LLM call failed: %s", e)
        return {"text": "", "confidence": 0.0, "legible": False,
                "redactions_present": False}

    # Strip markdown fences the model sometimes adds.
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    if raw.startswith("json"):
        raw = raw[4:]

    try:
        parsed = json.loads(raw.strip())
    except Exception:
        # The model didn't return JSON — fall back to treating the whole
        # response as the transcribed text. Better than dropping it.
        return {"text": raw.strip(), "confidence": 0.6, "legible": True,
                "redactions_present": False}

    text = (parsed.get("text") or "").strip()
    legible = bool(parsed.get("legible", bool(text)))
    redactions = bool(parsed.get("redactions_present", False))
    confidence = 0.9 if (legible and text) else 0.3
    return {"text": text, "confidence": confidence, "legible": legible,
            "redactions_present": redactions}


# ─── Orchestrator ───────────────────────────────────────────────────────

def extract_page(
    image_bytes: bytes,
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    llm_fallback: bool = True,
) -> dict:
    """Run the primary engine, fall back to vision on low confidence.

    Returns:
      {"text": str, "confidence": float, "engine": str,
       "lines": list, "redactions_present": bool}
    """
    primary = tesseract_extract(image_bytes)
    primary_text = primary["text"]
    primary_conf = primary["confidence"]

    if primary_text and primary_conf >= min_confidence:
        return {
            "text": primary_text,
            "confidence": primary_conf,
            "engine": "ocr_tesseract",
            "lines": primary["lines"],
            "redactions_present": False,
        }

    if not llm_fallback:
        return {
            "text": primary_text,
            "confidence": primary_conf,
            "engine": "ocr_tesseract" if primary_text else "none",
            "lines": primary["lines"],
            "redactions_present": False,
        }

    vision = vision_extract(image_bytes)
    vision_text = vision["text"]
    vision_conf = vision["confidence"]

    if not primary_text and vision_text:
        return {
            "text": vision_text,
            "confidence": vision_conf,
            "engine": "llm_vision",
            "lines": [],
            "redactions_present": vision["redactions_present"],
        }

    if vision_text and (
        len(vision_text) > len(primary_text) * 1.2 or vision_conf > primary_conf
    ):
        return {
            "text": vision_text,
            "confidence": max(primary_conf, vision_conf),
            "engine": "ocr_tesseract+llm_vision",
            "lines": primary["lines"],
            "redactions_present": vision["redactions_present"],
            "tesseract_text": primary_text,
        }

    return {
        "text": primary_text,
        "confidence": primary_conf,
        "engine": "ocr_tesseract+llm_vision",
        "lines": primary["lines"],
        "redactions_present": vision["redactions_present"],
        "vision_text": vision_text,
    }


def get_engine_status() -> dict:
    """Lightweight diagnostic for the worker's /status endpoint."""
    try:
        import pytesseract  # noqa: F401
        tesseract_ok = True
    except ImportError:
        tesseract_ok = False
    return {
        "primary_engine": "tesseract",
        "tesseract_available": tesseract_ok,
        "vision_model": VISION_MODEL,
        "ollama_url": OLLAMA_URL,
        "min_confidence": DEFAULT_MIN_CONFIDENCE,
    }
