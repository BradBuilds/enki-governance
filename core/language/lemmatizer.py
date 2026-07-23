"""English lemmatizer — reduce an inflected surface form to candidate base lemmas.

Two halves, mirroring WordNet's morphy:
  - DETACHMENT RULES (here, in code): the universal, deterministic English suffix
    logic (walking→walk, parties→party). Small and language-general, not knowledge
    about any entity, so it lives in code.
  - EXCEPTIONS (the pack): irregulars the rules can't derive (went→go, mice→mouse,
    better→good), sourced from WordNet via scripts/build_morphology_pack.py into
    libraries/language/definitions/language.exceptions.json.

`lemmas()` returns CANDIDATE base forms — it over-generates safely (e.g. "uses"→
{"use","us"}); the caller validates against the lexicon (only forms that actually
bind to a sense contribute candidates), so junk never resolves. This is the
morphology layer the sense resolver consults so an inflected form ("using") is
disambiguated against its real lemma's senses ("use"), not just the rare
nominalization the inflected surface happens to bind to.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_WS = re.compile(r"\s+")

# Universal English detachment rules (suffix → replacement), per POS. Mirrors
# wn.morphy.DETACHMENT_RULES (the System.ALL set plus the common -ves/-f variants).
_RULES: dict[str, list[tuple[str, str]]] = {
    "noun": [("s", ""), ("ses", "s"), ("xes", "x"), ("zes", "z"), ("ches", "ch"),
             ("shes", "sh"), ("men", "man"), ("ies", "y"), ("ves", "f"), ("ves", "fe")],
    "verb": [("s", ""), ("ies", "y"), ("es", "e"), ("es", ""), ("ed", "e"),
             ("ed", ""), ("ing", "e"), ("ing", "")],
    "adjective": [("er", ""), ("est", ""), ("er", "e"), ("est", "e")],
    "adverb": [],
}
_ALL_POS = ("noun", "verb", "adjective", "adverb")

_EXC: dict[str, dict[str, list[str]]] | None = None
_PACK_REL = "libraries/language/definitions/language.exceptions.json"
# repo root is three parents up from this file (core/language/lemmatizer.py)
_PACK_CANDIDATES = [Path(_PACK_REL),
                    Path(__file__).resolve().parents[2] / _PACK_REL]


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").lower()).strip()


def _exceptions() -> dict[str, dict[str, list[str]]]:
    global _EXC
    if _EXC is None:
        _EXC = {}  # fail-open: rules still work without the irregulars pack
        for p in _PACK_CANDIDATES:
            try:
                _EXC = json.loads(p.read_text())["content"]["exceptions"]
                break
            except Exception:
                continue
    return _EXC


def lemmas(word: str, pos: str | None = None) -> set[str]:
    """Candidate base lemmas for an inflected surface form. `pos` (noun/verb/
    adjective/adverb) narrows the rules; None tries all. Over-generates; the caller
    validates candidates against the lexicon. Never includes the input itself."""
    w = _norm(word)
    if not w or " " in w:           # multi-word units aren't lemmatized here
        return set()
    exc = _exceptions()
    out: set[str] = set()
    for p in ([pos] if pos else _ALL_POS):
        for b in exc.get(p, {}).get(w, []):
            out.add(b)
        for suf, rep in _RULES.get(p, []):
            if w.endswith(suf) and len(w) - len(suf) >= 2:
                out.add(w[: len(w) - len(suf)] + rep)
    out.discard(w)
    return out
