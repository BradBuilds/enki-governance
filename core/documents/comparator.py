"""
Dual-extraction comparator — runs BOTH native and OCR readings of every page,
then asks an LLM judge to score them against each other.

Why this exists:
  Single-engine extraction lies in two ways. pdfplumber confidently returns
  garbage on rendered/encoded-fonts PDFs. PaddleOCR happily invents words
  on dense text. The only honest way to know which engine to trust on THIS
  document class is to run both and have a third party (the LLM) judge.

What this produces, per page:
    document_page_readings rows:
      • engine='pdfplumber'       native pdfplumber text
      • engine='ocr_paddle'       paddle-on-rendered-image text
      • engine='judge_merged'     the chosen/merged text + judge verdict
    The judge_merged row's is_chosen=TRUE and its raw_text overwrites
    document_pages.raw_text — that text is what flows to mention extraction
    and node collapse.

Why no per-page LLM-vision here:
  We only invoke the vision model when one of the cheaper engines is
  catastrophically wrong (judge says 'reject_both'). Calling vision on
  every page of every doc would burn the GPU; the judge tells us where
  it's actually needed.

Public API:

    compare_document(document_id, llm=..., force=False) -> summary

    compare_page(page_id, llm=..., force=False) -> verdict

Calibration output (per corpus) is written to corpus_extraction_policy by
a separate aggregation step (not in this module) once enough pages have
been judged.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.documents.comparator")


# ─── Engine wrappers ────────────────────────────────────────────────────

def _read_native(page_text: str) -> dict:
    """We already have pdfplumber's reading on document_pages.raw_text from
    the ingester. Wrap it as a reading record."""
    text = (page_text or "").strip()
    return {
        "engine": "pdfplumber",
        "raw_text": text,
        "confidence": None,           # pdfplumber doesn't expose char confidence
        "char_count": len(text),
        "word_count": len(text.split()),
        "line_count": text.count("\n") + 1 if text else 0,
        "metadata": {},
    }


def _read_ocr(pdf_path: str, page_number: int) -> Optional[dict]:
    """Render the page and run PaddleOCR. Returns None if rendering or OCR
    is unavailable in this environment."""
    try:
        from core.ocr.pdf_renderer import render_page
        from core.ocr.engines import paddle_extract
    except Exception as e:
        log.warning("OCR engines not importable: %s", e)
        return None
    png = render_page(pdf_path, page_number)
    if png is None:
        return None
    result = paddle_extract(png)
    text = (result.get("text") or "").strip()
    return {
        "engine": "ocr_tesseract",
        "raw_text": text,
        "confidence": result.get("confidence"),
        "char_count": len(text),
        "word_count": len(text.split()),
        "line_count": text.count("\n") + 1 if text else 0,
        "metadata": {"line_count_from_paddle": len(result.get("lines") or [])},
    }


# ─── LLM judge ──────────────────────────────────────────────────────────

_JUDGE_PROMPT = """\
You are auditing two independent text extractions of the same scanned/PDF page.
One came from a native PDF text-pull (pdfplumber). The other came from
optical character recognition (PaddleOCR running on the page rendered to an
image). Decide which one is more faithful to the page, or whether to merge.

Return ONLY a JSON object of the form:
  {
    "winner": "native" | "ocr" | "merge" | "reject_both",
    "confidence": 0.0..1.0,
    "merged_text": "..."                  // present iff winner == "merge"
                                          // OR if you want to correct OCR/native errors
    "differences": [                       // optional, up to 5 most material
      {"location": "line 3", "native": "...", "ocr": "...", "verdict": "..."}
    ],
    "rationale": "one-sentence why"
  }

Choose "native" when pdfplumber clearly captured the page correctly and OCR
introduced spurious characters or word breaks.
Choose "ocr" when pdfplumber returned mojibake / missing text / wrong character
order and OCR is more readable.
Choose "merge" when each engine caught content the other missed and the
merged text is strictly better than either.
Choose "reject_both" only if neither extraction is usable — this should be
rare; the worker will escalate to vision-LLM on reject.

NATIVE_TEXT:
<<<
{native}
>>>

OCR_TEXT:
<<<
{ocr}
>>>
"""


def _call_judge(native_text: str, ocr_text: str, llm: Any) -> dict:
    """Send both readings to the LLM judge. Returns the parsed verdict
    dict, or a fallback verdict if the LLM call fails."""
    MAX = 4000
    native_t = (native_text or "")[:MAX]
    ocr_t = (ocr_text or "")[:MAX]
    prompt = _JUDGE_PROMPT.replace("{native}", native_t).replace("{ocr}", ocr_t)

    try:
        from core.ai.provider import LLMMessage
        messages = [
            LLMMessage(role="system",
                       content="You return only valid JSON. No commentary."),
            LLMMessage(role="user", content=prompt),
        ]
        response = llm.complete(messages, max_tokens=1500, temperature=0.0)
    except Exception as e:
        log.debug("Judge LLM call failed: %s", e)
        return _fallback_verdict(native_text, ocr_text, reason=f"llm_error: {e}")

    if response is None or getattr(response, "error", None):
        return _fallback_verdict(
            native_text, ocr_text,
            reason=f"llm_error: {getattr(response, 'error', 'no_response')}",
        )

    raw = (response.content or "").strip()
    # Models often wrap JSON in ```json ... ``` fences.
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    if raw.startswith("json"):
        raw = raw[4:].lstrip()

    try:
        parsed = json.loads(raw)
    except Exception:
        # Some models still emit prefix text. Salvage the first {...} block.
        try:
            start = raw.index("{")
            end = raw.rindex("}") + 1
            parsed = json.loads(raw[start:end])
        except Exception as e:
            return _fallback_verdict(native_text, ocr_text,
                                     reason=f"json_parse_failed: {e}")

    if not isinstance(parsed, dict):
        return _fallback_verdict(native_text, ocr_text, reason="non_dict_response")

    winner = parsed.get("winner")
    if winner not in ("native", "ocr", "merge", "reject_both"):
        return _fallback_verdict(native_text, ocr_text, reason="bad_winner_value")

    return {
        "winner": winner,
        "confidence": float(parsed.get("confidence", 0.5) or 0.5),
        "merged_text": parsed.get("merged_text"),
        "differences": parsed.get("differences") or [],
        "rationale": parsed.get("rationale") or "",
    }


def _fallback_verdict(native_text: str, ocr_text: str, reason: str) -> dict:
    """When the judge can't run (no LLM, error, bad output), pick the
    longer non-trivial reading. Better than silently dropping the page."""
    nt = (native_text or "").strip()
    ot = (ocr_text or "").strip()
    if len(nt) >= len(ot):
        winner = "native"
    else:
        winner = "ocr"
    return {
        "winner": winner,
        "confidence": 0.3,
        "merged_text": None,
        "differences": [],
        "rationale": f"fallback (no judge): {reason}",
    }


# ─── Persistence ────────────────────────────────────────────────────────

def _save_reading(
    document_id: int, page_id: int, reading: dict, is_chosen: bool = False,
    judge_verdict: Optional[dict] = None,
) -> None:
    """Upsert one reading row (unique on (page_id, engine))."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO document_page_readings
                    (document_id, page_id, engine, raw_text, confidence,
                     char_count, word_count, line_count, metadata,
                     judge_verdict, is_chosen)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
                ON CONFLICT (page_id, engine) DO UPDATE SET
                    raw_text = EXCLUDED.raw_text,
                    confidence = EXCLUDED.confidence,
                    char_count = EXCLUDED.char_count,
                    word_count = EXCLUDED.word_count,
                    line_count = EXCLUDED.line_count,
                    metadata = EXCLUDED.metadata,
                    judge_verdict = COALESCE(EXCLUDED.judge_verdict, document_page_readings.judge_verdict),
                    is_chosen = EXCLUDED.is_chosen
                """,
                (
                    document_id, page_id, reading["engine"],
                    reading.get("raw_text"), reading.get("confidence"),
                    reading.get("char_count"), reading.get("word_count"),
                    reading.get("line_count"),
                    json.dumps(reading.get("metadata") or {}),
                    json.dumps(judge_verdict) if judge_verdict else None,
                    is_chosen,
                ),
            )
        conn.commit()


def _write_chosen_text_back(page_id: int, chosen_text: str, engine_label: str) -> None:
    """Update document_pages.raw_text and extraction_method to reflect the
    judge's pick. Downstream readers (mention extractor, boilerplate) only
    consult document_pages, so this is the gate that lets validated text
    flow to node collapse."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE document_pages
                   SET raw_text = %s,
                       extraction_method = %s
                 WHERE page_id = %s
                """,
                (chosen_text, engine_label, page_id),
            )
        conn.commit()


# ─── Page-level orchestration ───────────────────────────────────────────

def compare_page(
    *, document_id: int, page_id: int, page_number: int,
    page_text_native: str, pdf_path: str,
    llm: Any = None, force: bool = False,
) -> dict:
    """Run both engines + judge for one page. Returns the judge verdict."""
    # 1) Native reading is what we already have on document_pages.
    native = _read_native(page_text_native)
    _save_reading(document_id, page_id, native, is_chosen=False)

    # 2) OCR reading — render + paddle.
    ocr = _read_ocr(pdf_path, page_number)
    if ocr is None:
        # Couldn't OCR (paddle missing, render failed). Single-engine fallback.
        verdict = {
            "winner": "native",
            "confidence": 0.5,
            "merged_text": None,
            "differences": [],
            "rationale": "ocr_unavailable",
        }
        _save_reading(
            document_id, page_id,
            {"engine": "judge_merged", "raw_text": native["raw_text"],
             "confidence": 0.5, "char_count": native["char_count"],
             "word_count": native["word_count"],
             "line_count": native["line_count"], "metadata": {}},
            is_chosen=True, judge_verdict=verdict,
        )
        _write_chosen_text_back(page_id, native["raw_text"], "pdfplumber")
        return verdict

    _save_reading(document_id, page_id, ocr, is_chosen=False)

    # 3) Judge if we have an LLM, otherwise fall back to length heuristic.
    if llm is None:
        verdict = _fallback_verdict(native["raw_text"], ocr["raw_text"],
                                    reason="no_llm_provided")
    else:
        verdict = _call_judge(native["raw_text"], ocr["raw_text"], llm)

    # 4) Pick the winner's text.
    winner = verdict["winner"]
    if winner == "merge" and verdict.get("merged_text"):
        chosen_text = verdict["merged_text"]
        chosen_label = "judge_merge"
    elif winner == "ocr":
        chosen_text = ocr["raw_text"]
        chosen_label = "ocr_tesseract"
    elif winner == "reject_both":
        # Defensive: keep native so downstream isn't blank, but mark for
        # vision-LLM escalation in a future pass.
        chosen_text = native["raw_text"]
        chosen_label = "needs_vision_review"
    else:
        chosen_text = native["raw_text"]
        chosen_label = "pdfplumber"

    chosen_reading = {
        "engine": "judge_merged",
        "raw_text": chosen_text,
        "confidence": verdict.get("confidence", 0.5),
        "char_count": len(chosen_text or ""),
        "word_count": len((chosen_text or "").split()),
        "line_count": (chosen_text or "").count("\n") + 1 if chosen_text else 0,
        "metadata": {"chosen_engine": chosen_label},
    }
    _save_reading(
        document_id, page_id, chosen_reading,
        is_chosen=True, judge_verdict=verdict,
    )
    _write_chosen_text_back(page_id, chosen_text, chosen_label)

    return verdict


# ─── Document-level orchestration ───────────────────────────────────────

def compare_document(
    *, document_id: int, llm: Any = None, force: bool = False,
    page_limit: Optional[int] = None,
) -> dict:
    """Run the comparator across every page of a document. Returns aggregate.

    force: when False, skip pages that already have a judge_merged reading.
    page_limit: cap pages processed this call (useful for big docs)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT on_disk_path, page_count FROM documents WHERE document_id = %s",
                (document_id,),
            )
            row = cur.fetchone()
            if not row:
                return {"ok": False, "error": "document_not_found"}
            pdf_path, page_count = row

            # Pull pages + their current text; skip already-judged when not force.
            sql = """
                SELECT p.page_id, p.page_number, p.raw_text
                  FROM document_pages p
                 WHERE p.document_id = %s
            """
            params: list = [document_id]
            if not force:
                sql += """
                  AND NOT EXISTS (
                    SELECT 1 FROM document_page_readings r
                     WHERE r.page_id = p.page_id AND r.engine = 'judge_merged'
                  )
                """
            sql += " ORDER BY p.page_number ASC"
            cur.execute(sql, params)
            pages = cur.fetchall()

    if page_limit:
        pages = pages[:page_limit]

    counters = {"native_wins": 0, "ocr_wins": 0, "merges": 0,
                "rejects": 0, "fallbacks": 0, "errors": 0}
    sample_verdicts: list[dict] = []

    for page_id, page_number, raw_text in pages:
        try:
            verdict = compare_page(
                document_id=document_id, page_id=page_id,
                page_number=page_number, page_text_native=raw_text or "",
                pdf_path=pdf_path, llm=llm, force=force,
            )
        except Exception as e:
            log.exception("compare_page failed doc=%s page=%s", document_id, page_number)
            counters["errors"] += 1
            continue

        w = verdict.get("winner")
        if w == "native":
            counters["native_wins"] += 1
        elif w == "ocr":
            counters["ocr_wins"] += 1
        elif w == "merge":
            counters["merges"] += 1
        elif w == "reject_both":
            counters["rejects"] += 1

        if "fallback" in (verdict.get("rationale") or ""):
            counters["fallbacks"] += 1

        if len(sample_verdicts) < 5:
            sample_verdicts.append({
                "page_number": page_number,
                "winner": w,
                "confidence": verdict.get("confidence"),
                "rationale": verdict.get("rationale", "")[:200],
                "differences_count": len(verdict.get("differences") or []),
            })

    return {
        "ok": True,
        "document_id": document_id,
        "pages_processed": len(pages),
        "counters": counters,
        "sample_verdicts": sample_verdicts,
    }
