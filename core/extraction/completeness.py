"""
Completeness scorer — Phase 8.

Computes completeness.score, completeness.tier, filled_slots, total_slots,
and gaps[] on every envelope, using the slot specs and tier thresholds
defined in the completenessvocab pack (loaded through the GDU runtime,
not from disk directly).

Generic over node_type/subtype:
    - For each envelope, look up the spec for "{node_type}.{subtype}"
    - Fall back to default_spec if no specific entry exists
    - Score = filled_required / total_required, with optional slots
      adding a small bonus (capped at 1.0)
    - Tier = first tier whose [min_score, max_score] contains the score
    - Verified tier has extra_requirements (corroborations >=1,
      contradictions == 0) — failing them demotes to solid

The scorer is pure: it reads envelope content, mutates only the
envelope.completeness sub-object, and never touches the database.
The runner records a 'scoring' pass to extraction_passes after.

The scorer NEVER hardcodes slot lists — it reads them through GDU at
import time so any pack update changes scoring without code changes.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("enki.extraction.completeness")

# Lazy-loaded GDU pack content (one-time at first use, then cached)
_TIERS: Optional[dict] = None
_SPECS: Optional[dict] = None
_DEFAULT_SPEC: Optional[dict] = None


def _load_pack() -> None:
    """Load completenessvocab through the GDU runtime once. Falls back
    to direct filesystem read if GDU is degraded so the scorer keeps
    working in dev mode (with a warning logged)."""
    global _TIERS, _SPECS, _DEFAULT_SPEC
    if _TIERS is not None and _SPECS is not None:
        return

    try:
        from core.gdu.bootstrap import get_gdu
        from core.gdu.config import PACK_PATHS
        gdu = get_gdu()
        result = gdu.load_pack("completenessvocab", PACK_PATHS["completenessvocab"])
        if not result.canonical:
            log.warning("completenessvocab loaded non-canonically — scoring will run anyway")
        for d in result.definitions:
            content = getattr(d, "content", None) or {}
            if "tiers" in content:
                _TIERS = content["tiers"]
            if "specs" in content:
                _SPECS = content["specs"]
                _DEFAULT_SPEC = content.get("default_spec") or {}
    except Exception as e:
        log.warning("GDU load failed for completenessvocab: %s — falling back to filesystem", e)
        try:
            import json as _json
            from pathlib import Path
            base = Path(__file__).resolve().parents[2] / "libraries" / "completenessvocab" / "definitions"
            with open(base / "completenessvocab.tiers.json") as f:
                _TIERS = _json.load(f)["content"]["tiers"]
            with open(base / "completenessvocab.slot_specs.json") as f:
                spec_doc = _json.load(f)["content"]
                _SPECS = spec_doc["specs"]
                _DEFAULT_SPEC = spec_doc.get("default_spec") or {}
        except Exception as e2:
            log.error("scorer cannot load completenessvocab: %s", e2)
            _TIERS = {}
            _SPECS = {}
            _DEFAULT_SPEC = {"required_slots": ["identity.title_display"]}


def _get_path(env: dict, path: str) -> Any:
    cur: Any = env
    for seg in path.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            return None
    return cur


def _is_filled(value: Any) -> bool:
    """Generic emptiness check. A slot counts as filled when it has
    real content. Empty strings, empty lists, empty dicts, and None
    all count as unfilled."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return len(value) > 0
    if isinstance(value, bool):
        return True  # explicit booleans count
    if isinstance(value, (int, float)):
        return True  # zero is a valid measurement
    return True


def _spec_for(node_type: Optional[str], subtype: Optional[str]) -> dict:
    if not _SPECS:
        return _DEFAULT_SPEC or {}
    if node_type and subtype:
        key = f"{node_type}.{subtype}"
        if key in _SPECS:
            return _SPECS[key]
    return _DEFAULT_SPEC or {}


def _tier_for_score(score: float, *, corroborations: int, contradictions: int) -> str:
    if not _TIERS:
        return "stub"
    candidate = "stub"
    for name, t in _TIERS.items():
        lo, hi = t.get("min_score", 0.0), t.get("max_score", 1.0)
        if lo <= score <= hi:
            candidate = name
    # Verified extra requirements
    if candidate == "verified":
        extra = (_TIERS.get("verified") or {}).get("extra_requirements") or {}
        if corroborations < extra.get("corroborations_min", 0):
            return "solid"
        if contradictions > extra.get("contradictions_max", 0):
            return "solid"
    return candidate


def score_envelope(envelope: dict) -> dict:
    """Compute the completeness sub-object for one envelope.

    Mutates envelope.completeness in place. Returns the new
    completeness dict for callers that want to log it.
    """
    _load_pack()

    node_type = envelope.get("node_type")
    subtype = envelope.get("subtype")
    spec = _spec_for(node_type, subtype)

    required = list(spec.get("required_slots") or [])
    optional = list(spec.get("optional_slots") or [])

    filled_req = 0
    gaps: list[str] = []
    for path in required:
        val = _get_path(envelope, path)
        if _is_filled(val):
            filled_req += 1
        else:
            gaps.append(path)

    filled_opt = sum(1 for p in optional if _is_filled(_get_path(envelope, p)))

    total = max(1, len(required))
    base = filled_req / total

    # Optional bonus: each optional slot worth at most 1/(2*len(required))
    # so the optional set as a whole can lift the score by up to ~0.25
    opt_bonus = 0.0
    if optional:
        per_opt = 0.5 / max(1, len(required))
        opt_bonus = min(0.25, filled_opt * per_opt)

    score = round(min(1.0, base + opt_bonus), 3)

    corr = int(_get_path(envelope, "evidence.corroborations") or 0)
    contra = int(_get_path(envelope, "evidence.contradictions") or 0)
    tier = _tier_for_score(score, corroborations=corr, contradictions=contra)

    completeness = envelope.setdefault("completeness", {})
    completeness["score"] = score
    completeness["tier"] = tier
    completeness["filled_slots"] = filled_req
    completeness["total_slots"] = total
    completeness["gaps"] = gaps
    # Diagnostic — useful for the card renderer to show "+X bonus"
    completeness["optional_filled"] = filled_opt
    completeness["optional_total"] = len(optional)
    completeness["spec_used"] = f"{node_type}.{subtype}" if node_type and subtype else "default"

    return completeness
