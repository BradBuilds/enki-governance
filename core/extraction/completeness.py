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
from typing import Optional

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
            # NO HARDCODED FALLBACK. This branch used to install
            # {"required_slots": ["identity.title_display"]} — a slot list written
            # in Python, which is domain knowledge the packs own and the project
            # rule forbids. Worse, it scored silently: a node measured against an
            # invented one-slot standard looked complete.
            #
            # Leaving these empty makes the core abstain (no spec, no default ->
            # None), which the adapters record as UNDEFINED. A scorer that cannot
            # load its governance does not get to guess.
            log.error("scorer cannot load completenessvocab: %s — "
                      "completeness will be UNDEFINED until the pack loads", e2)
            _TIERS = {}
            _SPECS = {}
            _DEFAULT_SPEC = {}


def score_envelope(envelope: dict, blocked_slots: Optional[set] = None) -> dict:
    """Compute the completeness sub-object for one envelope. Envelope adapter.

    Mutates envelope.completeness in place and returns it, unchanged in shape
    from before the merge — the 5 callers keep working without edits.

    WHAT MOVED. This function used to carry its own copy of the arithmetic, and
    that copy disagreed with the DB-side scorer: a different optional-slot bonus
    formula (`min(0.25, filled_opt * 0.5/len(required))` against
    `min(filled_opt * 0.05, 0.15)`), a different rounding (3 places against 2),
    a tier lookup that walked the ladder in dict order and silently returned
    "stub" for a score inside no band, and a `verified` check special-cased by
    name instead of following `demotes_to`. Measured across every reachable fill
    state of the governed specs, the two disagreed on score 70.3% of the time and
    on tier 8.8-11.6% (see `scripts/verify_completeness_core.py`).

    Scores from this path therefore MOVE — mostly downward, because the old
    optional bonus was up to 0.25 against the governed 0.15. That is the intended
    outcome, the same shape as Phase 1: the old number flattered.

    ABSTENTION IS NOW POSSIBLE HERE. The core returns None when nothing declares
    what a subtype should contain, and this function now writes NULL score and
    NULL tier rather than manufacturing a number. `validate_shape` requires the
    keys to exist, not to be non-null, so a null-scored envelope is still valid —
    and a null tier is what the downstream reader already handles.
    """
    _load_pack()

    from core.gdu.completeness_core import compute

    node_type = envelope.get("node_type")
    subtype = envelope.get("subtype")

    # The core reads nested dicts directly, so the envelope is passed as-is —
    # no projection needed on this side. `specs`/`tiers` are handed in from this
    # module's GDU-loaded cache rather than re-read, so the envelope path and the
    # DB path agree even if one of them is running against a pinned pack.
    # Phase 3: the caller may know that a source refused for specific slots.
    # Optional here rather than read from `slot_blocks` as the DB adapter does,
    # because an envelope is scored BEFORE it is a node — there is no node_id to
    # look blocks up by yet. The extractor that was refused is the only thing
    # that knows, so it has to say.
    result = compute(
        node_type or "", subtype or "", envelope,
        specs={"specs": _SPECS or {}, "default_spec": _DEFAULT_SPEC or {}},
        tiers=_TIERS or None,
        blocked_slots=blocked_slots,
    )

    completeness = envelope.setdefault("completeness", {})

    if result is None:
        # Undefined, not zero. Nothing governs this subtype, so there is no
        # standard to measure against and no honest number to write.
        completeness["score"] = None
        completeness["tier"] = None
        completeness["filled_slots"] = 0
        completeness["total_slots"] = 0
        completeness["gaps"] = []
        completeness["blocked"] = []
        completeness["optional_filled"] = 0
        completeness["optional_total"] = 0
        completeness["spec_used"] = "unscoreable:no_spec"
        return completeness

    completeness["score"] = result.score
    completeness["tier"] = result.tier
    completeness["filled_slots"] = result.filled_slots
    completeness["total_slots"] = result.total_slots
    completeness["gaps"] = result.gaps
    # Reported SEPARATELY from gaps, never merged into them: a blocked slot is a
    # question we could not ask, and the card that shows "3 gaps" must not turn
    # "the registry was down" into "the fact does not exist".
    completeness["blocked"] = result.blocked
    # Diagnostic — useful for the card renderer to show "+X bonus"
    completeness["optional_filled"] = result.optional_filled
    completeness["optional_total"] = result.optional_total
    completeness["spec_used"] = result.spec_used

    return completeness
