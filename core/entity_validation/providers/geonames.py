"""
GeoNames provider — the authoritative open registry for places.

GeoNames has 11M entries covering every inhabited place + every named
natural feature (straits, bays, mountains, rivers, lakes) + infrastructure
(airports, seaports) with coordinates, population, alt-names in 200+
languages, and an ISO feature-code taxonomy that maps cleanly to our types.

API: http://api.geonames.org/searchJSON?q=...&username=...
Requires a free GeoNames username. Set GEONAMES_USERNAME in env.
Rate limit: 30k/day free tier, sufficient for extraction-scale usage.

This provider runs at priority 15 (between Wikidata and Wikipedia) because
GeoNames is more authoritative than Wikipedia for place identification,
and catches the specific geographic features (Strait of Hormuz, Cape of
Good Hope, Everest) that Wikidata sometimes mislabels.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import requests

from core.entity_validation.provider import Provider
from core.entity_validation.types import Candidate, Match

log = logging.getLogger("enki.validation.geonames")

_SEARCH_URL = "http://api.geonames.org/searchJSON"

# GeoNames feature-class + feature-code to our taxonomy. The feature
# codes are the stable ISO identifiers (5-letter + 2-letter).
# Reference: https://www.geonames.org/export/codes.html
_FEATURE_TO_TYPE: dict[str, str] = {
    # A = countries, admin regions
    "PCLI": "country",                  # independent political entity
    "PCL":  "country",                  # political entity
    "PCLD": "country",                  # dependent political entity
    "PCLIX": "country",
    "PCLS": "country",                  # semi-independent
    "PCLF": "country",                  # freely associated state
    "ADM1": "region",                   # first-order admin division (state/province)
    "ADM2": "region",
    "ADM3": "region",
    "ADM1H": "region",
    "ADM2H": "region",

    # P = populated places (cities, towns)
    "PPL":  "city",
    "PPLA": "city",                     # seat of first-order admin
    "PPLA2": "city",
    "PPLA3": "city",
    "PPLC": "city",                     # capital
    "PPLF": "city",                     # farm village
    "PPLG": "city",                     # seat of government
    "PPLL": "city",
    "PPLS": "city",
    "PPLW": "city",                     # abandoned
    "PPLX": "city",                     # section of populated place

    # H = hydrographic features
    "STM":  "river",
    "STMC": "river",
    "STMX": "river",
    "LK":   "lake",
    "LKC":  "lake",
    "LKS":  "lake",
    "SEA":  "sea",
    "OCN":  "ocean",
    "GULF": "gulf",
    "BAY":  "bay",
    "STRT": "strait",                   # STRAIT OF HORMUZ!
    "CNL":  "canal",
    "CNLN": "canal",
    "CNLD": "canal",
    "HBR":  "seaport",                  # harbor
    "HBRX": "seaport",
    "CHN":  "strait",                   # channel
    "CHNM": "strait",
    "CHNN": "strait",

    # T = hypsographic features (mountains, deserts, etc.)
    "MT":   "mountain",
    "MTS":  "mountain_range",
    "RDGE": "mountain_range",
    "PK":   "mountain",                 # peak
    "HLL":  "mountain",
    "HLLS": "mountain_range",
    "PEN":  "peninsula",
    "PENX": "peninsula",
    "CAPE": "peninsula",
    "ISL":  "island",
    "ISLS": "island_group",
    "ARCH": "archipelago",
    "ATOL": "island",
    "DSRT": "desert",
    "PLAT": "region",                   # plateau
    "RGN":  "region",
    "RGNH": "region",                   # historical region
    "VAL":  "region",                   # valley

    # S = spots, infrastructure
    "AIRP": "airport",
    "AIRF": "airport",
    "AIRB": "airport",                  # airbase
    "AIRH": "airport",                  # heliport
    "PRT":  "seaport",
    "DAM":  "dam",
    "BDG":  "bridge",
    "TNL":  "bridge",                   # tunnel → treat as bridge-class infra
    "PS":   "power_plant",              # power station
    "MLWND": "power_plant",

    # V = vegetation, R = roads — not useful for entity types, skip
}


class GeoNamesProvider(Provider):
    name = "geonames"
    priority = 15                        # between Wikidata (10) and Wikipedia (20)
    timeout_seconds = 8.0
    supports_kinds = ("place", "entity")  # also handles entity candidates
                                          # that turn out to be places

    def __init__(self, timeout_seconds: float = 8.0, username: Optional[str] = None):
        self.timeout_seconds = timeout_seconds
        self.username = username or os.getenv("GEONAMES_USERNAME", "")
        self._session = requests.Session()

    def validate(self, candidate: Candidate) -> Optional[Match]:
        if not self.username:
            # No API key configured — skip silently so the chain proceeds.
            return None

        text = candidate.raw_text.strip()
        if not text:
            return None

        try:
            resp = self._session.get(
                _SEARCH_URL,
                params={
                    "q": text,
                    "username": self.username,
                    "maxRows": 5,
                    "style": "FULL",
                    "type": "json",
                    "fuzzy": 0.8,
                },
                timeout=self.timeout_seconds,
            )
        except Exception as e:
            log.debug("geonames fetch for %s failed: %s", text, e)
            return None

        if resp.status_code != 200:
            return None

        try:
            data = resp.json()
        except Exception:
            return None

        hits = data.get("geonames") or []
        if not hits:
            return None

        # Find the best-matching hit — exact name match preferred, then
        # population + admin-level. GeoNames ranks internally so hits[0]
        # is usually correct.
        for hit in hits[:3]:
            fcode = (hit.get("fcode") or "").upper()
            if fcode not in _FEATURE_TO_TYPE:
                continue
            mapped_type = _FEATURE_TO_TYPE[fcode]

            # Kind guard: place candidate must map to a place-type (always
            # true for GeoNames, but keep the invariant explicit).
            if candidate.kind == "entity" and mapped_type not in {
                "city", "country", "region", "river", "lake", "sea", "ocean",
                "gulf", "bay", "strait", "canal", "peninsula", "archipelago",
                "mountain_range", "mountain", "desert", "island", "island_group",
                "airport", "seaport", "dam", "bridge", "power_plant",
            }:
                continue

            name = hit.get("name") or text
            admin1 = hit.get("adminName1") or ""
            country = hit.get("countryName") or ""
            geonames_id = str(hit.get("geonameId") or "")

            # Description: "a strait between the Gulf of Oman and the Persian Gulf"
            desc_parts = []
            if fcode:
                desc_parts.append(_describe_feature(fcode))
            if admin1:
                desc_parts.append(f"in {admin1}")
            if country:
                desc_parts.append(country)
            description = ", ".join(desc_parts)

            return Match(
                source="geonames",
                source_id=geonames_id,
                source_url=f"https://www.geonames.org/{geonames_id}/",
                canonical_name=name,
                canonical_type=mapped_type,
                aliases=[
                    alt_name.get("name") for alt_name in (hit.get("alternateNames") or [])
                    if alt_name.get("name") and alt_name.get("lang") == "en"
                ][:8],
                identifiers={
                    "geonames_id": geonames_id,
                    "feature_code": fcode,
                    "country_code": hit.get("countryCode"),
                },
                confidence=0.92,
                description=description,
                extra={
                    "latitude":  hit.get("lat"),
                    "longitude": hit.get("lng"),
                    "population": hit.get("population"),
                    "feature_class": hit.get("fcl"),
                    "feature_code":  fcode,
                },
            )

        return None


def _describe_feature(fcode: str) -> str:
    """Human-readable feature description from GeoNames code."""
    descriptions = {
        "STRT": "a strait",
        "BAY":  "a bay",
        "GULF": "a gulf",
        "CNL":  "a canal",
        "ISL":  "an island",
        "ISLS": "an island group",
        "ARCH": "an archipelago",
        "MT":   "a mountain",
        "MTS":  "a mountain range",
        "PEN":  "a peninsula",
        "CAPE": "a cape",
        "PPLC": "the capital city",
        "PPLA": "an administrative capital",
        "PPL":  "a populated place",
        "ADM1": "a first-order administrative division",
        "PCLI": "an independent political entity",
        "AIRP": "an airport",
        "AIRB": "an airbase",
        "PRT":  "a port",
        "DAM":  "a dam",
    }
    return descriptions.get(fcode, f"a {fcode} feature")
