"""Place-name resolution as comparison between observers.

The extractor produces a place name as text. Turning that into a binding is
the same problem the fact layer has one level up, and it has the same wrong
answer: pick a source and trust it. Measured on the corpus, picking one is
not survivable —

    entities matched by name alone   3,048 of 3,256 rows bind ... but
                                     134 bind to an ORGANIZATION and
                                      68 bind to a PERSON
    gazetteerpack (governed, v1.1.0)   1,281 rows (39.3%)
    entity graph, place types only     2,584 rows (79.4%)

— and the two disagree in both directions. So no observer decides. Each says
what it saw, every answer is recorded including "I don't know", and the
binding is made only where they agree. Where they disagree the name keeps its
text, the binding stays NULL, and the disagreement is a finding.

This is the Phase 7 comparison pattern applied one layer down: `edgevocab` is
one column there, the gazetteer is one column here, and neither is the frame.

EXTERNAL OBSERVERS. An external geocoder is a legitimate observer and a
useful one — it is the only one that has seen places this corpus has not. It
is constrained in two ways that are not negotiable:

  1. It returns a VERDICT, never content. No coordinates, no canonical names,
     no identifiers cross back in. The plan's decision 5: licensed material is
     never an input; the only permitted shape is our data goes out and a
     conflict verdict comes back. Recorded as "an external reference
     disagrees", never as evidence and never as an input to a collapse.
  2. It is off unless explicitly enabled, because consulting one sends corpus
     place names to a third party. That is data egress and it is the
     operator's call, not a default.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.documents.place_observations")

# canonical_types that are places. Anything else answering to the name is a
# disagreement, not a binding — that is what catches "Harvard" and the 68
# rows whose "location" is a person.
PLACE_TYPES = ("named_area", "city", "country", "admin_region",
               "maritime_area", "facility", "region", "place")

VERDICT_PLACE = "place"
VERDICT_NOT_A_PLACE = "not_a_place"
VERDICT_UNKNOWN = "unknown"


def normalize(name: str) -> str:
    return (name or "").strip().lower()


# ─── Observers ────────────────────────────────────────────────────────────
# Each returns a dict: {verdict, entity_id?, canonical_type?, lat?, lng?,
# detail?, observer_version?}. None of them raises — an observer that fails
# is an observer that says "unknown", which is a real answer.

def _observe_entity_graph(cur, name: str) -> dict:
    """What the corpus already holds under this name."""
    cur.execute(
        """
        SELECT entity_id, canonical_type
          FROM entities
         WHERE lower(canonical_name) = %s
         ORDER BY (canonical_type = ANY(%s)) DESC, entity_id
         LIMIT 1
        """,
        (normalize(name), list(PLACE_TYPES)),
    )
    row = cur.fetchone()
    if not row:
        return {"verdict": VERDICT_UNKNOWN}
    entity_id, ctype = row
    if ctype in PLACE_TYPES:
        return {"verdict": VERDICT_PLACE, "entity_id": entity_id,
                "canonical_type": ctype}
    # The name exists and is something else. That is a positive finding, not
    # an absence — it is the signal that the extractor put a non-place in the
    # location slot.
    return {"verdict": VERDICT_NOT_A_PLACE, "entity_id": entity_id,
            "canonical_type": ctype,
            "detail": {"reason": "name resolves to a non-place entity"}}


def _observe_gazetteer_pack(cur, name: str) -> dict:
    """What the governed pack says. Content-bearing and allowed to be,
    because the pack is in the signed active set."""
    try:
        from core.gdu import gazetteer as gz
        hit = gz.lookup(name)
        version = gz.get_pack_version()
    except Exception as e:
        log.debug("gazetteer observer unavailable: %s", e)
        return {"verdict": VERDICT_UNKNOWN}
    if not hit:
        return {"verdict": VERDICT_UNKNOWN, "observer_version": version}
    return {"verdict": VERDICT_PLACE,
            "canonical_type": hit.subtype,
            "lat": hit.centroid_lat, "lng": hit.centroid_lng,
            "detail": {"canonical": hit.canonical, "qid": hit.qid},
            "observer_version": version}


# External observers register here. Nothing is registered by default; an
# entry only appears when an operator wires one up and enables egress.
_EXTERNAL: dict[str, Callable[[str], dict]] = {}


def register_external_observer(name: str, fn: Callable[[str], dict]) -> None:
    """Register an external reference check.

    The callable receives a place name and must return only
    {verdict, detail?} — a verdict and, at most, a short non-content note.
    Any coordinates, canonical names or identifiers it returns are stripped
    before storage: an external reference may disagree with us, it may not
    tell us the answer.
    """
    _EXTERNAL[name] = fn


def _external_enabled() -> bool:
    return (os.environ.get("ENKI_ALLOW_EXTERNAL_PLACE_CHECK") or "").lower() \
        in ("1", "true", "yes", "on")


def _observe_external(name: str) -> list[tuple[str, dict]]:
    if not _EXTERNAL or not _external_enabled():
        return []
    out = []
    for obs_name, fn in _EXTERNAL.items():
        try:
            raw = fn(name) or {}
        except Exception as e:
            log.warning("external observer %s failed on %r: %s", obs_name, name, e)
            raw = {"verdict": VERDICT_UNKNOWN, "detail": {"error": str(e)[:120]}}
        # Strip everything but the verdict and a note. This is the boundary.
        out.append((f"external:{obs_name}", {
            "verdict": raw.get("verdict") or VERDICT_UNKNOWN,
            "detail": {"note": str((raw.get("detail") or {}).get("note", ""))[:200]},
        }))
    return out


# ─── Comparison ───────────────────────────────────────────────────────────

def observe(cur, name: str, *, refresh: bool = False) -> dict[str, dict]:
    """Run every enabled observer over one place name and persist what each
    said. Returns {observer: observation}. Cached per name — 3,256 rows carry
    only 1,177 distinct names, so repeat lookups are free."""
    norm = normalize(name)
    if not norm or len(norm) < 2:
        return {}

    if not refresh:
        cur.execute(
            """
            SELECT observer, verdict, entity_id, canonical_type, lat, lng, detail
              FROM place_name_observations WHERE name_norm = %s
            """,
            (norm,),
        )
        cached = {r[0]: {"verdict": r[1], "entity_id": r[2],
                         "canonical_type": r[3], "lat": r[4], "lng": r[5],
                         "detail": r[6]} for r in cur.fetchall()}
        if cached:
            return cached

    observations: list[tuple[str, dict]] = [
        ("entity_graph", _observe_entity_graph(cur, name)),
        ("gazetteer_pack", _observe_gazetteer_pack(cur, name)),
    ]
    observations.extend(_observe_external(name))

    import json
    for observer, obs in observations:
        cur.execute(
            """
            INSERT INTO place_name_observations
                (name_norm, observer, verdict, entity_id, canonical_type,
                 lat, lng, detail, observer_version)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (name_norm, observer) DO UPDATE SET
                verdict = EXCLUDED.verdict,
                entity_id = EXCLUDED.entity_id,
                canonical_type = EXCLUDED.canonical_type,
                lat = EXCLUDED.lat, lng = EXCLUDED.lng,
                detail = EXCLUDED.detail,
                observer_version = EXCLUDED.observer_version,
                observed_at = now()
            """,
            (norm, observer, obs.get("verdict"), obs.get("entity_id"),
             obs.get("canonical_type"), obs.get("lat"), obs.get("lng"),
             json.dumps(obs.get("detail") or {}), obs.get("observer_version")),
        )
    return dict(observations)


def agreed_entity_id(cur, name: str) -> tuple[Optional[int], str]:
    """The binding, and why. Returns (entity_id or None, reason).

    The rule is abstention, not majority: any observer calling this name a
    non-place vetoes the binding, however many others call it a place. A
    single contrary observation is cheap to record and expensive to ignore —
    it is exactly the 68 person-as-location rows that a majority rule would
    have bound anyway.
    """
    obs = observe(cur, name)
    if not obs:
        return None, "no_name"

    if any(o.get("verdict") == VERDICT_NOT_A_PLACE for o in obs.values()):
        who = [k for k, o in obs.items() if o.get("verdict") == VERDICT_NOT_A_PLACE]
        return None, f"vetoed_by:{','.join(sorted(who))}"

    binders = {k: o for k, o in obs.items()
               if o.get("verdict") == VERDICT_PLACE and o.get("entity_id")}
    if not binders:
        speakers = [k for k, o in obs.items() if o.get("verdict") == VERDICT_PLACE]
        if speakers:
            # Someone recognises the place but nothing in the corpus holds it.
            # Deliberately NOT admitting a new entity here: minting places
            # from extractor output is how junk entities get made. The text
            # survives on the statement and the observation is recorded.
            return None, f"known_but_unbound:{','.join(sorted(speakers))}"
        return None, "unknown_to_all"

    ids = {o["entity_id"] for o in binders.values()}
    if len(ids) > 1:
        return None, "observers_disagree_on_entity"
    return ids.pop(), f"agreed:{','.join(sorted(binders))}"


def disagreements(cur, limit: int = 100) -> list[dict]:
    """Names where the observers do not agree. Surfaced, not corrected."""
    cur.execute(
        """
        SELECT name_norm,
               array_agg(observer || '=' || verdict ORDER BY observer) AS votes,
               count(DISTINCT verdict) AS n_verdicts
          FROM place_name_observations
         GROUP BY name_norm
        HAVING count(DISTINCT verdict) > 1
         ORDER BY name_norm
         LIMIT %s
        """,
        (limit,),
    )
    return [{"name": r[0], "votes": r[1], "distinct_verdicts": r[2]}
            for r in cur.fetchall()]
