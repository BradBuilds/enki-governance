"""The resolution clock — one owner, from the moment it exists.

Phase 4 of the internal temporal constitutive substrate plan. Migration
147 gave `form_concept_bindings` a validity interval; this is where the clock
that will one day be compared against it is normalised.

WHY A MODULE FOR ONE FUNCTION
-----------------------------
Because of the defect this codebase has now paid for four times: completeness
(12 writers), the admission hash chain (2), form folding, and the decisive-tier
lift. Every one was the same shape — one rule, two owners, silent divergence —
and every fix was the same: the function that writes the value owns the
invariant.

Phase 5 adds a predicate ("is this binding valid as of X?") that BOTH the sense
resolver and the mention resolver will need, since both read
`form_concept_bindings`. Written twice, it will diverge; and a temporal
predicate that diverges produces two nodes that disagree about the same
document, which is the one property federation cannot survive. So the home
exists before there are two callers, not after.

WHAT PHASE 4 PUTS HERE, AND WHAT IT DELIBERATELY DOES NOT
---------------------------------------------------------
Phase 4's exit criterion is verbatim: *"bindings can carry intervals; nothing
yet reads them."* So this module normalises and validates the clock and stops.
There is no interval predicate here yet — writing one would make something read
the intervals, which is Phase 5 and must arrive with the shadow-run and the
anachronism veto that justify it.

`None` IS NOT "NOW", AND THAT IS THE WHOLE POINT
------------------------------------------------
Phase 5.3: defaulting an unspecified clock to "now" is the single most likely
way this goes wrong — right most of the time, catastrophically wrong on exactly
the historical archive material this system is being built for. Phase 0.3
measured the ground for that: `document_date` is NULL on 6,820 of 6,820 node-A
documents, so a "now" default would silently license every one of them to be
read with 2026 vocabulary.

`None` therefore means **no clock asserted**, never "today". It leaves Phase 5
free to abstain, which is the only honest answer for undated evidence.

DETERMINISM
-----------
Phase 5.4: the clock is part of the input, so `same input + same packs -> same
output` now includes it. `normalize_as_of` returns a `date`, never a
`datetime` — a resolution that depended on the hour it ran would make the public
determinism claim false, and a `datetime` slipped in through a caller's
`datetime.now()` is exactly how that would happen without anyone deciding it.
"""
from __future__ import annotations

from datetime import date, datetime

__all__ = ["normalize_as_of"]


def normalize_as_of(as_of) -> date | None:
    """Validate a resolution clock down to a `date`, or `None` for no clock.

    Accepts `None`, a `date`, a `datetime` (truncated to its date — see the
    determinism note above), or an ISO-8601 string. Anything else raises.

    Raising rather than coercing is deliberate and matches gate F2: a typo is
    not an empty answer. Silently reading an unparseable clock as `None` would
    turn a caller's mistake into an abstention that looks like a considered
    one, and Phase 5 would then abstain for a reason nobody chose.
    """
    if as_of is None:
        return None
    if isinstance(as_of, datetime):
        return as_of.date()
    if isinstance(as_of, date):
        return as_of
    if isinstance(as_of, str):
        s = as_of.strip()
        if not s:
            # An empty string is a caller with no clock who thinks they have
            # one. Distinguishing it from None matters: None was chosen, "" was
            # not.
            raise ValueError("as_of='' is not a clock; pass None for 'no clock asserted'")
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
        except ValueError:
            pass
        try:
            return date.fromisoformat(s)
        except ValueError as e:
            raise ValueError(f"as_of={as_of!r} is not an ISO-8601 date or datetime") from e
    raise TypeError(f"as_of must be None, date, datetime or ISO-8601 str, "
                    f"not {type(as_of).__name__}")
