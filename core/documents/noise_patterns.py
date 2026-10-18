"""
Declarative noise patterns for the generic LLM extractor.

The generic extractor casts a wide net — it'll happily emit a mention
for any capitalized token that resembles a name. That's the right
default for unstructured prose where we don't want to hand-tune for
every domain, but it leaks structural tokens from any document that
has a formal grammar embedded in it: GDS booking codes, FOIA exemption
markers, queue references, internal office codes.

This module is the LAST line of defense. Domain-aware extractors (PNR,
court, government) should catch most of these at extraction time;
patterns here exist for documents that fell through to generic
extraction OR that mix structural codes into otherwise prose-heavy
content (e.g. a news article that quotes a PNR line).

Each pattern is a declarative rule:

  - kind:       short identifier for telemetry / pack federation
  - regex:      compiled regex applied to surface_form
  - action:     'reject' (drop the mention entirely) |
                'split_airport_pair' (decompose into segments — handled
                in the caller) | 'redaction_marker' (treat as a span,
                not a mention)
  - rationale:  human-readable why we're rejecting it. Recorded on the
                mention's review_reason so the operator can audit.

Use:

    from core.documents.noise_patterns import classify_noise
    verdict = classify_noise(surface_form, suggested_type)
    if verdict.action == 'reject':
        skip_or_mark(mention, verdict.rationale)
    elif verdict.action == 'split_airport_pair':
        for code in verdict.split_into:
            ...

For the long term these patterns belong in a versioned, signed pack
(entityvocab or a sibling) so customer nodes pick them up via the
federation channel. Hard-coded today, packed later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class NoiseVerdict:
    action:        str            # 'keep' | 'reject' | 'split_airport_pair' | 'redaction_marker'
    kind:          Optional[str]  # which pattern fired
    rationale:     str
    split_into:    list[str]      # populated for split_airport_pair


# ─── Known IATA airport codes (subset). Same intent as pnr._IATA — kept
# in sync deliberately; we want both modules to agree on what IS a real
# airport. A pull-from-shared-source refactor is the eventual right move.

_IATA_CODES: set[str] = {
    # mirror of pnr._IATA keys; this set is what the airport-pair rule
    # consults to decide "is this really a two-airport mash-up?"
    "JFK","EWR","LGA","BOS","DCA","IAD","BWI","ATL","MIA","FLL","PBI","TEB",
    "ORD","MDW","DFW","IAH","DEN","PHX","LAX","SFO","SEA","LAS","SAN","MSP",
    "DTW","PHL","CLT","YVR","YYZ","YUL","MEX","CUN",
    "STT","STX","SJU","NAS","BGI",
    "LHR","LGW","STN","LTN","LCY","MAN","EDI","CDG","ORY","NCE","AMS","FRA",
    "MUC","TXL","BER","ZRH","GVA","MAD","BCN","FCO","MXP","LIN","VIE","CPH",
    "ARN","OSL","HEL","DUB","BRU","LIS","ATH","IST","SVO","DME",
    "TLV","DXB","DOH","AUH","RUH","JED","AMM","BEY","CAI",
    "HND","NRT","KIX","ICN","PEK","PVG","HKG","TPE","SIN","BKK","KUL","CGK",
    "MNL","DEL","BOM","BLR","MAA","CCU",
    "SYD","MEL","BNE","PER","AKL",
    "GRU","GIG","EZE","SCL","LIM","BOG","JNB","CPT","NBO","ADD","LOS",
}


# ─── Patterns ───────────────────────────────────────────────────────────

# 1. Multi-airport routing mashed into a single token: EWRORY, ORYEWR,
#    CDGJFKLHR. Generic extractor emits as `place` or `facility`.
_AIRPORT_PAIR_RE = re.compile(r"^([A-Z]{3})([A-Z]{3})(?:([A-Z]{3}))?(?:([A-Z]{3}))?$")

# 2. Amadeus office code: 3-letter city + EC + 2 digits + 2 letters.
#    e.g. NYCEC08AA (New York), PAREC08AA (Paris).
_AMADEUS_OFFICE_RE = re.compile(r"^[A-Z]{3}EC\d{2}[A-Z]{2}$")

# 3. FOIA exemption markers — never an entity, always a redaction.
_FOIA_RE = re.compile(r"^\(b\)\([0-9](?:\)\([A-Z])?\)$")

# 4. GDS queue / internal codes: 6-9 chars, ALL CAPS + digits, no spaces,
#    not a known acronym. Examples: MUCWW21TD, DCS-SYNCUS, 1A1NV.
#    We only fire when the suggested_type was 'facility' or
#    'organization' because plenty of real ticker symbols / company
#    codes match this shape too.
_GDS_QUEUE_RE = re.compile(r"^[A-Z0-9]{5,12}[-_]?[A-Z0-9]{0,6}$")
_GDS_QUEUE_TRUSTED_BLACKLIST = {
    # Known Amadeus / Sabre / GDS noise tokens spotted in the corpus.
    "TKXL", "TKOK", "QENYC", "QEPAR", "ETEC", "MODETICKET",
    "MOPEXTERNAL", "AMADEUS-H",
    "DCS-SYNCUS", "1AINV",
}

# 5. Passenger-routing concat: EWRORY/Acme/Alex — the FA tail end
#    sometimes leaks into mention extraction as a single 'person' or
#    'facility'. Only handled when the suggested_type is person-ish.
_ROUTING_NAME_CONCAT_RE = re.compile(
    r"^[A-Z]{6}(?:[A-Z]{3})*/[A-Z]{3,}(?:/[A-Z]{3,})*$"
)

# 5b. PNR-format person name: LAST/FIRST optionally followed by a title
#     and (ADT/CHD/INF tag. e.g. 'Acme/Alex MR(ADT',
#     'Acme/Alex JAMES', 'Acme/Alex MR'. These should be
#     canonicalized to "First Last" form, not surfaced as their own
#     entity. We emit a verdict with split_into = ['First', 'Last'] so
#     the caller can rebuild the proper surface.
_PNR_PERSON_RE = re.compile(
    r"^(?P<last>[A-Z]{2,})/(?P<first>[A-Z]+(?:\s+[A-Z]+)*)"
    r"(?:\s+(?:MR|MRS|MS|MISS|DR|MSTR))?"
    r"(?:\((?:ADT|CHD|INF|YTH))?$"
)

# 6. Bare action / verb codes: TKXL, TKOK, FF, etc. — 2-5 chars of
#    all-caps, no digits.
_GDS_ACTION_RE = re.compile(r"^(?:TKXL|TKOK|FF|QE|FA|FP|RM|AP|FH|MOPEXTERNAL|MODETICKET)$")

# 7. Lone single-character / two-character "names" suggested as people
#    — almost always a parser artifact.
_TINY_PERSON_RE = re.compile(r"^[A-Z][a-z]?$|^[A-Z]{1,2}$")


def classify_noise(surface_form: str, suggested_type: Optional[str] = None) -> NoiseVerdict:
    """Apply the rule set in priority order. First match wins."""
    sf = (surface_form or "").strip()
    if not sf:
        return NoiseVerdict("reject", "empty_form", "empty surface_form", [])

    t = (suggested_type or "").lower()

    # 1. Airport pair
    m = _AIRPORT_PAIR_RE.match(sf)
    if m and t in {"place", "facility", "organization"}:
        codes = [g for g in m.groups() if g]
        known = sum(1 for c in codes if c in _IATA_CODES)
        # Need at least two known IATA codes for high confidence — that
        # rules out random 6-letter ALL-CAPS tokens that happen to fit
        # the shape but aren't airports (e.g. KGBJFK from a body of text
        # — KGB is not IATA).
        if known >= 2:
            return NoiseVerdict(
                action="split_airport_pair",
                kind="airport_pair",
                rationale=f"multi-airport routing token ({'+'.join(codes)})",
                split_into=codes,
            )

    # 2. Amadeus office code
    if _AMADEUS_OFFICE_RE.match(sf):
        return NoiseVerdict(
            action="reject",
            kind="amadeus_office_code",
            rationale=f"Amadeus office code ({sf}), not a place/facility entity",
            split_into=[],
        )

    # 3. FOIA exemption
    if _FOIA_RE.match(sf):
        return NoiseVerdict(
            action="redaction_marker",
            kind="foia_exemption",
            rationale=f"FOIA exemption marker {sf}, treat as redaction span",
            split_into=[],
        )

    # 4. Known GDS noise tokens
    sf_up = sf.upper()
    if sf_up in _GDS_QUEUE_TRUSTED_BLACKLIST:
        return NoiseVerdict(
            action="reject",
            kind="gds_noise_token",
            rationale=f"known GDS/PNR structural token ({sf_up}), not an entity",
            split_into=[],
        )

    # 5. Routing-name concat
    if _ROUTING_NAME_CONCAT_RE.match(sf):
        return NoiseVerdict(
            action="reject",
            kind="passenger_routing_concat",
            rationale=f"PNR routing-and-passenger concatenation, not a single entity",
            split_into=[],
        )

    # 5b. PNR-format person name → emit canonical "First Last" via split_into
    pnr_p = _PNR_PERSON_RE.match(sf)
    if pnr_p and t == "person":
        last = pnr_p.group("last").title()
        first = pnr_p.group("first").title()
        canonical = f"{first} {last}"
        return NoiseVerdict(
            action="reject",
            kind="pnr_person_format",
            rationale=f"PNR slash-form person ({sf!r}); canonical is {canonical!r}",
            split_into=[canonical],
        )

    # 6. Bare action codes
    if _GDS_ACTION_RE.match(sf):
        return NoiseVerdict(
            action="reject",
            kind="gds_action_code",
            rationale=f"GDS action verb ({sf}), not an entity",
            split_into=[],
        )

    # 7. Single/double-letter "person" mentions
    if t == "person" and _TINY_PERSON_RE.match(sf):
        return NoiseVerdict(
            action="reject",
            kind="tiny_person_artifact",
            rationale=f"single/double-letter person name ({sf!r}), parser artifact",
            split_into=[],
        )

    # Default: keep.
    return NoiseVerdict("keep", None, "", [])
