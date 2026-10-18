"""Split a flight-manifest surface into individual passenger tokens.

A manifest fragment like "AA, JD, XY, ED TURNER" is not one person — it's the
passenger list of a flight. This splits it into ["AA", "JD", "XY", "ED TURNER"]
so each token can be resolved (via the governed alias equation) to a canonical
person or parked in the LDU. No hardcoded cast — resolution is the equation's job.
"""

from __future__ import annotations

import re

# Non-name manifest tokens: counts ("1 FEMALE"), roles, unnamed placeholders.
_NOISE = re.compile(
    r"^\s*\d*\s*(?:fe)?male\b|\bchild\b|\binfant\b|\bguest\b|\bunknown\b|"
    r"\bpilot\b|\bcrew\b|\bstaff\b|\bsafety\b|\bpassenger[s]?\b",
    re.IGNORECASE,
)
_LEAD_SEQ = re.compile(r"^\s*\d{2,}\s+")          # leading flight sequence number "1518 "
_INITIAL = re.compile(r"^[A-Z]{1,3}\.?$")          # AA, JD, XY, A.P.
# A surface that is itself a manifest list (comma-separated, or ≥2 bare-initial
# tokens) — used to stop a passenger token collapsing into another junk fragment.
_MANIFEST_SHAPE = re.compile(r",|^\s*\d{3,}\s|\b[A-Z]{1,3}\.?\s+[A-Z]{1,3}\.?\b")


def looks_like_manifest(name: str) -> bool:
    """True if `name` is itself a manifest list / passenger-row fragment (so it must
    never be a resolution TARGET — a token collapses to a real person, not a twin)."""
    return bool(name) and bool(_MANIFEST_SHAPE.search(name))


def split_passenger_tokens(surface: str) -> list[str]:
    """Return the passenger surfaces in a manifest fragment. Comma/slash/period
    separated; a part that is a run of bare initials ("AA JD XY") is split into each
    initial; a name part ("ED TURNER", "SMITHSON") is kept whole. Count/role dropped."""
    if not surface:
        return []
    out: list[str] = []
    # split on comma / slash / a period that follows an initials token ("JD. XY.")
    parts = re.split(r"\s*[,/]\s*|(?<=\b[A-Z]{2})\.\s+", surface)
    for part in parts:
        part = _LEAD_SEQ.sub("", (part or "").strip()).strip(" .")
        if not part or _NOISE.match(part):
            continue
        toks = part.split()
        if len(toks) > 1 and all(_INITIAL.match(t) for t in toks):
            out.extend(t.rstrip(".") for t in toks)   # "AA JD XY" → AA, JD, XY
        else:
            out.append(part)                          # "ED TURNER" / "SMITHSON" kept whole
    # de-dupe preserving order, drop any residual noise
    seen, result = set(), []
    for t in out:
        k = t.lower()
        if k in seen or _NOISE.match(t):
            continue
        seen.add(k)
        result.append(t)
    return result
