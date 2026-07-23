"""
Shared resolver helpers: normalization, identifier hashing, type mapping.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Optional

# Suffixes stripped during name normalization (organizations mostly).
_ORG_SUFFIXES = (
    " inc", " inc.", " incorporated",
    " ltd", " ltd.", " limited",
    " llc", " l.l.c.",
    " corp", " corp.", " corporation",
    " co", " co.", " company",
    " gmbh", " ag", " sa", " s.a.", " plc",
    " holdings", " group",
)

_WHITESPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s\-']", re.UNICODE)


def normalize_name(name: str) -> str:
    """Soft dedup key for entities / places / topics.

    Phase 2.4: this thin wrapper delegates to
    ``core.gdu.normalize.normalize`` so all writers normalize the same
    way. Falls back to the legacy local logic only if the pack is
    unreachable (test environments, partial installs).
    """
    if not name:
        return ""
    try:
        from core.gdu.normalize import normalize as _gdu_norm
        result = _gdu_norm(name, role="name")
        return result.normalized
    except Exception:
        s = name.strip().lower()
        s = _PUNCT.sub(" ", s)
        s = _WHITESPACE.sub(" ", s).strip()
        for suf in _ORG_SUFFIXES:
            if s.endswith(suf):
                s = s[: -len(suf)].strip()
                break
        return s


def compute_identifier_hash(identifiers: dict) -> Optional[str]:
    """Deterministic SHA-256 over a sorted JSON dump of the identifiers
    dict. Returns None if the dict is empty. Used as the strong dedup
    key for entities with structured identifiers (MMSI, email, EIN, ...).
    """
    if not identifiers:
        return None
    # Lowercase all string values for canonical comparison
    normalized = {
        k: (v.lower().strip() if isinstance(v, str) else v)
        for k, v in sorted(identifiers.items())
    }
    blob = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# Identifier types that map to real-world entities and deserve an
# entity row. Loaded from identifier_types.yaml — hand-editable, no
# code change needed to extend.
def _load_identifier_type_map() -> dict[str, str]:
    from pathlib import Path
    import yaml as _yaml
    p = Path(__file__).parent / "identifier_types.yaml"
    if not p.exists():
        return {}
    try:
        with open(p) as f:
            data = _yaml.safe_load(f) or {}
        return {str(k): str(v) for k, v in data.items() if v}
    except Exception:
        return {}


_IDENT_TO_ENTITY_TYPE: dict[str, str] = _load_identifier_type_map()


def canonical_type_for_identifier(identifier_type: Optional[str]) -> Optional[str]:
    """Map an observable/identifier type id to the canonical entity
    type id (nodevocab entity subtype). Returns None for identifier
    types that should NOT become entity rows."""
    if not identifier_type:
        return None
    return _IDENT_TO_ENTITY_TYPE.get(identifier_type)


def is_resolvable_entity(entity: dict) -> bool:
    """Return True if this envelope-entity dict should be inserted into
    the entities master table. Applied by the entity resolver as a
    pre-filter."""
    ident_type = entity.get("identifier_type")
    if ident_type and canonical_type_for_identifier(ident_type):
        return True
    # Entities without an identifier type need a non-empty name_raw to
    # be worth resolving. Phase 4 (LLM NER) will produce these.
    name = (entity.get("name_raw") or "").strip()
    return len(name) >= 2 and not name.startswith("http")
