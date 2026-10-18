"""
Wikidata entity-type checker — fetches and caches the "instance of" (P31)
claim for a QID so we can verify a Wikipedia/Wikidata match is the right
KIND of thing.

Critical for collapse correctness: a doc mention of "From To" (OCR-garbled
flight-log header) should NOT auto-collapse to Wikidata Q107072432 ("From",
the TV series) when the mention is suggested_type='person' or 'place'.

Public surface:

    get_qid_types(qid) -> dict
        Returns {is_human, is_organization, is_place, is_aircraft, is_vessel,
                 is_creative_work, is_event, instance_of_qids, instance_of_labels}
        Cached in wikidata_type_cache; only the first lookup hits the network.

    is_compatible(qid, suggested_type) -> bool
        Returns True iff the QID's instance_of types overlap with what
        suggested_type expects. Used to gate Wikipedia matches.

Type → expected Wikidata instance_of (transitively):

    suggested_type   → required overlap
    'person'         → is_human
    'organization'   → is_organization OR is_human (a person can run a thing)
    'place'          → is_place
    'aircraft'       → is_aircraft
    'vessel'         → is_vessel
    'facility'       → is_place
    'product'        → NOT is_creative_work (allow physical products, reject TV/film)

When suggested_type is None or 'unknown', we DON'T reject — we trust the
match. Most of the false positives we want to block come from confident
type guesses being matched to wrong-type Wikipedia articles.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.documents.wikidata_types")

# Wikidata QID → canonical "is a kind of X" buckets we care about.
# These are the top-level superclasses; the type fetcher walks instance_of
# AND subclass_of (P279) to determine membership.
#
# Reference: a Q ID is "human" if Q5 is in its instance_of OR in its
# subclass_of's transitive closure. Same for the others.

_HUMAN_QIDS = {"Q5"}                                 # human
_ORG_QIDS = {
    # Verified by lookup on 2026-05-13 — only QIDs we actually observed
    # being returned by Wikidata for our corpus matches.
    "Q43229",                                         # organization
    "Q4830453",                                       # business
    "Q783794",                                        # company
    "Q161726",                                        # multinational corporation
    "Q484652",                                        # international organization
    "Q1320047",                                       # government agency
    "Q1407",                                          # government organization
    "Q2659904",                                       # nonprofit organization
    "Q327333",                                        # government agency
    # Verified additions for U.S. gov / military / archives:
    "Q61883",                                         # air force (branch of military)
    "Q20857065",                                      # United States federal agency
    "Q1752939",                                       # independent agency of the United States government
    "Q2122214",                                       # national archives
    "Q176799",                                        # military unit
    "Q3918",                                          # university
    "Q31855",                                         # research institute
}
_PLACE_QIDS = {
    "Q515",                                           # city
    "Q6256",                                          # country
    "Q486972",                                        # human settlement
    "Q5119",                                          # capital city
    "Q35657",                                         # U.S. state
    "Q1248784",                                       # airport (verified label)
    "Q133442",                                        # mountain
    "Q22808",                                         # park
    "Q23005",                                         # district
    "Q41176",                                         # building (used loosely as a place)
    "Q1093829",                                       # city in the United States
    "Q47168",                                         # county of the United States
    # Verified additions for European place types (Paris, etc.):
    "Q15284",                                         # municipality
    "Q1549591",                                       # big city
    "Q200250",                                        # metropolis
    "Q484170",                                        # commune of France
    "Q3257686",                                       # locality
    "Q15642541",                                      # human-geographic territorial entity
    "Q56061",                                         # administrative territorial entity
}
_AIRCRAFT_QIDS = {
    "Q11436",                                         # aircraft
    "Q197",                                           # airplane
    "Q160091",                                        # helicopter
    "Q15056993",                                      # aircraft family (verified)
    "Q62414513",                                      # carrier onboard delivery aircraft (verified)
}
_VESSEL_QIDS = {
    "Q11446",                                         # ship/vessel
    "Q12271",                                         # boat
}
_CREATIVE_WORK_QIDS = {
    "Q17537576", "Q5398426", "Q11424", "Q571",       # creative work, TV series, film, book
    "Q482994", "Q7725634", "Q105543609",             # album, novel, song
    "Q7889", "Q1107",                                # video game, anime
    "Q1004",                                          # comics
}
_EVENT_QIDS = {
    "Q1656682", "Q1190554", "Q1148538",              # event, incident, occurrence
    "Q198", "Q180684",                               # war, conflict
}

# A Wikidata QID is a CONCEPT (class/metaclass) — not an instance of
# something real — if its instance_of (P31) includes one of these
# meta-types. Concepts are the dictionary/taxonomy anchors: "aircraft",
# "person", "city" — the abstract classes that specific instances
# (Boeing 727 N123AA, Alex Acme, New York City) instantiate.
_METACLASS_QIDS = {
    "Q35120",         # metaclass — "a class of classes"
    "Q16889133",      # class
    "Q5127848",       # category in mathematics
    "Q16887380",      # group of human beings
    "Q23958946",      # variety of chemical entity
    # Wikidata's own internal "class" tagging:
    "Q21146257",      # type (Wikidata)
    "Q104027032",     # disjoint class
    # Common concept anchors that DON'T tag themselves as metaclass but
    # are universally classes (instance_of=human is itself a class):
    # we cover these via heuristics below.
}

# Bucket QIDs that are well-known concept roots (entire bucket sets are
# concept roots by definition — "human", "city", "aircraft" are abstract
# types). When a QID itself IS one of these bucket QIDs (not an instance
# OF one), it's a concept.
_CONCEPT_ROOT_QIDS = (
    _HUMAN_QIDS | _ORG_QIDS | _PLACE_QIDS | _AIRCRAFT_QIDS |
    _VESSEL_QIDS | _CREATIVE_WORK_QIDS | _EVENT_QIDS
)


def _get_cached(qid: str) -> Optional[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT instance_of_qids, instance_of_labels, is_human,
                       is_organization, is_place, is_aircraft, is_vessel,
                       is_creative_work, is_event, is_concept
                  FROM wikidata_type_cache WHERE wikidata_qid = %s
                """,
                (qid,),
            )
            row = cur.fetchone()
    if not row:
        return None
    return {
        "wikidata_qid": qid,
        "instance_of_qids": row[0] or [],
        "instance_of_labels": row[1] or [],
        "is_human":         row[2],
        "is_organization":  row[3],
        "is_place":         row[4],
        "is_aircraft":      row[5],
        "is_vessel":        row[6],
        "is_creative_work": row[7],
        "is_event":         row[8],
        "is_concept":       row[9] if len(row) > 9 else False,
    }


def _save_cache(qid: str, fetched: dict) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wikidata_type_cache
                    (wikidata_qid, instance_of_qids, instance_of_labels,
                     is_human, is_organization, is_place, is_aircraft, is_vessel,
                     is_creative_work, is_event, is_concept, raw)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (wikidata_qid) DO UPDATE SET
                    instance_of_qids   = EXCLUDED.instance_of_qids,
                    instance_of_labels = EXCLUDED.instance_of_labels,
                    is_human           = EXCLUDED.is_human,
                    is_organization    = EXCLUDED.is_organization,
                    is_place           = EXCLUDED.is_place,
                    is_aircraft        = EXCLUDED.is_aircraft,
                    is_vessel          = EXCLUDED.is_vessel,
                    is_creative_work   = EXCLUDED.is_creative_work,
                    is_event           = EXCLUDED.is_event,
                    is_concept         = EXCLUDED.is_concept,
                    raw                = EXCLUDED.raw,
                    fetched_at         = NOW()
                """,
                (
                    qid, fetched["instance_of_qids"], fetched["instance_of_labels"],
                    fetched["is_human"], fetched["is_organization"], fetched["is_place"],
                    fetched["is_aircraft"], fetched["is_vessel"],
                    fetched["is_creative_work"], fetched["is_event"],
                    fetched.get("is_concept", False),
                    json.dumps(fetched.get("raw") or {}),
                ),
            )
        conn.commit()


# Per-process cache of "subclass_of (P279) ancestors" for any QID we've
# already walked. Keyed by QID, value is the set of all ancestor QIDs.
# Shared across calls so the second time we see "U.S. state" we don't
# re-walk to "place".
_P279_ANCESTORS_CACHE: dict[str, frozenset[str]] = {}

# Cap on P279 walk depth so a pathological subclass chain (or a cycle) can't
# spiral. 5 hops covers our needs: "carrier onboard delivery aircraft" →
# "transport aircraft" → "military aircraft" → "aircraft" reaches our root.
_P279_MAX_DEPTH = 5


import time as _time

# Small sleep between P279 walker fetches so we don't burst Wikidata.
# Wikidata's public budget is generous but bursts cause intermittent failures.
_P279_FETCH_DELAY_S = 0.05


def _fetch_p279_parents(qid: str) -> list[str]:
    """Return the direct P279 (subclass_of) parents of a QID. One network call.
    Used by the transitive walker below."""
    try:
        from core.research.sources import _CLIENT
    except Exception:
        return []
    if not _CLIENT:
        return []
    _time.sleep(_P279_FETCH_DELAY_S)
    try:
        resp = _CLIENT.get(
            f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json",
            follow_redirects=True,
        )
        if resp.status_code != 200:
            return []
        entity = resp.json().get("entities", {}).get(qid, {})
        claims = entity.get("claims", {})
        out: list[str] = []
        for c in claims.get("P279") or []:
            ms = c.get("mainsnak", {})
            dv = ms.get("datavalue", {})
            if dv.get("type") == "wikibase-entityid":
                pq = dv.get("value", {}).get("id")
                if pq:
                    out.append(pq)
        return out
    except Exception:
        return []


def _walk_p279_ancestors(qid: str, *, max_depth: int = _P279_MAX_DEPTH) -> frozenset[str]:
    """Walk P279 (subclass_of) from QID up to max_depth hops. Returns the
    transitive set of ancestor QIDs.

    Cached in _P279_ANCESTORS_CACHE so a corpus that mentions 50 different
    aircraft variants still only walks "aircraft" once."""
    if qid in _P279_ANCESTORS_CACHE:
        return _P279_ANCESTORS_CACHE[qid]
    visited: set[str] = set()
    frontier: list[str] = [qid]
    for _ in range(max_depth):
        next_frontier: list[str] = []
        for q in frontier:
            if q in visited:
                continue
            visited.add(q)
            for parent in _fetch_p279_parents(q):
                if parent not in visited:
                    next_frontier.append(parent)
        if not next_frontier:
            break
        frontier = next_frontier
    visited.discard(qid)   # don't list self as ancestor
    result = frozenset(visited)
    _P279_ANCESTORS_CACHE[qid] = result
    return result


def _matches_bucket_transitively(direct_qids: list[str], bucket: set) -> bool:
    """True iff any QID in direct_qids — OR any of its P279 ancestors —
    is in the bucket set."""
    direct_set = set(direct_qids)
    if direct_set & bucket:
        return True
    # Walk each direct QID's subclass_of ancestors looking for a bucket hit
    for q in direct_qids:
        if _walk_p279_ancestors(q) & bucket:
            return True
    return False


def _fetch_instance_of(qid: str) -> dict:
    """Fetch P31 (instance_of) + P279 (subclass_of) for a QID and classify it
    into our type buckets. With transitive P279 walking so we don't need to
    hand-curate every leaf-class QID."""
    try:
        from core.research.sources import _CLIENT, _wikidata_labels_batch
    except Exception as e:
        log.warning("could not import wikidata client: %s", e)
        return _empty_result(qid)

    if not _CLIENT:
        return _empty_result(qid)

    try:
        resp = _CLIENT.get(
            f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json",
            follow_redirects=True,
        )
        if resp.status_code != 200:
            return _empty_result(qid)
        data = resp.json()
        entity = data.get("entities", {}).get(qid, {})
        claims = entity.get("claims", {})
    except Exception as e:
        log.debug("wikidata fetch failed for %s: %s", qid, e)
        return _empty_result(qid)

    # Collect P31 (instance_of) + P279 (subclass_of, this entity's direct
    # superclasses). For each of those we walk further up via
    # _matches_bucket_transitively.
    direct_qids: list[str] = []
    for prop in ("P31", "P279"):
        for c in (claims.get(prop) or []):
            ms = c.get("mainsnak", {})
            dv = ms.get("datavalue", {})
            if dv.get("type") == "wikibase-entityid":
                bq = dv.get("value", {}).get("id")
                if bq:
                    direct_qids.append(bq)

    # Resolve labels for the direct claims only (we don't need labels for
    # the ancestors; the bucket check is by QID, not by label).
    labels_map = _wikidata_labels_batch(direct_qids) if direct_qids else {}
    labels = [labels_map.get(q, q) for q in direct_qids]

    return {
        "wikidata_qid":       qid,
        "instance_of_qids":   direct_qids,
        "instance_of_labels": labels,
        "is_human":           _matches_bucket_transitively(direct_qids, _HUMAN_QIDS),
        "is_organization":    _matches_bucket_transitively(direct_qids, _ORG_QIDS),
        "is_place":           _matches_bucket_transitively(direct_qids, _PLACE_QIDS),
        "is_aircraft":        _matches_bucket_transitively(direct_qids, _AIRCRAFT_QIDS),
        "is_vessel":          _matches_bucket_transitively(direct_qids, _VESSEL_QIDS),
        "is_creative_work":   _matches_bucket_transitively(direct_qids, _CREATIVE_WORK_QIDS),
        "is_event":           _matches_bucket_transitively(direct_qids, _EVENT_QIDS),
        "is_concept":         _is_concept_qid(qid, direct_qids),
        "raw":                {"claims_p31_count": len(claims.get("P31") or []),
                                "claims_p279_count": len(claims.get("P279") or []),
                                "p279_walked": True},
    }


def _empty_result(qid: str) -> dict:
    return {
        "wikidata_qid":       qid,
        "instance_of_qids":   [],
        "instance_of_labels": [],
        "is_human":           False, "is_organization": False, "is_place": False,
        "is_aircraft":        False, "is_vessel": False,
        "is_creative_work":   False, "is_event": False,
        "is_concept":         False,
        "raw":                {},
    }


def _is_concept_qid(qid: str, direct_qids: list[str]) -> bool:
    """A QID is a CONCEPT (class/category/type) iff one of:

       1. The QID itself is one of our curated bucket-root QIDs.
          These are the canonical "types" we recognize as concepts:
          Q5 (human), Q11436 (aircraft), Q515 (city), Q43229 (org), etc.

       2. The QID's instance_of (P31) is DIRECTLY a metaclass QID
          (Q35120 metaclass, Q16889133 class). This catches Wikidata
          items that are tagged as their own class.

    We deliberately do NOT walk P279 ancestors for concept detection,
    because P279 chains traverse mountains of class hierarchy and would
    wrongly tag instances (Sarah Q194808, Paris Q90, Kenya Q114) as
    concepts just because their type's class hierarchy eventually
    reaches a metaclass.

    Instances of a class don't become concepts — only the class itself.
    """
    if qid in _CONCEPT_ROOT_QIDS:
        return True
    if any(q in _METACLASS_QIDS for q in direct_qids):
        return True
    return False


def get_qid_types(qid: str) -> dict:
    """Public: return the bucket flags for a QID. Cached.

    We do NOT cache empty/failed results — those mean the network call
    failed or returned malformed data, and we want a retry next time, not
    a permanent "no_wikidata_classification" record."""
    if not qid:
        return _empty_result(qid or "")
    cached = _get_cached(qid)
    if cached is not None:
        # Only trust a cached hit if it has real classification data.
        # Empty cached results (all-False flags AND empty instance_of_qids)
        # are stale failed-fetches — drop and re-try.
        if cached["instance_of_qids"]:
            return cached
    fetched = _fetch_instance_of(qid)
    if fetched["instance_of_qids"]:
        # Only cache successful fetches
        _save_cache(qid, fetched)
    return fetched


# ─── The actual compatibility check ─────────────────────────────────────

# Mapping: suggested_type → bucket flag(s) that ANY of must be true.
# If suggested_type isn't in the map, we don't filter (return True).
_TYPE_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "person":       ("is_human",),
    "organization": ("is_organization", "is_human"),     # CEOs etc. can match orgs they founded
    "place":        ("is_place",),
    "facility":     ("is_place",),
    "aircraft":     ("is_aircraft",),
    "vessel":       ("is_vessel",),
}

# Conversely, types we BLOCK when the suggested_type is a real-world entity.
# A "person" mention should never resolve to a TV series QID.
_REAL_WORLD_TYPES = {"person", "organization", "place", "facility",
                     "aircraft", "vessel"}


def is_compatible(
    qid: str,
    suggested_type: Optional[str],
    *,
    strict_unknown: bool = False,
) -> tuple[bool, str]:
    """Return (compatible, reason).

    compatible=True  → accept the match
    compatible=False → reject; `reason` says why for logging

    suggested_type=None or 'unknown':
      • default: we can't filter — accept (trust the match)
      • strict_unknown=True: reject creative_work matches anyway. Used for
        review_required corpora (Acme, FBI files, etc.) where a wrong
        link is worse than no link.
    """
    if not qid:
        return True, "no_qid"

    no_type = (not suggested_type) or (suggested_type in ("unknown", ""))
    if no_type and not strict_unknown:
        return True, "no_type_hint"

    types = get_qid_types(qid)

    # Hard block: when we're in strict mode, reject creative_work + event
    # matches even without a type hint. Catches "From To" (OCR fragment) →
    # "From (TV series)" class of false positives on adversarial corpora.
    if no_type and strict_unknown:
        if types["is_creative_work"]:
            return False, "strict_unknown: creative_work_rejected"
        if types["is_event"]:
            return False, "strict_unknown: event_rejected"
        return True, "strict_unknown: not_creative_or_event"

    # Hard block: real-world entity type vs creative work
    if suggested_type in _REAL_WORLD_TYPES and types["is_creative_work"]:
        return False, "creative_work_blocks_realworld"

    # Type-specific requirement
    required = _TYPE_REQUIREMENTS.get(suggested_type)
    if required is None:
        return True, "no_requirement_for_type"

    if any(types[flag] for flag in required):
        return True, "type_match"

    # Wikidata returned classification but none matched
    if types["instance_of_qids"]:
        return False, f"type_mismatch: required {required} got {types['instance_of_labels'][:3]}"

    # No classification on Wikidata side — soft accept
    return True, "no_wikidata_classification"
