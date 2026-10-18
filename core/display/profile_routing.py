"""
Per-entity-type display field routing.

Same algorithmic pattern as `core.research.source_routing`: a single
pack-driven map from canonical_type (and optional subtype) to an
ordered list of structured-profile field keys that should be surfaced
on the entity dossier card.

Reads `libraries/displayvocab/definitions/displayvocab.type_profiles.json`
and exposes:

    fields_for_type(canonical_type, subtype=None) -> list[str]
    label_for_field(field_key)                    -> str
    kind_for_field(field_key)                     -> str | None
    project_profile(canonical_type, subtype, data) -> list[dict]

`project_profile` is the main entry: given the raw `data` jsonb from a
Wikidata/Wikipedia structured_profile finding, it returns a list of
{key, label, value, kind} dicts in display order, dropping any fields
that don't apply to this type AND any fields where the value is empty.

Adding a new entity type means editing the pack JSON — no Python
change. Adding a new field key to the universe of known fields means
optionally adding a row to display_label_map / display_kind_map for
pretty rendering, but the loader degrades gracefully when neither is
present (falls back to the raw key as the label and 'text' as the
kind).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

log = logging.getLogger("enki.display.profile_routing")

_PACK: Optional[dict] = None


def _load_pack() -> dict:
    global _PACK
    if _PACK is not None:
        return _PACK
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    pack_path = os.path.join(
        repo_root, "libraries", "displayvocab",
        "definitions", "displayvocab.type_profiles.json",
    )
    try:
        with open(pack_path, "r") as f:
            data = json.load(f)
        _PACK = data.get("content") or {}
        log.info(
            "displayvocab: loaded %d type profiles",
            len((_PACK.get("type_profiles") or {})),
        )
    except Exception as e:
        log.warning("displayvocab: failed to load (%s); using empty pack", e)
        _PACK = {"type_profiles": {}, "display_label_map": {}, "display_kind_map": {}}
    return _PACK


def fields_for_type(canonical_type: Optional[str],
                     subtype: Optional[str] = None) -> list[str]:
    """Return the ordered field-key list for this type.

    Tries (canonical_type.subtype) first, falls back to canonical_type,
    then to '_default'. Returns an empty list only if the pack is empty.
    """
    profiles = (_load_pack().get("type_profiles") or {})
    ct = (canonical_type or "").lower().strip()
    st = (subtype or "").lower().strip()
    if ct and st:
        key = f"{ct}.{st}"
        fields = profiles.get(key)
        if fields:
            return list(fields)
    if ct:
        fields = profiles.get(ct)
        if fields:
            return list(fields)
    return list(profiles.get("_default") or [])


def label_for_field(field_key: str) -> str:
    """Pretty label for a field. Falls back to a title-cased version
    of the raw key with underscores stripped."""
    labels = (_load_pack().get("display_label_map") or {})
    if field_key in labels:
        return labels[field_key]
    return field_key.replace("_", " ").strip().title()


def kind_for_field(field_key: str) -> str:
    """Returns a render hint — 'url' | 'coords' | 'date' | 'datetime' |
    'year' | 'number' | 'money' | 'text'. The UI uses this to pick a
    renderer (e.g. wrap URLs in an <a>, format big numbers, etc.)."""
    kinds = (_load_pack().get("display_kind_map") or {})
    return kinds.get(field_key) or "text"


def _is_empty_value(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str) and not v.strip():
        return True
    if isinstance(v, (list, dict)) and len(v) == 0:
        return True
    return False


def project_profile(
    canonical_type: Optional[str],
    subtype: Optional[str],
    data: Optional[dict],
) -> list[dict]:
    """Given a Wikidata structured_profile finding's `data` blob, return
    the displayable fields in display order. Each entry is a dict:
       {"key": str, "label": str, "value": Any, "kind": str}

    Fields missing from the data, or with empty values, are dropped.
    The output is bounded only by the length of the type's field list."""
    if not isinstance(data, dict):
        return []
    out: list[dict] = []
    for key in fields_for_type(canonical_type, subtype):
        if key not in data:
            continue
        value = data[key]
        if _is_empty_value(value):
            continue
        out.append({
            "key": key,
            "label": label_for_field(key),
            "value": value,
            "kind": kind_for_field(key),
        })
    return out


def reset_cache() -> None:
    """Drop the cached pack so the next call reloads from disk."""
    global _PACK
    _PACK = None
