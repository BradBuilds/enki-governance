"""
Place extractor — universal gazetteer + LLM hook.

Cheap pass: uses the existing gazetteer at core.extraction.gazetteer, which
carries ~200 major geopolitical locations (countries, capitals, strategic
cities/regions). It scans the text for matches and returns ALL hits, not
just the best one — downstream resolvers dedupe and rank.

Deep pass: stubbed. Phase 4 wires an LLM pass that recognizes novel place
names (towns, districts, neighborhoods, coordinates in text) and fetches
geometry from a full gazetteer / Nominatim.
"""

from __future__ import annotations

import re
from typing import Any, Optional

# We reuse the existing curated gazetteer rather than copy its data. It
# exposes extract_location() for the best single match; for multi-place
# extraction we reach into its internal _LOCATIONS dict since that's the
# only way today without modifying that module. A follow-up cleanup can
# promote _LOCATIONS to a public constant.
from core.extraction import gazetteer as _gz


def _all_matches(text: str) -> list[dict[str, Any]]:
    """Return every gazetteer location that appears in `text`.

    Priority order (country > capital > region > city) is preserved in
    the output so the first element is the highest-priority match.
    """
    if not text:
        return []
    low = f" {text.lower()} "
    priority = {"country": 4, "capital": 3, "region": 2, "city": 1}

    hits: list[tuple[int, dict[str, Any]]] = []
    for name, (lat, lon, loc_type) in _gz._LOCATIONS.items():
        # word-boundary check mirroring gazetteer.extract_location
        if f" {name} " in low or low.startswith(" " + name + " ") or low.endswith(" " + name + " "):
            # record position of first occurrence for offset
            pos = low.find(name)
            hits.append((
                priority.get(loc_type, 0),
                {
                    "name_raw": name.title(),
                    "name_canonical": name.title(),
                    "lat": lat,
                    "lon": lon,
                    "place_type": loc_type,
                    "source": "gazetteer",
                    "first_offset": max(pos - 1, 0),
                    "confidence": 0.80 if loc_type in ("country", "capital") else 0.65,
                }
            ))

    # Dedupe by canonical name, keep highest priority
    dedup: dict[str, tuple[int, dict[str, Any]]] = {}
    for prio, hit in hits:
        key = hit["name_canonical"]
        if key not in dedup or dedup[key][0] < prio:
            dedup[key] = (prio, hit)

    ranked = sorted(dedup.values(), key=lambda x: (-x[0], x[1]["first_offset"]))
    return [h for _, h in ranked]


def extract_places_cheap(text: str) -> list[dict[str, Any]]:
    """Gazetteer-based place extraction. Returns a list of place dicts
    shaped for envelope.spatial.place_text / resolved_places use."""
    return _all_matches(text)


def extract_places_deep(
    text: str,
    *,
    llm: Optional[Any] = None,
    prompt_version: str = "place_v1",
) -> list[dict[str, Any]]:
    """LLM-backed place extraction. Phase 2 stub.

    TODO(Phase 4): Gemma prompt that returns place names + a confidence
    that each is geo-referring (disambiguates "Jordan" the person from
    "Jordan" the country). Geocoding handoff to Nominatim or the full
    gazetteer service is Phase 4.
    """
    if llm is None:
        return []
    return []
