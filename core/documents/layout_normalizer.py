"""Deterministic layout normalization for PDF text extraction.

Part of the collapse ingestion contract. The plain pdfplumber `page.extract_text()`
flattens multi-column layouts (e.g. condensed 2-column deposition transcripts,
4-up mini-scripts) into cross-column reading order, producing jumbled text that
then collapses faithfully into garbage. This module recovers true reading order
from the page's own word geometry — no OCR, no LLM, no document-type assumptions.

General and content-derived (NOT hardcoded to depositions / column counts /
page fractions):

  * The NUMBER of columns is discovered from the word-coverage profile — the
    low-coverage vertical valleys between text bands are the gutters.
  * Line-number gutters (a thin band of bare numbers 1..25) are recognised by
    CONTENT and dropped; their numbers are line coordinates, not text.
  * Thin/sparse bands (margins, page numbers, running headers) are dropped
    relative to the page's own columns, not a fixed width.
  * Tabular pages (mostly-numeric tokens, or a grid of >=4 real columns) are
    read in natural row order — column-splitting a table would jumble it.

Determinism: a pure function of the PDF's word geometry + token content. Same
bytes -> same layout -> same normalized text -> same collapse.
"""
from __future__ import annotations

import re

_NUM = re.compile(r"^(?:[1-9]|1\d|2[0-5])$")          # a transcript line number
_NUMERICISH = re.compile(r"^[\d.,:/$#()+-]+$")         # dates, amounts, codes


def _bands(words, page_width, bins=160):
    """Candidate x-bands separated by coverage valleys. Returns [(lo, hi), ...]."""
    if not words:
        return [(0.0, page_width)]
    bw = page_width / bins
    cov = [0] * bins
    for w in words:
        for i in range(max(0, int(w["x0"] / bw)),
                       min(bins - 1, int(w["x1"] / bw)) + 1):
            cov[i] += 1
    lo = next((i for i, c in enumerate(cov) if c), 0)
    hi = next((i for i in range(bins - 1, -1, -1) if cov[i]), bins - 1)
    covered = sorted(c for c in cov[lo:hi + 1] if c)
    med = covered[len(covered) // 2] if covered else 0
    gap = max(1, med * 0.15)             # valley = <15% of typical column density
    out, start, run = [], lo, 0
    for i in range(lo, hi + 1):
        if cov[i] <= gap:
            run += 1
        else:
            if run >= 2 and (i - run) > start:
                out.append((start * bw, (i - run) * bw))
                start = i
            run = 0
    out.append((start * bw, (hi + 1) * bw))
    return out


def _words_in(words, lo, hi):
    return [w for w in words if lo <= (w["x0"] + w["x1"]) / 2 < hi]


def _is_gutter(band_words):
    """A band whose tokens are ~all bare line-numbers is a line-number gutter."""
    toks = [w["text"] for w in band_words]
    if not toks:
        return True
    return sum(1 for t in toks if _NUM.match(t)) / len(toks) >= 0.8


def _column_text(band_words):
    lines: dict[int, list] = {}
    for w in band_words:
        lines.setdefault(round(w["top"] / 3.0), []).append(w)
    out = []
    for k in sorted(lines):
        row = sorted(lines[k], key=lambda w: w["x0"])
        if row and _NUM.match(row[0]["text"]):     # strip a leading line number
            row = row[1:]
        t = " ".join(w["text"] for w in row).strip()
        if t:
            out.append(t)
    return out


def normalize_page(page) -> tuple[str, dict]:
    """Return (reading-order text, layout_info) for a pdfplumber page.

    layout_info: {columns:int, tabular:bool, numeric_frac:float, ...}. Falls back
    to page.extract_text() on any failure, so it is always safe to substitute for
    a bare extract_text() call.
    """
    try:
        words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    except Exception:
        return (page.extract_text() or ""), {"columns": 1, "tabular": False,
                                              "fallback": "extract_words_failed"}
    if not words:
        return "", {"columns": 0, "tabular": False}

    numeric_frac = sum(1 for w in words if _NUMERICISH.match(w["text"])) / len(words)
    bands = _bands(words, page.width)

    kept = [(b, len(_words_in(words, *b))) for b in bands
            if not _is_gutter(_words_in(words, *b))]
    if kept:
        max_mass = max(m for _, m in kept)
        cols = [b for b, m in kept if m >= 0.2 * max_mass]
    else:
        cols = list(bands)

    # Tabular → natural row order (extract_text). Layout-general signal.
    if numeric_frac >= 0.45 or len(cols) >= 4:
        return (page.extract_text() or ""), {
            "columns": 1, "tabular": True,
            "real_cols": len(cols), "numeric_frac": round(numeric_frac, 3),
        }

    lines = []
    for (lo, hi) in cols:
        lines.extend(_column_text(_words_in(words, lo, hi)))
    return "\n".join(lines), {"columns": len(cols), "tabular": False,
                              "numeric_frac": round(numeric_frac, 3)}
