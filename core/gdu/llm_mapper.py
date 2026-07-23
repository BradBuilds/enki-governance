"""
LLM-to-GDU Mapper — The Boundary

Sits at the LDU-W → GDU boundary. The LLM extracts freely (variable
categories, natural language types). This module maps that free output
to the nearest valid GDU base-60 address.

The LLM does NOT speak GDU. This mapper translates after extraction.
Items that don't map to any valid address go to LDU-P (provisional),
where they accumulate. If enough cluster around the same gap, that
signals a need for vocabulary expansion.

Usage:
    mapper = get_mapper()
    result = mapper.map_entity({"name": "Iran", "type": "location", "subtype": "country"})
    # result.category = "place", result.subtype = "country", result.routed_to_ldup = False

    result = mapper.map_entity({"name": "20%", "type": "quantity"})
    # result.category = None, result.is_metadata = True (not a node)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

_log = logging.getLogger("enki.gdu.llm_mapper")


# ── Result types ────────────────────────────────────────────────────────

@dataclass
class MappingResult:
    """Result of mapping an LLM extraction to GDU coordinates."""
    category: Optional[str] = None
    subtype: Optional[str] = None
    input_type: str = ""
    routed_to_ldup: bool = False
    ldup_reason: str = ""
    is_metadata: bool = False     # quotes, quantities — stored but not nodes


@dataclass
class MappedEdge:
    """An edge to create, derived from an LLM relationship."""
    source_name: str = ""
    target_name: str = ""
    relationship: str = ""        # edgevocab edge type
    is_entity_relationship: bool = False  # goes to entity_relationships table


# ── Singleton ───────────────────────────────────────────────────────────

_MAPPER: Optional[LLMMapper] = None


def get_mapper() -> LLMMapper:
    global _MAPPER
    if _MAPPER is None:
        _MAPPER = LLMMapper()
    return _MAPPER


class LLMMapper:
    """Maps free-form LLM extraction output to GDU vocabulary addresses."""

    # ── Entity type mapping ─────────────────────────────────────────────
    # LLM type string → (GDU category, GDU subtype)
    # None values mean "not a node" (metadata only)
    # "ldup" values mean "no valid mapping, route to provisional"

    _ENTITY_TYPE_MAP: dict[str, tuple[Optional[str], Optional[str]]] = {
        # Direct entity mappings
        "person":       ("entity", "person"),
        "organization": ("entity", "organization"),
        "company":      ("entity", "organization"),
        "government":   ("entity", "organization"),
        "military":     ("entity", "organization"),
        "media":        ("entity", "organization"),
        "ngo":          ("entity", "organization"),
        "research":     ("entity", "organization"),
        "vessel":       ("entity", "vessel"),
        "aircraft":     ("entity", "aircraft"),
        "vehicle":      ("entity", "vehicle"),
        "facility":     ("entity", "facility"),
        "device":       ("entity", "device"),
        "weapon":       ("entity", "weapon_system"),
        "weapon_system": ("entity", "weapon_system"),
        "asset":        ("entity", "product"),
        "product":      ("entity", "product"),
        "unit":         ("entity", "unit"),
        "account":      ("entity", "account"),

        # Place mappings
        "location":     ("place", None),   # subtype inferred from context
        "place":        ("place", None),
        "country":      ("place", "country"),
        "city":         ("place", "city"),
        "region":       ("place", "admin_region"),

        # Event mapping (subtype resolved via map_event_type)
        "event":        ("event", None),

        # Assessment mappings — THESE WERE PREVIOUSLY DISCARDED
        "fact":         ("assessment", "fact"),
        "claim":        ("assessment", "claim"),
        "observation":  ("assessment", "observation"),
        "hypothesis":   ("assessment", "hypothesis"),

        # Metadata only — stored on parent node, not separate nodes
        "quote":        (None, None),
        "quantity":     (None, None),
    }

    # ── Event type mapping ──────────────────────────────────────────────
    # LLM event_type string → nodevocab event subtype
    _EVENT_TYPE_MAP: dict[str, str] = {
        "military_action": "strike",
        "military":        "strike",
        "diplomatic":      "meeting",
        "political":       "statement",
        "economic":        "transaction",
        "disaster":        "weather",
        "cyber":           "alert",
        "legal":           "statement",
        "terrorism":       "strike",
        "scientific":      "publication",
        "archaeological":  "sighting",
        "archaeology":     "sighting",
    }

    # ── Relationship mapping ────────────────────────────────────────────
    # LLM relationship → (edgevocab edge OR entity_relationship, is_entity_rel)
    _RELATIONSHIP_MAP: dict[str, tuple[str, bool]] = {
        # `supports` and `contrasts` in edgevocab are ASSESSMENT-only edges.
        # Entity-to-entity "opposes" / "supports" are SEMANTIC relationships
        # that belong in the entity_relationships table, not knowledge_edges.
        "supports":       ("supports", True),   # entity-relationship, not edgevocab
        "opposes":        ("opposes", True),    # was wrongly mapped to 'contrasts'
        "member_of":      ("member_of", True),  # entity-relationship

        # These are entity-to-entity semantic relationships
        "meets_with":     ("meets_with", True),
        "attacks":        ("attacks", True),
        "sanctions":      ("sanctions", True),
        "leads":          ("leads", True),
        "allied_with":    ("allied_with", True),
        "investigates":   ("investigates", True),
        "funds":          ("funds", True),
        "reports_on":     ("reports_on", True),
        "owns":           ("owns", True),
        "employs":        ("employs", True),
        "partners_with":  ("partners_with", True),
        "competes_with":  ("competes_with", True),
        "seizes":         ("seizes", True),
        "threatens":      ("threatens", True),
        "negotiates_with": ("negotiates_with", True),
        "arrests":        ("arrests", True),
        "regulates":      ("regulates", True),
        "targets":        ("targets", True),
        "subsidiary_of":  ("subsidiary_of", True),
    }

    # ── Public API ──────────────────────────────────────────────────────

    def map_entity(self, ext_entity: dict) -> MappingResult:
        """Map one LLM-extracted entity to GDU coordinates.

        The LLM extracts freely. This method translates to the nearest
        valid GDU address. Returns MappingResult with category/subtype
        for valid mappings, or routed_to_ldup=True for unmappable items.
        """
        ext_type = (ext_entity.get("type", "") or "").lower().strip()
        ext_subtype = (ext_entity.get("subtype", "") or "").lower().strip()
        name = (ext_entity.get("name", "") or "").lower().strip()

        # ── Canonical name normalization ──────────────────────────
        # Look up the raw name in the canonical names dictionary.
        # If found, override both name and type with the canonical
        # version. This ensures "Bibi" and "Netanyahu" collapse to
        # the same node before they ever reach the gateway.
        try:
            from core.gdu.canonical_names import normalize_to_canonical
            canon = normalize_to_canonical(name)
            if canon:
                canonical_name, canonical_type, qid, role = canon
                # Update the extracted entity in place so the gateway uses it
                ext_entity["name"] = canonical_name
                ext_entity["_canonical"] = True
                ext_entity["_qid"] = qid
                ext_entity["_role"] = role
                # Override the type if the LLM got it wrong
                if canonical_type == "person":
                    return MappingResult(category="entity", subtype="person",
                                          input_type=ext_type)
                if canonical_type == "organization":
                    return MappingResult(category="entity", subtype="organization",
                                          input_type=ext_type)
                if canonical_type == "place":
                    return MappingResult(category="place", subtype="country",
                                          input_type=ext_type)
        except Exception:
            pass

        if not ext_type:
            return MappingResult(
                input_type=ext_type,
                routed_to_ldup=True,
                ldup_reason="no type specified",
            )

        # Gazetteer override — if name is in the gazetteer (country,
        # maritime area, or named admin region), force place/<subtype>
        # regardless of what the LLM classified it as. Phase 2.2 moved
        # this from a hardcoded set to the versioned gazetteerpack so
        # maritime areas and admin regions are no longer LDU-P fodder.
        try:
            from core.gdu.gazetteer import lookup as _gaz_lookup
            hit = _gaz_lookup(name)
            if hit:
                # Stamp coordinates so downstream writers can populate
                # entity_mentions.latitude/longitude (Phase 4).
                if hit.centroid_lat is not None and hit.centroid_lng is not None:
                    ext_entity["_centroid_lat"] = hit.centroid_lat
                    ext_entity["_centroid_lng"] = hit.centroid_lng
                if hit.canonical:
                    ext_entity["name"] = hit.canonical
                return MappingResult(
                    category="place",
                    subtype=hit.subtype,
                    input_type=ext_type,
                )
        except Exception:
            pass

        mapping = self._ENTITY_TYPE_MAP.get(ext_type)

        if mapping is None:
            # Unknown type → LDU-P
            return MappingResult(
                input_type=ext_type,
                routed_to_ldup=True,
                ldup_reason=f"type '{ext_type}' has no GDU mapping",
            )

        category, subtype = mapping

        # Metadata-only types (quote, quantity)
        if category is None:
            return MappingResult(
                input_type=ext_type,
                is_metadata=True,
            )

        # Location — infer subtype from context
        if category == "place" and subtype is None:
            subtype = self._infer_place_subtype(ext_entity)

        # Event — resolve subtype from event_type field
        if category == "event" and subtype is None:
            event_subtype = ext_subtype or ext_entity.get("event_type", "")
            subtype = self.map_event_type(event_subtype)
            if not subtype:
                subtype = "alert"  # fallback for unmapped event types

        return MappingResult(
            category=category,
            subtype=subtype,
            input_type=ext_type,
        )

    def map_event_type(self, event_type: str, context: Optional[dict] = None) -> Optional[str]:
        """Map an LLM event_type string to a nodevocab event subtype.

        Returns the mapped subtype or None if no mapping exists.
        """
        if not event_type:
            return None
        normalized = event_type.lower().strip()
        return self._EVENT_TYPE_MAP.get(normalized)

    def map_relationship(self, rel: dict) -> list[MappedEdge]:
        """Map an LLM relationship to edge(s) to create.

        Returns a list because one LLM relationship can produce multiple
        graph edges (e.g., "attacks" → entity_relationship + participated_in edge).
        """
        rel_type = (rel.get("type", "") or "").lower().strip().replace(" ", "_")
        source = (rel.get("source", "") or "").strip()
        target = (rel.get("target", "") or "").strip()

        if not rel_type or not source or not target:
            return []

        mapping = self._RELATIONSHIP_MAP.get(rel_type)
        if not mapping:
            # Unknown relationship — still create as entity_relationship
            return [MappedEdge(
                source_name=source,
                target_name=target,
                relationship=rel_type,
                is_entity_relationship=True,
            )]

        edge_type, is_entity_rel = mapping
        edges = [MappedEdge(
            source_name=source,
            target_name=target,
            relationship=edge_type,
            is_entity_relationship=is_entity_rel,
        )]

        return edges

    # ── Internal: Place subtype inference ───────────────────────────────

    def _infer_place_subtype(self, ext: dict) -> str:
        """Infer place subtype from LLM extraction context."""
        ext_subtype = (ext.get("subtype", "") or "").lower()
        name = (ext.get("name", "") or "").lower()
        relevance = (ext.get("relevance", "") or "").lower()

        # Direct subtype mapping
        subtype_map = {
            "country": "country",
            "city": "city",
            "town": "city",
            "village": "city",
            "region": "admin_region",
            "state": "admin_region",
            "province": "admin_region",
            "district": "admin_region",
            "facility": "facility_location",
            "strait": "maritime_area",
            "gulf": "maritime_area",
            "bay": "maritime_area",
            "sea": "maritime_area",
            "ocean": "maritime_area",
            "archaeological_site": "poi",
            "discovery_site": "poi",
        }

        if ext_subtype in subtype_map:
            return subtype_map[ext_subtype]

        # Heuristics from name
        for water_word in ("strait", "gulf", "bay", "sea", "ocean", "lake", "river"):
            if water_word in name:
                return "maritime_area"

        for country_indicator in ("republic", "kingdom", "states of"):
            if country_indicator in name:
                return "country"

        # ISO code check
        if len(ext.get("name", "")) == 2 and ext.get("name", "").isupper():
            return "country"

        # Relevance-based
        if any(w in relevance for w in ("country", "nation")):
            return "country"
        if any(w in relevance for w in ("city", "capital", "town")):
            return "city"
        if any(w in relevance for w in ("region", "province")):
            return "admin_region"

        # Default: most LLM-extracted locations are countries
        return "country"
