"""LDU subtype validator — write-time gate for canonical_type values.

Loads libraries/entityvocab/definitions/entityvocab.entity_subtypes.json
(the closed vocabulary) and provides validate_for_ldu() which is called
by admit_candidate BEFORE any concept/entity row is created. This is
the LDU side of the LDU/GDU lock-down (the GDU side is the
trg_enforce_entities_governance trigger).

Validation outcomes:
  • accept(canonical_subtype)         — type is valid (canonical or alias hit)
  • route_to_other_category(cat,sub)  — type belongs in a different table
                                        (e.g. 'gpe' → ('place','named_area'))
  • drop(reason)                      — type is pure LLM/NER garbage
                                        ('other', 'misc', 'abstract', etc.)
  • unknown(raw_type)                 — type is not in any list; caller
                                        decides whether to route to LDU-P or reject

The module re-reads the JSON on import + on demand (lru_cache with a
short TTL). It tolerates the file being missing — in that case every
type is treated as 'unknown' and the caller defaults to LDU-P.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("enki.ldu.subtype_validator")

_DEF_PATH_CANDIDATES = [
    Path("/app/libraries/entityvocab/definitions/entityvocab.entity_subtypes.json"),
    Path("libraries/entityvocab/definitions/entityvocab.entity_subtypes.json"),
    Path("/opt/enki/app/libraries/entityvocab/definitions/entityvocab.entity_subtypes.json"),
]


@dataclass(frozen=True)
class SubtypeValidationResult:
    """Outcome of validate_for_ldu()."""
    action: str                                 # "accept" | "route" | "drop" | "unknown"
    canonical_subtype: Optional[str] = None     # set when action=accept
    route_to: Optional[tuple[str, str]] = None  # (target_category, target_subtype) when action=route
    raw_type: str = ""                          # what the caller passed in
    reason: str = ""                            # human-readable explanation


# ── State (refreshed on a TTL so def-file edits don't require restart) ─────

_DEFINITIONS: dict = {}
_LOADED_AT: float = 0.0
_TTL_SECONDS: float = 60.0


def _try_load() -> dict:
    """Read the entity_subtypes def file from the first candidate path that
    exists. Returns the parsed dict, or {} if no file found / parse error."""
    for p in _DEF_PATH_CANDIDATES:
        try:
            if p.exists():
                with p.open() as f:
                    data = json.load(f)
                return data.get("content", data)
        except Exception as e:
            log.warning("entity_subtypes load from %s failed: %s", p, e)
    log.warning("entity_subtypes.json not found in any candidate path")
    return {}


def _try_load_overrides() -> list[dict]:
    """Read runtime decisions from entityvocab_overrides. Most recent row
    per suspected_type wins. Silently returns [] if the DB / table is
    unreachable so a vocab read never blocks on infrastructure."""
    try:
        from storage.postgres.db import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT ON (LOWER(suspected_type))
                           suspected_type, decision,
                           target_category, target_subtype, canonical_subtype
                      FROM entityvocab_overrides
                     ORDER BY LOWER(suspected_type), decided_at DESC
                """)
                cols = ("suspected_type","decision","target_category","target_subtype","canonical_subtype")
                return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:
        return []


def _merge_overrides(defs: dict, overrides: list[dict]) -> dict:
    """Layer operator decisions on top of the JSON-defined vocab. Returns
    a NEW dict — never mutates the cached JSON."""
    if not overrides:
        return defs
    merged = {
        "subtypes": dict(defs.get("subtypes", {})),
        "cross_category_routes": {"routes": dict((defs.get("cross_category_routes") or {}).get("routes", {}))},
        "drop_types": {"list": list((defs.get("drop_types") or {}).get("list", []))},
    }
    for o in overrides:
        key = (o.get("suspected_type") or "").strip().lower()
        if not key:
            continue
        decision = o.get("decision")
        if decision == "route":
            cat, sub = o.get("target_category"), o.get("target_subtype")
            if cat and sub:
                merged["cross_category_routes"]["routes"][key] = [cat, sub]
        elif decision == "drop":
            if key not in (d.lower() for d in merged["drop_types"]["list"]):
                merged["drop_types"]["list"].append(key)
        elif decision == "alias":
            canonical = o.get("canonical_subtype")
            if canonical and canonical in merged["subtypes"]:
                aliases = merged["subtypes"][canonical].setdefault("aliases", [])
                if key not in (a.lower() for a in aliases):
                    aliases.append(key)
        # decision == "accept" doesn't change the vocab — the suspected_type
        # must already be a known alias of the canonical_subtype, or the
        # admit path needs to set canonical_subtype explicitly.
    return merged


def _definitions() -> dict:
    """Load (or refresh) the definitions. Cheap on cache-hit."""
    global _DEFINITIONS, _LOADED_AT
    now = time.time()
    if not _DEFINITIONS or now - _LOADED_AT > _TTL_SECONDS:
        base = _try_load()
        overrides = _try_load_overrides()
        _DEFINITIONS = _merge_overrides(base, overrides) if overrides else base
        _LOADED_AT = now
    return _DEFINITIONS


# ── Index helpers ───────────────────────────────────────────────────────────

def _build_alias_index(defs: dict) -> dict[str, str]:
    """Map (lowercased alias OR ner_tag OR canonical) → canonical_subtype."""
    idx: dict[str, str] = {}
    subtypes = defs.get("subtypes", {})
    for canonical, spec in subtypes.items():
        idx[canonical.lower()] = canonical
        for alias in spec.get("aliases", []):
            idx[alias.lower()] = canonical
        for tag in spec.get("ner_tags", []):
            idx[tag.lower()] = canonical
    return idx


def _build_route_index(defs: dict) -> dict[str, tuple[str, str]]:
    """Map (lowercased subtype) → (target_category, target_subtype)."""
    routes = defs.get("cross_category_routes", {}).get("routes", {})
    return {k.lower(): tuple(v) for k, v in routes.items()}


def _drop_set(defs: dict) -> set[str]:
    return {t.lower() for t in defs.get("drop_types", {}).get("list", [])}


# ── Public API ──────────────────────────────────────────────────────────────

def validate_for_ldu(raw_type: Optional[str]) -> SubtypeValidationResult:
    """Validate a proposed canonical_type for entry into the entities table.

    Decision tree:
      1. None/empty → unknown
      2. Lookup in alias index → accept(canonical)
      3. Lookup in cross_category_routes → route(target_cat, target_sub)
      4. Lookup in drop_types → drop
      5. Otherwise → unknown (caller decides LDU-P vs reject)
    """
    if not raw_type:
        return SubtypeValidationResult(
            action="unknown",
            raw_type="",
            reason="empty type",
        )
    key = raw_type.strip().lower()
    defs = _definitions()

    alias_idx = _build_alias_index(defs)
    if key in alias_idx:
        canonical = alias_idx[key]
        return SubtypeValidationResult(
            action="accept",
            canonical_subtype=canonical,
            raw_type=raw_type,
            reason=f"alias hit → {canonical}",
        )

    route_idx = _build_route_index(defs)
    if key in route_idx:
        target = route_idx[key]
        return SubtypeValidationResult(
            action="route",
            route_to=target,
            raw_type=raw_type,
            reason=f"cross-category route → {target[0]}/{target[1]}",
        )

    if key in _drop_set(defs):
        return SubtypeValidationResult(
            action="drop",
            raw_type=raw_type,
            reason="garbage subtype",
        )

    return SubtypeValidationResult(
        action="unknown",
        raw_type=raw_type,
        reason="not in entity pack, not in cross_category_routes, not in drop_types",
    )


def safe_canonical_type(
    proposed: Optional[str],
    current: Optional[str] = None,
) -> Optional[str]:
    """Helper for UPDATE callsites that may overwrite entities.canonical_type
    with externally-sourced values (Wikidata, LLM-validator, etc.).

    Returns a value that is safe to write back to the column:
      - validator accepts proposed → its canonical alias
        (e.g. 'org' → 'organization')
      - validator routes / drops / unknowns proposed → keep ``current``
        unchanged (do not overwrite the row with a non-admittable type)
      - both are bad → None (caller should skip touching the column)

    Use in SQL like ``SET canonical_type = COALESCE(%s, canonical_type)``
    paired with ``safe_canonical_type(new_type, existing_type)`` as the
    parameter; or guard the whole UPDATE on a non-None return.
    """
    if proposed:
        v = validate_for_ldu(proposed)
        if v.action == "accept":
            return v.canonical_subtype or proposed
    return current if current else None


def valid_entity_subtypes() -> list[str]:
    """Return the canonical list of valid entity subtypes (for DB CHECK
    constraint generation, harness checks, etc.)."""
    return sorted((_definitions().get("subtypes") or {}).keys())


def classification_fingerprint() -> str:
    """Deterministic fingerprint of EVERYTHING that decides how this node maps a
    raw type to a canonical_type: the entityvocab pack content PLUS any local
    `entityvocab_overrides`. Two nodes with the same fingerprint classify
    identically; a mismatch is the precise signal that their collapse will
    diverge. Federation compares this (not just the pack version) because an
    operator override is a node-local divergence the pack version can't see.

    Stable across processes: hashes the sorted, canonical-JSON of the merged
    definitions actually in effect.
    """
    import hashlib
    import json as _json
    defs = _definitions()
    payload = {
        "subtypes": {k: sorted((v.get("aliases") or []) + (v.get("ner_tags") or []))
                     for k, v in (defs.get("subtypes") or {}).items()},
        "routes": (defs.get("cross_category_routes") or {}).get("routes", {}),
        "drops": sorted((defs.get("drop_types") or {}).get("list", [])),
    }
    blob = _json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def override_count() -> int:
    """How many local entityvocab_overrides are layered on top of the pack.
    >0 means this node has operator customizations that aren't in the versioned
    pack — a divergence source until they're promoted into pack content."""
    return len(_try_load_overrides())


def reload() -> None:
    """Force re-read on next call (test/debug helper)."""
    global _LOADED_AT
    _LOADED_AT = 0
