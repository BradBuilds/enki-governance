"""
GDU Pack Registry — loads vocabulary packs into structured lookups.

Replaces all hardcoded type constants (VALID_ENTITY_TYPES, TYPE_MAP,
VALID_RELATIONSHIPS) with pack-loaded values from nodevocab, edgevocab,
and completenessvocab. Singleton, cached at startup.

The registry IS the GDU grammar in memory. If a category, subtype, or
edge type isn't in this registry, it doesn't exist in the collapsed space.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

_log = logging.getLogger("enki.gdu.pack_registry")

# ── Singleton state ─────────────────────────────────────────────────────
_NODE_TAXONOMY: Optional[dict] = None
_EDGE_TYPES: Optional[dict] = None
_SLOT_SPECS: Optional[dict] = None
_INITIALIZED = False

# ── Fixed category order (base-60 first digit) ─────────────────────────
# This order is permanent. New categories append; existing never move.
CATEGORY_ORDER = [
    "entity",      # [0]
    "place",       # [1]
    "event",       # [2]
    "topic",       # [3]
    "source",      # [4]
    "document",    # [5]
    "chronicle",   # [6]
    "assessment",  # [7]
    "pattern",     # [8]
    "mention",     # [9]  — v0.4.0 (2026-05-11): per-occurrence mentions inside documents
    "issue",       # [10] — v0.4.0 (2026-05-11): first-class ambiguity nodes
    "form",        # [11] — v0.5.0 (2026-05-14): surface-form address layer; foundation of base-60 cyclical language model
    "definition",  # [12] — v0.7.0 (2026-05-14): the schema becomes addressable — edge types, type-pair contracts, concept-instance rules, dictionary edges
    "library",     # [13] — v0.8.0 (2026-05-14): topic-anchored addressable subgraphs (OSINT discovery loop)
]

_CATEGORY_INDEX = {name: i for i, name in enumerate(CATEGORY_ORDER)}

# ── Subtype order caches (built from nodevocab on first load) ──────────
_SUBTYPE_ORDERS: dict[str, list[str]] = {}
_SUBTYPE_INDEX: dict[str, dict[str, int]] = {}


def _load_packs():
    """Load all vocabulary packs through GDU strict mode. Falls back to
    filesystem if GDU context is unavailable (dev/test)."""
    global _NODE_TAXONOMY, _EDGE_TYPES, _SLOT_SPECS, _INITIALIZED
    global _SUBTYPE_ORDERS, _SUBTYPE_INDEX

    if _INITIALIZED:
        return

    _NODE_TAXONOMY = _load_nodevocab()
    _EDGE_TYPES = _load_edgevocab()
    _SLOT_SPECS = _load_completenessvocab()

    # Build subtype order caches from taxonomy
    if _NODE_TAXONOMY:
        categories = _NODE_TAXONOMY.get("categories", {})
        for cat_name, cat_data in categories.items():
            subtypes = cat_data.get("subtypes", {})
            ordered = list(subtypes.keys())
            _SUBTYPE_ORDERS[cat_name] = ordered
            _SUBTYPE_INDEX[cat_name] = {st: i for i, st in enumerate(ordered)}

    _INITIALIZED = True
    _log.info(
        "Pack registry loaded: %d categories, %d edge types, %d slot specs",
        len(_NODE_TAXONOMY.get("categories", {})) if _NODE_TAXONOMY else 0,
        len(_EDGE_TYPES.get("edges", {})) if _EDGE_TYPES else 0,
        len(_SLOT_SPECS.get("specs", {})) if _SLOT_SPECS else 0,
    )


def _read_pack_version(pack_name: str) -> str:
    """Read the version field from a pack's pack.json. Returns 'unknown'
    if the file is missing or unreadable. Phase 1.5: previously the
    pack version never propagated to gdu_pack_positions, leaving every
    row stamped 'unknown'. Now we read it once per pack at load time."""
    try:
        from core.gdu.config import PROJECT_ROOT
        path = PROJECT_ROOT / "libraries" / pack_name / "pack.json"
        if path.exists():
            with open(path) as f:
                meta = json.load(f) or {}
                v = meta.get("version")
                if isinstance(v, str) and v:
                    return v
    except Exception as e:
        _log.debug("Could not read %s/pack.json: %s", pack_name, e)
    return "unknown"


def _load_json_file(pack_name: str, filename: str) -> dict:
    """Load a specific pack definition JSON file via filesystem.

    Goes directly to the file — no GDU pack iteration needed since we
    know exactly which file contains which definitions.

    Stamps the loaded dict with `__pack_version__` (read from the pack's
    pack.json) so downstream consumers (sql_pack_sync, lineage stamping)
    can record which pack version generated each row.
    """
    try:
        from core.gdu.config import PROJECT_ROOT
        path = PROJECT_ROOT / "libraries" / pack_name / "definitions" / filename
        if path.exists():
            with open(path) as f:
                data = json.load(f)
                content = data.get("content", data)
                if isinstance(content, dict):
                    content.setdefault("__pack_version__", _read_pack_version(pack_name))
                return content
        else:
            _log.warning("Pack file not found: %s", path)
    except Exception as e:
        _log.warning("Failed to load %s/%s: %s", pack_name, filename, e)

    return {}


def _load_nodevocab() -> dict:
    return _load_json_file("nodevocab", "nodevocab.node_types.json")


def _load_edgevocab() -> dict:
    return _load_json_file("edgevocab", "edgevocab.edge_types.json")


def _load_completenessvocab() -> dict:
    return _load_json_file("completenessvocab", "completenessvocab.slot_specs.json")


# ── Public API ──────────────────────────────────────────────────────────

def get_node_taxonomy() -> dict:
    """Full 9-category tree from nodevocab."""
    _load_packs()
    return _NODE_TAXONOMY or {}


def get_edge_types() -> dict:
    """All edge definitions from edgevocab."""
    _load_packs()
    return _EDGE_TYPES or {}


def get_slot_specs() -> dict:
    """Completeness slot specs from completenessvocab."""
    _load_packs()
    return _SLOT_SPECS or {}


def get_valid_categories() -> set[str]:
    """All 9 category names."""
    return set(CATEGORY_ORDER)


def get_valid_entity_subtypes() -> set[str]:
    """All entity subtypes from nodevocab. Replaces hardcoded VALID_ENTITY_TYPES."""
    _load_packs()
    return set(_SUBTYPE_ORDERS.get("entity", []))


def get_valid_subtypes(category: str) -> set[str]:
    """All subtypes for a given category."""
    _load_packs()
    return set(_SUBTYPE_ORDERS.get(category, []))


def get_all_valid_subtypes() -> dict[str, set[str]]:
    """All subtypes grouped by category."""
    _load_packs()
    return {cat: set(subs) for cat, subs in _SUBTYPE_ORDERS.items()}


def get_valid_edge_names() -> set[str]:
    """All valid edge relationship names from edgevocab.
    Replaces hardcoded VALID_RELATIONSHIPS."""
    _load_packs()
    if not _EDGE_TYPES:
        return set()
    return set(_EDGE_TYPES.get("edges", {}).keys())


def get_edge_definition(edge_name: str) -> Optional[dict]:
    """Full edge definition including source_types, target_types, inverse."""
    _load_packs()
    if not _EDGE_TYPES:
        return None
    return _EDGE_TYPES.get("edges", {}).get(edge_name)


def is_symmetric_edge(edge_name: str) -> bool:
    """True if edgevocab declares this relationship symmetric (undirected).
    Symmetric edges must have their endpoints canonicalized (sorted) before
    hashing the edge_id, so A→B and B→A collapse to ONE edge and two nodes that
    saw the pair in opposite order produce the same graph."""
    d = get_edge_definition(edge_name)
    return bool(d and d.get("symmetric"))


def get_category_index(category: str) -> int:
    """Base-60 first digit for a category. Returns -1 if invalid."""
    return _CATEGORY_INDEX.get(category, -1)


def get_subtype_index(category: str, subtype: str) -> int:
    """Base-60 second digit for a subtype within its category. Returns -1 if invalid."""
    _load_packs()
    cat_index = _SUBTYPE_INDEX.get(category, {})
    return cat_index.get(subtype, -1)


def get_category_for_subtype(subtype: str) -> Optional[str]:
    """Reverse lookup: given a subtype name, find which category it belongs to.
    Returns None if the subtype doesn't exist in any category."""
    _load_packs()
    for cat_name, subs in _SUBTYPE_ORDERS.items():
        if subtype in subs:
            return cat_name
    return None


def is_valid_node_type(category: str, subtype: str) -> bool:
    """Check if (category, subtype) is a valid position in the GDU grid."""
    _load_packs()
    if category not in _CATEGORY_INDEX:
        return False
    return subtype in _SUBTYPE_ORDERS.get(category, [])


def is_valid_edge(relationship: str, source_category: str, target_category: str) -> bool:
    """Check if an edge relationship is valid between the given node categories."""
    defn = get_edge_definition(relationship)
    if not defn:
        return False
    source_types = defn.get("source_types", [])
    target_types = defn.get("target_types", [])
    return source_category in source_types and target_category in target_types


def reset():
    """Reset cached state (testing only)."""
    global _NODE_TAXONOMY, _EDGE_TYPES, _SLOT_SPECS, _INITIALIZED
    global _SUBTYPE_ORDERS, _SUBTYPE_INDEX
    _NODE_TAXONOMY = None
    _EDGE_TYPES = None
    _SLOT_SPECS = None
    _SUBTYPE_ORDERS = {}
    _SUBTYPE_INDEX = {}
    _INITIALIZED = False
