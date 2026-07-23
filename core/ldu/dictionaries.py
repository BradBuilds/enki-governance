"""
LDU dictionaries — unified thesaurus resolver.

One ``resolve(name, role)`` API across every pack-driven lookup we have:
  Layer 1: canonicalpack.aliases    (curated people/orgs, tier C)
  Layer 2: gazetteerpack             (places with QIDs + centroids, tier B)
  Layer 3: entityvocab.alias_seeds   (synonym sets, tier C)

Future layers (stubbed in, no entries yet):
  Layer 4: demonyms        ("Iranian" → "Iran")
  Layer 5: acronyms        ("USAF" → "United States Air Force")
  Layer 6: honorifics      (already handled by normalize_rules — placeholder)
  Layer 7: tickers         ("LMT" → "Lockheed Martin")
  Layer 8: external_id     (auto-populated index of QID→concept_id etc.)

The module is pure: no DB, no I/O beyond reading pack files at module
load. Results are deterministic for a given pack version set.

For ``admit_candidate``: call ``resolve(surface_form, role=proposed_type)``.
If hit, use the returned canonical_name as the concept's display name,
the canonical_type to pick the subtype_path, and the identifiers +
evidence_tier to gate promotion.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("enki.ldu.dictionaries")


# ── Result type ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ResolveResult:
    """A canonical resolution from any thesaurus layer."""
    canonical_name: str
    canonical_type: str                 # nodevocab category, e.g., 'entity' / 'place'
    canonical_subtype: Optional[str]    # nodevocab subtype, e.g., 'person' / 'country'
    identifiers: dict                   # {wikidata_id, cik, ein, iso2, ticker, ...}
    evidence_tier: str                  # 'A' / 'B' / 'C'
    source: str                         # which layer hit
    # Partition hints for the divisor-aware addressing:
    role_hint: Optional[str] = None         # 'political_leader' / 'business' / ...
    region_hint: Optional[str] = None       # 'africa' / 'asia' / 'middle_east' / ...
    sector_hint: Optional[str] = None       # 'financial_services' / 'tech_telecom' / ...
    domain_hint: Optional[str] = None       # 'air' / 'sea' / 'cyber' (weapon_system) ...
    # Free-form extras (centroid coords, role string, sanctions list, etc.)
    extra: dict = field(default_factory=dict)


# ── Layer 3: alias_seeds loader (new — wasn't loaded anywhere before) ──────

_ALIAS_SEEDS: Optional[dict[str, ResolveResult]] = None
_ALIAS_SEEDS_VERSION: Optional[str] = None


def _pack_root(pack: str) -> Path:
    try:
        from core.gdu.config import PROJECT_ROOT
        return PROJECT_ROOT / "libraries" / pack
    except Exception:
        return Path(__file__).resolve().parents[2] / "libraries" / pack


def _load_alias_seeds() -> None:
    global _ALIAS_SEEDS, _ALIAS_SEEDS_VERSION
    if _ALIAS_SEEDS is not None:
        return

    root = _pack_root("entityvocab")
    version = "unknown"
    index: dict[str, ResolveResult] = {}

    try:
        with open(root / "pack.json") as f:
            meta = json.load(f) or {}
            version = meta.get("version", "unknown")
    except Exception as e:
        log.debug("entityvocab pack.json not readable: %s", e)

    try:
        with open(root / "definitions" / "entityvocab.alias_seeds.json") as f:
            data = json.load(f) or {}
            content = data.get("content") or {}
            for seed in content.get("seeds", []):
                canonical = seed.get("canonical_name")
                ctype = (seed.get("canonical_type") or "").lower()
                if not canonical or not ctype:
                    continue

                # Map ctype to nodevocab (category, subtype). alias_seeds
                # uses bare names like "country", "person", "military_branch".
                category, subtype = _ctype_to_category_subtype(ctype)

                result = ResolveResult(
                    canonical_name=canonical,
                    canonical_type=category,
                    canonical_subtype=subtype,
                    identifiers={},
                    evidence_tier="C",
                    source="alias_seeds",
                    extra={"raw_type": ctype},
                )
                for form in seed.get("surface_forms", []):
                    key = (form or "").lower().strip()
                    if key:
                        index[key] = result
                # Also index the canonical itself
                index.setdefault(canonical.lower().strip(), result)
    except FileNotFoundError:
        log.info("alias_seeds file not present")
    except Exception as e:
        log.warning("alias_seeds not loaded: %s", e)

    _ALIAS_SEEDS = index
    _ALIAS_SEEDS_VERSION = version
    log.info("alias_seeds loaded: %d surface forms (v%s)", len(index), version)


def _ctype_to_category_subtype(ctype: str) -> tuple[str, Optional[str]]:
    """Map alias_seeds.canonical_type to (nodevocab_category, subtype).

    alias_seeds was written before the full nodevocab and uses bare type
    names. This maps them.
    """
    ct = ctype.lower().strip()
    # Place subtypes
    if ct in ("country", "state", "city", "region", "admin_region",
             "maritime_area", "ocean", "sea", "named_area"):
        if ct == "state":
            return ("place", "admin_region")
        return ("place", ct)
    # Entity subtypes
    if ct in ("person", "organization", "vessel", "aircraft", "vehicle",
             "facility", "account", "device", "unit", "weapon_system",
             "product", "government_agency", "mass_media"):
        return ("entity", ct)
    if ct == "military_branch":
        return ("entity", "unit")
    if ct == "company":
        return ("entity", "organization")
    # Fallback
    return ("entity", ct or None)


# ── Layer 1+2: existing pack APIs (wrap them) ──────────────────────────────

def _check_canonicalpack(name: str) -> Optional[ResolveResult]:
    """Layer 1 — curated people/orgs from canonicalpack.aliases.

    canonicalpack.normalize_to_canonical returns (canonical_name, type, qid, role)
    or None. We wrap it in a ResolveResult.
    """
    try:
        from core.gdu.canonical_names import normalize_to_canonical
    except Exception:
        return None

    hit = normalize_to_canonical(name)
    if not hit:
        return None

    canonical_name, ctype, qid, role = hit
    category, subtype = _ctype_to_category_subtype(ctype or "")

    identifiers = {}
    if qid:
        identifiers["wikidata_id"] = qid

    return ResolveResult(
        canonical_name=canonical_name,
        canonical_type=category,
        canonical_subtype=subtype,
        identifiers=identifiers,
        evidence_tier="C",
        source="canonicalpack",
        role_hint=_role_to_partition(role) if role else None,
        extra={"raw_role": role} if role else {},
    )


def _check_gazetteer(name: str) -> Optional[ResolveResult]:
    """Layer 2 — places from gazetteerpack."""
    try:
        from core.gdu.gazetteer import lookup
    except Exception:
        return None

    hit = lookup(name)
    if not hit:
        return None

    identifiers = {}
    if hit.qid:
        identifiers["wikidata_id"] = hit.qid
    if hit.iso2:
        identifiers["iso2"] = hit.iso2
    if hit.iso3:
        identifiers["iso3"] = hit.iso3

    # Region hint: pick the right axis based on place subtype.
    #   country         → continent of this country (by name)
    #   city / admin    → continent of the parent country (by ISO2 code)
    #   maritime_area   → no hint, falls back to depth-1 (only 5 oceans)
    if hit.subtype == "country":
        region_hint = _country_to_continent(hit.canonical)
    elif hit.subtype in ("city", "admin_region"):
        region_hint = _iso2_to_continent(hit.parent_country)
    else:
        region_hint = None

    extra = {}
    if hit.centroid_lat is not None and hit.centroid_lng is not None:
        extra["centroid"] = {"lat": hit.centroid_lat, "lng": hit.centroid_lng}
    if hit.parent_country:
        extra["parent_country"] = hit.parent_country

    return ResolveResult(
        canonical_name=hit.canonical,
        canonical_type="place",
        canonical_subtype=hit.subtype,
        identifiers=identifiers,
        evidence_tier="B",
        source="gazetteer",
        region_hint=region_hint,
        extra=extra,
    )


def _check_alias_seeds(name: str) -> Optional[ResolveResult]:
    """Layer 3 — synonym sets from entityvocab.alias_seeds."""
    _load_alias_seeds()
    if not _ALIAS_SEEDS:
        return None
    return _ALIAS_SEEDS.get(name.lower().strip())


# ── Public API ─────────────────────────────────────────────────────────────

def resolve(name: str, role: str = "name") -> Optional[ResolveResult]:
    """Find a canonical resolution for a surface form.

    Layers are consulted in priority order. First hit wins.

    Args:
        name: raw surface form (case-insensitive).
        role: optional role hint ('person'/'place'/'organization'/...).
              Currently unused for layer selection but may influence
              future layers (e.g., ticker layer only fires for type=org).

    Returns:
        ResolveResult or None if no layer hits.
    """
    if not name:
        return None

    # Layer order matters when the bulk-fill packs contain a ticker
    # (or other short identifier) that collides with a country code or
    # iconic place name. "USA" is a SEC ticker (Liberty All Star Equity
    # Fund) AND an ISO3 country code. The gazetteer is the authority
    # for place names, so it runs first.
    hit = _check_gazetteer(name)
    if hit:
        return hit

    hit = _check_canonicalpack(name)
    if hit:
        return hit

    hit = _check_alias_seeds(name)
    if hit:
        return hit

    return None


def get_versions() -> dict:
    """Pack versions for all loaded layers — for lineage stamping."""
    from core.gdu.canonical_names import get_pack_version as cp_ver
    from core.gdu.gazetteer import get_pack_version as gz_ver
    _load_alias_seeds()
    return {
        "canonicalpack": cp_ver(),
        "gazetteerpack": gz_ver(),
        "alias_seeds": _ALIAS_SEEDS_VERSION or "unknown",
    }


def reload_for_tests() -> None:
    global _ALIAS_SEEDS, _ALIAS_SEEDS_VERSION
    _ALIAS_SEEDS = None
    _ALIAS_SEEDS_VERSION = None
    try:
        from core.gdu.canonical_names import reload_pack_for_tests as cp_reload
        cp_reload()
    except Exception:
        pass
    try:
        from core.gdu.gazetteer import reload_for_tests as gz_reload
        gz_reload()
    except Exception:
        pass


# ── Partition hint helpers ─────────────────────────────────────────────────

# Map free-form `role` strings (from canonicalpack) to entity.person
# partition labels. Match the divisor declarations doc.
_ROLE_PARTITION_RULES = [
    # (substring match list, partition_label)
    (["president", "prime minister", "pm ", "chancellor", "monarch",
      "king", "queen", "senator", "minister", "ambassador",
      "governor", "mayor", "judge", "justice"], "political_leader"),
    (["general", "admiral", "commander", "intel", "intelligence",
      "spy", "operative", "marshal"], "military_security"),
    (["ceo", "cto", "cfo", "coo", "founder", "chairman", "chairwoman",
      "investor", "financier", "billionaire"], "business"),
    (["professor", "researcher", "scientist", "phd", "nobel",
      "academic"], "science_academic"),
    (["journalist", "reporter", "anchor", "editor", "writer",
      "musician", "artist", "actor", "actress", "celebrity",
      "athlete", "activist", "dissident", "defector",
      "whistleblower"], "media_culture"),
]


def _role_to_partition(role: str) -> Optional[str]:
    if not role:
        return None
    r = role.lower()
    for substrings, label in _ROLE_PARTITION_RULES:
        for s in substrings:
            if s in r:
                return label
    return "other_civilian"


# Map country canonical names to continent partition labels.
# Covers the gazetteerpack countries by canonical name. Future: pull
# from a structured continent column on the gazetteer entries.
_COUNTRY_CONTINENT = {
    # Africa
    "Algeria": "africa", "Angola": "africa", "Egypt": "africa",
    "Ethiopia": "africa", "Ghana": "africa", "Kenya": "africa",
    "Libya": "africa", "Morocco": "africa", "Nigeria": "africa",
    "South Africa": "africa", "Sudan": "africa", "Tunisia": "africa",
    # Asia
    "Afghanistan": "asia", "Bangladesh": "asia", "China": "asia",
    "India": "asia", "Indonesia": "asia", "Iran": "asia",
    "Iraq": "asia", "Israel": "asia", "Japan": "asia",
    "Jordan": "asia", "Kazakhstan": "asia", "Lebanon": "asia",
    "Malaysia": "asia", "Mongolia": "asia", "Myanmar": "asia",
    "Nepal": "asia", "North Korea": "asia", "Pakistan": "asia",
    "Palestine": "asia", "Philippines": "asia", "Qatar": "asia",
    "Saudi Arabia": "asia", "Singapore": "asia", "South Korea": "asia",
    "Sri Lanka": "asia", "Syria": "asia", "Taiwan": "asia",
    "Thailand": "asia", "Turkey": "asia", "United Arab Emirates": "asia",
    "Vietnam": "asia", "Yemen": "asia",
    # Europe
    "Austria": "europe", "Belarus": "europe", "Belgium": "europe",
    "Bulgaria": "europe", "Croatia": "europe", "Czech Republic": "europe",
    "Denmark": "europe", "Estonia": "europe", "Finland": "europe",
    "France": "europe", "Germany": "europe", "Greece": "europe",
    "Hungary": "europe", "Iceland": "europe", "Ireland": "europe",
    "Italy": "europe", "Latvia": "europe", "Lithuania": "europe",
    "Netherlands": "europe", "Norway": "europe", "Poland": "europe",
    "Portugal": "europe", "Romania": "europe", "Russia": "europe",
    "Serbia": "europe", "Slovakia": "europe", "Slovenia": "europe",
    "Spain": "europe", "Sweden": "europe", "Switzerland": "europe",
    "Ukraine": "europe", "United Kingdom": "europe",
    # Americas
    "Argentina": "americas", "Bolivia": "americas", "Brazil": "americas",
    "Canada": "americas", "Chile": "americas", "Colombia": "americas",
    "Cuba": "americas", "Ecuador": "americas", "Guatemala": "americas",
    "Honduras": "americas", "Jamaica": "americas", "Mexico": "americas",
    "Nicaragua": "americas", "Panama": "americas", "Paraguay": "americas",
    "Peru": "americas", "United States": "americas", "Uruguay": "americas",
    "Venezuela": "americas",
    # Oceania
    "Australia": "oceania", "Fiji": "oceania", "New Zealand": "oceania",
    "Papua New Guinea": "oceania",
    # Multi/maritime
    "Antarctica": "antarctica_maritime",
}


def _country_to_continent(canonical_name: str) -> Optional[str]:
    return _COUNTRY_CONTINENT.get(canonical_name)


# ISO2 → continent partition label. Used to backfill region_hint for
# cities (the gazetteer returns parent_country as ISO2, not name).
_ISO2_CONTINENT = {
    # Africa
    "DZ":"africa","AO":"africa","BJ":"africa","BW":"africa","BF":"africa",
    "BI":"africa","CM":"africa","CV":"africa","CF":"africa","TD":"africa",
    "KM":"africa","CG":"africa","CD":"africa","CI":"africa","DJ":"africa",
    "EG":"africa","GQ":"africa","ER":"africa","SZ":"africa","ET":"africa",
    "GA":"africa","GM":"africa","GH":"africa","GN":"africa","GW":"africa",
    "KE":"africa","LS":"africa","LR":"africa","LY":"africa","MG":"africa",
    "MW":"africa","ML":"africa","MR":"africa","MU":"africa","MA":"africa",
    "MZ":"africa","NA":"africa","NE":"africa","NG":"africa","RW":"africa",
    "ST":"africa","SN":"africa","SC":"africa","SL":"africa","SO":"africa",
    "ZA":"africa","SS":"africa","SD":"africa","TZ":"africa","TG":"africa",
    "TN":"africa","UG":"africa","ZM":"africa","ZW":"africa","EH":"africa",
    # Asia
    "AF":"asia","AM":"asia","AZ":"asia","BH":"asia","BD":"asia",
    "BT":"asia","BN":"asia","KH":"asia","CN":"asia","CY":"asia",
    "GE":"asia","HK":"asia","IN":"asia","ID":"asia","IR":"asia",
    "IQ":"asia","IL":"asia","JP":"asia","JO":"asia","KZ":"asia",
    "KW":"asia","KG":"asia","LA":"asia","LB":"asia","MO":"asia",
    "MY":"asia","MV":"asia","MN":"asia","MM":"asia","NP":"asia",
    "KP":"asia","OM":"asia","PK":"asia","PS":"asia","PH":"asia",
    "QA":"asia","SA":"asia","SG":"asia","KR":"asia","LK":"asia",
    "SY":"asia","TW":"asia","TJ":"asia","TH":"asia","TL":"asia",
    "TR":"asia","TM":"asia","AE":"asia","UZ":"asia","VN":"asia",
    "YE":"asia",
    # Europe
    "AL":"europe","AD":"europe","AT":"europe","BY":"europe","BE":"europe",
    "BA":"europe","BG":"europe","HR":"europe","CZ":"europe","DK":"europe",
    "EE":"europe","FI":"europe","FR":"europe","DE":"europe","GR":"europe",
    "HU":"europe","IS":"europe","IE":"europe","IT":"europe","XK":"europe",
    "LV":"europe","LI":"europe","LT":"europe","LU":"europe","MT":"europe",
    "MD":"europe","MC":"europe","ME":"europe","NL":"europe","MK":"europe",
    "NO":"europe","PL":"europe","PT":"europe","RO":"europe","RU":"europe",
    "SM":"europe","RS":"europe","SK":"europe","SI":"europe","ES":"europe",
    "SE":"europe","CH":"europe","UA":"europe","GB":"europe","VA":"europe",
    # Americas
    "AG":"americas","AR":"americas","BS":"americas","BB":"americas","BZ":"americas",
    "BO":"americas","BR":"americas","CA":"americas","CL":"americas","CO":"americas",
    "CR":"americas","CU":"americas","DM":"americas","DO":"americas","EC":"americas",
    "SV":"americas","GD":"americas","GT":"americas","GY":"americas","HT":"americas",
    "HN":"americas","JM":"americas","MX":"americas","NI":"americas","PA":"americas",
    "PY":"americas","PE":"americas","KN":"americas","LC":"americas","VC":"americas",
    "SR":"americas","TT":"americas","US":"americas","UY":"americas","VE":"americas",
    "PR":"americas","GL":"americas",
    # Oceania
    "AU":"oceania","FJ":"oceania","KI":"oceania","MH":"oceania","FM":"oceania",
    "NR":"oceania","NZ":"oceania","PW":"oceania","PG":"oceania","WS":"oceania",
    "SB":"oceania","TO":"oceania","TV":"oceania","VU":"oceania",
    # Antarctica + multi
    "AQ":"antarctica_maritime",
}


def _iso2_to_continent(iso2: Optional[str]) -> Optional[str]:
    if not iso2:
        return None
    return _ISO2_CONTINENT.get(iso2.upper())
