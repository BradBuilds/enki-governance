"""The one completeness computation. Pure — no database, no envelope mutation.

WHY THIS MODULE EXISTS
----------------------
Completeness had two governed scorers. Both read `completenessvocab` through GDU,
both honoured `default_spec`, and both looked correct in isolation:

    core/gdu/completeness.py        DB-oriented; abstains; fail-closed ladder;
                                    stamps completeness_regime (migration 141)
    core/extraction/completeness.py envelope-oriented; pure; emits gaps[];
                                    5 callers

They were assumed equivalent. Measured across all 559 reachable fill states of the
18 specs in slot_specs@0.1.1, they disagree on **score in 70.3%** of them and on
**tier in 11.1%** — because the optional-slot bonus is a different formula in each:

    gdu         min(filled_opt * 0.05, 0.15)
    extraction  min(0.25, filled_opt * 0.5 / len(required))

They coincide only when no optional slot is filled or both clip to 1.0, and every
real spec declares 2-6 optional slots. Worked examples:

    entity.vessel     2/3 req + 2 opt  ->  0.77 solid   vs  0.917 verified
    assessment.fact   5/7 req + 3 opt  ->  0.86 solid   vs  0.929 verified
    entity.person     1/4 req + 2 opt  ->  0.35 stub    vs  0.500 partial

`verified` is the tier the pack itself makes "eligible for epistemic_type promotion
to 'fact'", so the divergence reached the fact layer.

WHICH SEMANTICS WON, AND WHY
----------------------------
This module carries `core/gdu/completeness.py`'s decision rules, because on the
three points where the two differed, only one side was defensible under the
project's own principles:

  * ABSTENTION. gdu returns None when nothing declares what a subtype should
    contain. extraction always returned a tier. A gap is not a negative.
  * FAIL-CLOSED. gdu raises if the ladder will not load or a score matches no
    band. extraction initialised `candidate = "stub"` and returned it silently —
    and there is one reachable hole: document.article at 0.692 falls between
    partial (<=0.69) and solid (>=0.7), so a 9/13-complete article read `stub`.
  * NO HARDCODED KNOWLEDGE. extraction's last-resort fallback was a slot list
    written in Python: {"required_slots": ["identity.title_display"]}. The pack
    declares default_spec for exactly this case; read it, do not restate it.

It carries extraction's OUTPUT SHAPE — filled/total/gaps[] — because that is most
of plan Phase 3 and the reason the merge could not be deferred: Phase 3 adds a
`blocked` slot state, and a state that exists in two scorers gets built twice and
diverges a third time.

ONE DELIBERATE BEHAVIOUR CHANGE
-------------------------------
Slot-filled semantics are taken from extraction, not gdu. gdu tested
`val != "" and val != [] and val != {}`, which counts a whitespace-only string
("   ") as filled. extraction's `_is_filled` strips first. Explicit beats
incidental, and "a slot containing only spaces is filled" was never a decision
anyone made. Both agree that False and 0 ARE filled — an explicit boolean and a
zero measurement are answers, not gaps.

WHAT THIS MODULE MUST NOT GROW
------------------------------
No database access, no envelope mutation, no regime stamping. Those belong to the
adapters (core/gdu/completeness.py and core/extraction/completeness.py). Keeping
this pure is what lets the same numbers be produced at admit time, at reconcile
time, and in a report script without three code paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class Completeness:
    """One computed completeness verdict.

    `gaps` names the unfilled REQUIRED slots, by spec path. It is the Phase 3
    surface: the queue entry "this vessel has an IMO but no route" is a gap plus
    the slot's declared target, not a hand-written research task.
    """
    score: float
    tier: str
    filled_slots: int
    total_slots: int
    gaps: list[str] = field(default_factory=list)
    optional_filled: int = 0
    optional_total: int = 0
    used_default: bool = False
    spec_used: str = "default"
    #: Required slots we never got to ask about, because the source that would
    #: answer them refused (Cloudflare, auth wall, rate limit, dead endpoint).
    #: Kept SEPARATE from `gaps` on purpose — a gap is a question with a known
    #: next source, and queueing research against a source that just refused is
    #: how a work queue fills with work it cannot do. These slots are also
    #: EXCLUDED FROM `total_slots`; see compute().
    blocked: list[str] = field(default_factory=list)


def is_filled(value: Any) -> bool:
    """Does this value answer the slot?

    Explicit per type rather than a chain of inequality tests, so the edge cases
    are decisions rather than side effects:
      - False is FILLED. An explicit boolean is an answer.
      - 0 and 0.0 are FILLED. Zero is a valid measurement.
      - "" and "   " are EMPTY. Whitespace is not content.
      - [] and {} are EMPTY.
      - None is EMPTY.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) > 0
    return True


def read_slot(data: dict, slot: str) -> Any:
    """Read a dotted slot path out of a data dict.

    Two shapes reach this function and both are legitimate: the DB adapter builds
    a FLAT dict keyed by the full dotted path ("identity.title_display"), while
    the envelope adapter passes NESTED dicts. Try flat first (it is unambiguous),
    then walk. This is why the two adapters can share one core without either
    having to reshape its data first.
    """
    if slot in data:
        return data[slot]
    cur: Any = data
    for seg in slot.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            return None
    return cur


def score_to_tier(score: float, corroborations: int = 0,
                  contradictions: int = 0, *, tiers: Optional[dict] = None) -> str:
    """Map a score onto the governed ladder.

    Thresholds live in completenessvocab.tiers.json, whose change control reads
    "Adding a new tier requires full ceremony". Restating them as Python constants
    would let a ceremony pass while the code kept the old ladder, so they are read
    from the pack every time.

    Raises rather than falling back. A governed score computed against a ladder we
    could not load is not a governed score, and a silent default is precisely how
    two scorers diverge without anyone noticing.
    """
    if tiers is None:
        from core.gdu.pack_registry import get_completeness_tiers
        tiers = get_completeness_tiers()
    if not tiers:
        raise RuntimeError(
            "completenessvocab tier ladder unavailable — cannot assign a "
            "completeness tier. Check libraries/completenessvocab/definitions/"
            "completenessvocab.tiers.json is present and in the active set."
        )

    # Highest min_score the value clears. Sorted by name so two nodes with the
    # same score cannot land in different tiers on different nodes — the ladder
    # is a dict, and dict order is not a governed input.
    best, best_min = None, None
    for name, spec in sorted(tiers.items()):
        lo = spec.get("min_score")
        if lo is None or score < lo:
            continue
        if best_min is None or lo > best_min:
            best, best_min = name, lo
    if best is None:
        raise RuntimeError(
            f"score {score} matched no tier in the governed ladder "
            f"({sorted(tiers)}) — the ladder does not cover the score range."
        )

    # A tier may declare extra_requirements the score alone cannot satisfy.
    # `verified` does: corroborations_min 1, contradictions_max 0, demotes_to
    # solid. Applied generically via demotes_to rather than special-casing the
    # name, so a pack that adds requirements to another tier needs no code change.
    seen: set[str] = set()
    while best and best not in seen:
        seen.add(best)
        extra = (tiers.get(best) or {}).get("extra_requirements") or {}
        if not extra:
            break
        ok = (corroborations >= extra.get("corroborations_min", 0)
              and contradictions <= extra.get("contradictions_max", 10**9))
        if ok:
            break
        demoted = (tiers.get(best) or {}).get("demotes_to")
        if not demoted:
            break
        best = demoted
    return best


def _as_int(*vals) -> int:
    """First value that reads as an int, else 0. Absent evidence counts as none."""
    for v in vals:
        if v is None:
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return 0


def compute(node_type: str, subtype: str, data: dict, *,
            specs: Optional[dict] = None,
            tiers: Optional[dict] = None,
            blocked_slots: Optional[set] = None) -> Optional[Completeness]:
    """Score one thing against its governed slot spec.

    Returns None when completeness is UNDEFINED for this (node_type, subtype) —
    no subtype spec, no usable default_spec, or a spec declaring no required
    slots. None is not zero. The caller records the abstention (NULL score, NULL
    tier, an 'unscoreable:no_spec' regime); it must not substitute a number,
    because a number here asserts completeness against a standard that does not
    exist.

    `specs` / `tiers` are injectable so a report script can score against a
    candidate pack version without loading it into the runtime. Default is the
    live governed packs.
    """
    if specs is None:
        from core.gdu.pack_registry import get_slot_specs
        specs = get_slot_specs()

    all_specs = specs.get("specs", {}) or {}
    spec_key = f"{node_type}.{subtype}"
    spec = all_specs.get(spec_key)

    used_default = False
    if not spec:
        # The pack ALREADY says what to do here: slot_specs declares a
        # default_spec ("Used when a (node_type, subtype) is not in the specs
        # map. Conservative — requires only identity and source").
        #
        # Reading it is not a code decision. Abstaining WOULD be one: it would
        # override a governed declaration with a Python judgement, which is the
        # exact failure this consolidation exists to remove. The gap stays
        # visible through `used_default`, which the DB adapter turns into a
        # 'default_spec/...' regime stamp so Phase 1 can find the subtypes worth
        # writing a real spec for.
        spec = all_specs.get("__default__") or specs.get("default_spec") or {}
        used_default = True
        if not spec:
            return None

    required = list(spec.get("required_slots") or [])
    optional = list(spec.get("optional_slots") or [])

    # ── Phase 3: withdraw blocked slots from the measurement ────────────────
    # A slot is `blocked` when the source that would answer it REFUSED — a WAF
    # block, an auth wall, a dead endpoint. Migration 140 already records this
    # per RUN; here it lands per SLOT, which is the level that changes the
    # number.
    #
    # Blocked slots leave the DENOMINATOR entirely. Any other treatment is a
    # claim we cannot support: counting one as missing asserts the fact is
    # absent, counting it as filled asserts we know it. Removing it says the only
    # true thing — this measurement covers eight slots, not nine. Without this a
    # vessel with no route data scores identically whether routes do not exist or
    # the route source was Cloudflared, and the score lands in the flattering
    # direction either way.
    #
    # Only REQUIRED slots can be blocked. An optional slot contributes a bonus
    # when present and nothing when absent, so "we could not ask" and "it is not
    # there" already score the same and there is nothing to withdraw.
    blocked = sorted(s for s in (blocked_slots or ()) if s in required)
    if blocked:
        bset = set(blocked)
        required = [s for s in required if s not in bset]

    if not required:
        # Two ways to arrive here, one answer.
        #
        # A spec declaring no required slots defines no standard to measure
        # against. There is nothing here to compute, so say so rather than
        # average a guess — 0.5/"partial" was a made-up midpoint.
        #
        # EVERY required slot blocked lands here too, and abstention is the only
        # honest answer: the denominator is zero. 1.0 would say "nothing is
        # missing" and 0.0 would say "nothing is known" — both are inventions
        # about a thing we were never able to ask about. A node whose every
        # source refused is not complete and not empty; it is unmeasured.
        return None

    gaps: list[str] = []
    filled_required = 0
    for slot in required:
        if is_filled(read_slot(data, slot)):
            filled_required += 1
        else:
            gaps.append(slot)

    filled_optional = sum(1 for slot in optional
                          if is_filled(read_slot(data, slot)))

    total_required = len(required)
    base_score = filled_required / total_required
    optional_bonus = min(filled_optional * 0.05, 0.15)
    score = round(min(base_score + optional_bonus, 1.0), 2)

    corr = _as_int(data.get("evidence.corroborations"),
                   data.get("corroboration_count"),
                   read_slot(data, "evidence.corroborations"))
    contr = _as_int(data.get("evidence.contradictions"),
                    data.get("contradiction_count"),
                    read_slot(data, "evidence.contradictions"))

    tier = score_to_tier(score, corr, contr, tiers=tiers)

    # ── An unfinished measurement may not certify completeness ─────────────
    # Withdrawing blocked slots from the denominator makes `blocked` different
    # from `empty`, which is the point of the phase — but it moves the score
    # UPWARD, because a smaller denominator with the same numerator is a higher
    # fraction. Measured on a real node: 3/4 = 0.75 became 3/3 = 1.00 the moment
    # one slot was blocked. Left there, "the registry was down" would read as
    # BETTER KNOWN than a node whose data genuinely exists, which inverts the
    # rule this project keeps landing on: the honest number is the worse one.
    #
    # The score is still right — it answers "of what we could reach, how much is
    # there". The TIER is what over-claims, because the top tier is a
    # certification: the pack makes `verified` the tier eligible for promotion to
    # `fact`. A node we could not finish measuring must not be promotable on the
    # strength of the part we happened to see.
    #
    # So: blocked slots cap the ladder one rung below the top. Implemented with
    # the pack's own `demotes_to` edge rather than by naming `verified` here, so
    # a ceremony that renames or re-ranks the tiers moves this with it.
    if blocked and tier and tiers:
        top, top_min = None, None
        for name, spec in sorted(tiers.items()):
            lo = spec.get("min_score")
            if lo is None:
                continue
            if top_min is None or lo > top_min:
                top, top_min = name, lo
        if tier == top:
            demoted = (tiers.get(tier) or {}).get("demotes_to")
            if demoted:
                tier = demoted

    return Completeness(
        score=score,
        tier=tier,
        filled_slots=filled_required,
        total_slots=total_required,
        gaps=gaps,
        optional_filled=filled_optional,
        optional_total=len(optional),
        used_default=used_default,
        spec_used="default" if used_default else spec_key,
        blocked=blocked,
    )
