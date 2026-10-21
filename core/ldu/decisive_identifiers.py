"""Which identifiers settle identity — read from the governed decomposition.

Phase 2.1/2.2. Replaces `DECISIVE_IDENTIFIER_KEYS`, a 20-key `frozenset`
hardcoded in `core/ldu/admit_candidate.py`. Two things were wrong with it: it was
domain knowledge in Python, which this project's rule forbids outright, and it
was FLAT — one list for every subtype, so a committee's EIN and a person's name
were evaluated by the same rule.

DECISIVE IS A PROPERTY OF A SLOT, NOT A SEPARATE LIST
-----------------------------------------------------
The plan originally proposed a per-subtype decisive-key list declared "alongside
the slot spec". That would have been a SECOND declaration next to the one Phase 1
already built — `entity.vessel` declares `identifiers.imo` as a constitutive slot
— and one truth with two owners is the twelve-completeness-writers defect again.

So there is one declaration, the decomposition, and `decisive_slots` names the
subset of a subtype's own slots that alone settle identity. An IMO is not
decisive because it appears on a list; it is decisive because that slot alone
settles which hull this is.

WHAT `decisive` ACTUALLY CLAIMS — AND WHY MMSI IS NOT
-----------------------------------------------------
"This slot, filled, names one real-world referent, permanently." Permanence is
the load-bearing half and a flat key list cannot express it. An IMO is welded to
the hull and survives renaming, reflagging and sale. An MMSI or a call sign is
REASSIGNED on reflagging — so it identifies a registration, not a ship, and
treating it as decisive would merge two different hulls that held the same MMSI
in different decades. Those slots are declared constitutive (they are part of
what a vessel is) and deliberately NOT decisive. When Phase 5 gives resolution a
clock they become decisive *as of a time*, which is a different claim.

SPELLING, WHICH COST US 383 ENTITIES
------------------------------------
The same identifier arrives under several spellings. Measured on node-A 2026-08-09:
**383 entities carry `wikidata_qid` and neither `qid` nor `wikidata_id`** — both
of which were in the incumbent set. Their Wikidata QID was invisible to the
decisive check and they were denied Tier A for a spelling. 184 were people. The
pack now declares accepted spellings per slot (`slot_keys`), so the concept owns
its variants instead of the variants owning the concept.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("enki.ldu.decisive_identifiers")

# Subtypes are declared under "<category>.<subtype>" in the pack, but callers
# hold a bare subtype ("organization"). Probed in this order — same shape as
# completeness_core's spec lookup, so both paths agree on which spec a subtype
# resolves to.
_CATEGORY_PROBE = ("entity", "place", "document", "event", "assessment",
                   "topic", "source", "chronicle")


def _specs() -> dict:
    """The live governed slot specs, via the pack registry (STRICT loader).

    Never read off the filesystem: the STRICT loader is what refuses a pack whose
    content hash does not match the signed active set, and a rule about identity
    that can be edited in place is not governed.
    """
    from core.gdu.pack_registry import get_slot_specs
    return get_slot_specs() or {}


def _spec_for(subtype: str) -> Optional[dict]:
    if not subtype:
        return None
    all_specs = (_specs().get("specs") or {})
    if "." in subtype and subtype in all_specs:
        return all_specs[subtype]
    for cat in _CATEGORY_PROBE:
        hit = all_specs.get(f"{cat}.{subtype}")
        if hit:
            return hit
    return None


def _keys_for_slot(spec: dict, slot: str) -> set[str]:
    """Accepted `identifiers` keys for a slot. Defaults to the last path segment,
    so a slot only needs `slot_keys` when it has variant spellings."""
    sk = (spec.get("slot_keys") or {}).get(slot)
    if isinstance(sk, list) and sk:
        return {str(k) for k in sk}
    return {slot.split(".")[-1]}


def decisive_slots_for(subtype: str) -> frozenset[str]:
    """Slot paths that alone settle identity for this subtype.

    EMPTY for a subtype the pack does not speak about. That is abstention, not a
    fallback to some global set: a subtype whose decomposition nobody has written
    has no declared way to be decisively identified, and inventing one here would
    be exactly the Python-side judgement this module exists to delete.
    """
    spec = _spec_for(subtype)
    if not spec:
        return frozenset()
    return frozenset(s for s in (spec.get("decisive_slots") or [])
                     if isinstance(s, str) and not s.startswith("_"))


def decisive_keys_for(subtype: str) -> frozenset[str]:
    """The `identifiers` keys carried by this subtype's decisive slots."""
    spec = _spec_for(subtype)
    if not spec:
        return frozenset()
    out: set[str] = set()
    for slot in decisive_slots_for(subtype):
        out |= _keys_for_slot(spec, slot)
    return frozenset(out)


def unassigned_decisive_keys() -> frozenset[str]:
    """Decisive keys whose subtype has no spec yet — PARKED, not rejected.

    `icao` identifies an aircraft and 226 aircraft exist, but there is no
    `entity.aircraft` spec to declare it in. Dropping the key would silently
    weaken collapse for every one of them; declaring an aircraft decomposition
    without surveying what registries publish would be imported judgement. So it
    is parked: still returned by `all_decisive_keys()`, and listed in the pack
    where the gap is visible. Park is not reject — it leaves this set the moment
    its subtype gets a decomposition.
    """
    blob = (_specs().get("_unassigned_decisive") or {})
    return frozenset(k for k in blob if not k.startswith("_"))


def all_decisive_keys() -> frozenset[str]:
    """Every decisive key, across all subtypes, plus the parked ones.

    This is the shape the incumbent flat frozenset had, and it is what both
    current consumers use — `admit_candidate` and `workers/investigation` each
    ask only "does this thing carry any decisive identifier", without consulting
    the subtype. Keeping them on the union means THIS PHASE CHANGES NO BEHAVIOUR:
    it moves the knowledge into the pack and nothing else.

    Tightening those consumers to `decisive_keys_for(subtype)` is the real prize
    — it is what stops an organisation's EIN lifting a person to Tier A — but it
    is a separate, measurable change to who collapses onto whom, and mixing it
    into the same step would leave two variables moving at once with no way to
    read which caused what.
    """
    all_specs = (_specs().get("specs") or {})
    out: set[str] = set(unassigned_decisive_keys())
    for spec in all_specs.values():
        if not isinstance(spec, dict):
            continue
        for slot in (spec.get("decisive_slots") or []):
            if isinstance(slot, str) and not slot.startswith("_"):
                out |= _keys_for_slot(spec, slot)
    return frozenset(out)


#: Strongest tier on the ladder. Tier letters sort by strength — 'A' < 'B' <
#: ... < 'G' lexicographically IS strongest-to-weakest, which is the same
#: comparison `_accumulate_concept_signal` already relies on
#: (`WHEN %s < evidence_tier THEN %s  -- A beats B beats C`).
DECISIVE_TIER = "A"


def tier_with_decisive(current_tier, identifiers) -> str:
    """Lift a concept's tier to A when it carries a decisive identifier.

    ONE OWNER, BECAUSE TWO WRITERS ALREADY DISAGREED. This rule lived inline in
    `admit_candidate()`, so `ensure_substrate_for_entity` — which mints concepts
    on the gateway path and is handed the very same `identifiers` dict — never
    applied it and stamped its hardcoded default instead. Measured on node-A
    2026-08-12: 520 person concepts carrying `fec_id`, a declared decisive slot,
    sat at tier D, against 537 identical-keyed concepts at tier A. Same key, same
    period, different tier, because a different function wrote the row.

    NEVER DOWNGRADES, and A is the only tier it leaves alone. An earlier draft of
    this docstring claimed B was also left alone, which the code contradicted —
    and the code was right. `B -> A` is an UPGRADE, because A is the stronger
    rung; the incumbent comment's "never downgrade a stronger thesaurus tier like
    A/B" reads B as stronger than A, which inverts the ladder it is defending. A
    concept carrying a decisive identifier IS tier-A evidence by the pack's own
    definition of the rung, so labelling it anything weaker is the error.

    THE INCUMBENT CONDITION WAS TOO NARROW, and that is the second half of the
    defect. It read `evidence_tier in (None, "G")` — so it lifted the bare-capture
    floor and refused to lift C or D, which are also WEAKER than A. The intent in
    its own comment was "never downgrade"; excluding C and D does not serve that
    intent, it just declines to fix them. Comparing against the ladder expresses
    the intent directly and lifts anything weaker.

    Uses `all_decisive_keys()` deliberately, matching the incumbent consumers.
    Tightening to `decisive_keys_for(subtype)` — so an organisation's EIN cannot
    lift a person — is the real prize and is a separate, measurable change to who
    collapses onto whom. Mixing it in here would move two variables at once.
    """
    if not identifiers:
        return current_tier
    if not any(identifiers.get(k) for k in all_decisive_keys()):
        return current_tier
    if current_tier is None:
        return DECISIVE_TIER
    return DECISIVE_TIER if str(current_tier) > DECISIVE_TIER else current_tier
