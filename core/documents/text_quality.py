"""Sentence-structure validator — the gate that decides whether a chunk of text is
real enough to enter the meaning graph (sense-collapse, statements), or is garbage
(bad OCR / corrupted PDF text layers) that must stay raw, searchable evidence only.

Source-agnostic and DETERMINISTIC — it judges the TEXT, using the loaded lexicon as
a "real English word" oracle, not the document's provenance and not an LLM. Three
signals (all must pass for `is_real_sentence`):

  dict_ratio   fraction of distinct content tokens that are known lexicon forms
               (lemmatized). OCR garbage ("ccaassee","ddooccuummeenntt") → ~0.
  has_predicate at least one token resolves to a real verb — kills fragments/headers.
  structure_ok terminal punctuation present, not dominated by doubled-character OCR
               runs, sane alphabetic ratio — kills "CCaassee…" and tabular junk.

Reversible/auditable by design: callers record the verdict like a collapse decision.
Thresholds are arguments so a policy pack can own them later.
"""
from __future__ import annotations

import json
import re

from core.language.lemmatizer import lemmas

_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*")
_SENT_END = re.compile(r"[.!?]")

_FUNC_WORDS: set | None = None
_FUNC_PACK = "/app/libraries/function_words/definitions/function_words.senses.json"


def _function_words() -> set:
    """Closed-class English (the/a/was/is/and/of/to/in/…). Real prose is rich in
    these; data rows / flight logs / tabular records have ~none — the cleanest signal
    that a chunk is a SENTENCE, not a record."""
    global _FUNC_WORDS
    if _FUNC_WORDS is None:
        try:
            p = json.load(open(_FUNC_PACK))
            _FUNC_WORDS = {s["lemma"].lower() for s in p["content"]["senses"]}
        except Exception:
            _FUNC_WORDS = set()
    return _FUNC_WORDS


def _doubled_letter_ratio(s: str) -> float:
    """Fraction of adjacent character pairs that are the same letter — the OCR
    doubling signature ('CCaassee' ≈ 0.5; normal English < 0.05)."""
    pairs = [(s[i], s[i + 1]) for i in range(len(s) - 1)]
    if not pairs:
        return 0.0
    dbl = sum(1 for a, b in pairs if a == b and a.isalpha())
    return dbl / len(pairs)


def _alpha_ratio(s: str) -> float:
    non_space = [c for c in s if not c.isspace()]
    if not non_space:
        return 0.0
    return sum(1 for c in non_space if c.isalpha()) / len(non_space)


def _structure_ok(raw: str, toks: list[str]) -> tuple[bool, str]:
    if _doubled_letter_ratio(raw) > 0.20:
        return False, "ocr_doubling"
    if _alpha_ratio(raw) < 0.55:
        return False, "low_alpha_ratio"
    avg_len = sum(len(t) for t in toks) / len(toks) if toks else 0
    if avg_len > 14:                       # OCR doubling / run-ons inflate token length
        return False, "abnormal_word_length"
    # NOTE: terminal punctuation is NOT required — line-numbered, wrapped transcript
    # chunks ("23 a row ... 24 would") legitimately lack it. dict_ratio + has_predicate
    # are the real gate; structure only rejects strong garbage signatures.
    return True, "ok"


def chunk_quality(cur, text: str, *, min_dict_ratio: float = 0.5,
                  min_func_ratio: float = 0.12, min_tokens: int = 4,
                  tenant: str = "default") -> dict:
    """Validate one chunk. Returns a verdict dict with the signals + reason.
    `cur` is a live DB cursor (for lexicon membership against the loaded grid)."""
    raw = text or ""
    toks = [w.lower() for w in _WORD.findall(raw)]
    distinct = sorted({t for t in toks if len(t) >= 2})

    def verdict(is_real, dr, hp, so, reason, fr=0.0):
        return {"is_real_sentence": is_real, "dict_ratio": round(dr, 3),
                "func_ratio": round(fr, 3), "has_predicate": hp,
                "structure_ok": so, "reason": reason, "tokens": len(toks)}

    if len(toks) < min_tokens:
        return verdict(False, 0.0, False, False, f"too_short({len(toks)})")

    # function-word ratio — sentences flow through closed-class words; records don't
    fw = _function_words()
    func_ratio = sum(1 for t in toks if t in fw) / len(toks)

    # dict_ratio (lemmatized): a token counts as known if it OR a lemma is a lexical form
    cands = set(distinct)
    for t in distinct:
        cands.update(lemmas(t))
    cur.execute("""SELECT surface_form_normalized FROM form_nodes
                    WHERE surface_form_normalized = ANY(%s) AND tenant_id = %s""",
                (list(cands), tenant))
    known_forms = {r[0] for r in cur.fetchall()}

    def known(t):
        return t in known_forms or any(l in known_forms for l in lemmas(t))

    n_known = sum(1 for t in distinct if known(t))
    dict_ratio = n_known / len(distinct) if distinct else 0.0

    # has_predicate: any token/lemma bound to a verb sense in the loaded lexicon
    cur.execute("""SELECT 1 FROM form_nodes f
                     JOIN form_concept_bindings b ON b.form_id = f.form_id
                     JOIN concept_nodes c ON c.concept_id = b.concept_id
                    WHERE f.surface_form_normalized = ANY(%s) AND f.tenant_id = %s
                      AND c.subtype_path LIKE 'lexical.%%' AND c.canonical_subtype = 'verb'
                    LIMIT 1""", (list(cands), tenant))
    has_pred = cur.fetchone() is not None

    structure_ok, _sr = _structure_ok(raw, toks)
    sreason = _sr
    is_real = (dict_ratio >= min_dict_ratio and func_ratio >= min_func_ratio
               and has_pred and structure_ok)
    if is_real:
        reason = "ok"
    elif dict_ratio < min_dict_ratio:
        reason = f"low_dict_ratio({dict_ratio:.2f})"
    elif func_ratio < min_func_ratio:
        reason = f"low_func_ratio({func_ratio:.2f})"   # tabular record, not a sentence
    elif not has_pred:
        reason = "no_predicate"
    else:
        reason = sreason
    return verdict(is_real, dict_ratio, has_pred, structure_ok, reason, func_ratio)


_CAPS_BOUNDARY = re.compile(r'[.!?:;]["\')\]]?\s+|\n+')


def caps_ratio(raw: str) -> float:
    """Fraction of alphabetic tokens capitalised where prose would not capitalise:
    ALL-CAPS anywhere, or Title-Case away from a sentence start.

    Language-general, not domain knowledge — prose flows in lower case, while
    headers, captions and case captions are set in caps or title case.
    `lexical_tokens` already applies exactly this test per TOKEN ("ALL-CAPS label →
    skip"); this aggregates it to a span verdict.

    Measured separation on a legal-deposition corpus: real prose 0.00-0.33 (an
    all-caps speaker label followed by prose, "<NAME>: I know they never met.", is
    the 0.33 — correctly kept because the rest of the span is a sentence); document
    furniture 0.50-1.00 (a court caption line measures 1.00).
    """
    starts = set()
    starts.add(0)
    for m in _CAPS_BOUNDARY.finditer(raw or ""):
        starts.add(m.end())
    toks = [(m.group(0), m.start()) for m in _WORD.finditer(raw or "")
            if len(m.group(0)) > 1]
    if not toks:
        return 0.0
    n = sum(1 for t, pos in toks
            if t.isupper() or (t[0].isupper() and pos not in starts))
    return n / len(toks)


def span_quality(cur, span: str, *, max_caps_ratio: float = 0.60,
                 tenant: str = "default") -> dict:
    """Grade a STATEMENT'S SOURCE SPAN — stricter than the paragraph gate.

    The paragraph gate already works: every statement in the corpus comes from a
    `prose`, non-boilerplate paragraph that passed `chunk_quality`. The furniture
    that reaches the fact layer is not the paragraph — it is the SENTENCE that
    `_canonical_span()` carved out of it: page stamps, docket headers, captions and
    splitter fragments embedded inside otherwise-good prose.

    So the span is graded on the paragraph signals PLUS capitalisation, which is
    what distinguishes a caption from a claim. Deliberately a separate function:
    `chunk_quality`/`is_real_sentence` gates the sense layer and is covered by
    `validate_language_layer.py`'s 25 assertions — this must not change that
    behaviour.

    Returns the chunk verdict extended with `caps_ratio` and `span_ok`.
    """
    v = chunk_quality(cur, span, tenant=tenant)
    cr = caps_ratio(span)
    v["caps_ratio"] = round(cr, 3)
    v["span_ok"] = bool(v.get("is_real_sentence")) and cr < max_caps_ratio
    if not v["span_ok"] and v.get("is_real_sentence") and cr >= max_caps_ratio:
        v["reason"] = f"header_or_caption(caps={cr:.2f})"
    return v


def band(v: dict) -> str:
    """Route a verdict into one of four lanes — the human stays out of the
    confident ones:
      pass    real sentence            → into the meaning graph (auto)
      record  tabular / low-func-words → to the record extractor (auto, not a sentence)
      drop    garbage / no real words  → raw evidence only (auto)
      flag    real content, incomplete → human review queue (the non-deterministic ~few %)"""
    if v.get("is_real_sentence"):
        return "pass"
    if v.get("dict_ratio", 0) < 0.2 or not v.get("structure_ok"):
        return "drop"
    if v.get("func_ratio", 0) < 0.08:
        return "record"
    return "flag"


def score(v: dict) -> float:
    """Continuous 0..1 sentence-likeness — for sorting the review queue (lowest =
    most suspect). Same signals as the verdict, just collapsed to a number."""
    s = (0.45 * min(v.get("dict_ratio", 0.0), 1.0)
         + 0.35 * min(v.get("func_ratio", 0.0) / 0.15, 1.0)
         + 0.20 * (1.0 if v.get("has_predicate") else 0.0))
    return round(s if v.get("structure_ok") else s * 0.4, 3)
