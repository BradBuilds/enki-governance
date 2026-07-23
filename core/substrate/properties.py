"""Property concept lookups + type compatibility checks.

Properties live in the `properties` table seeded from propertyvocab. This
module wraps lookups and applies the type-compatibility rules from
propertyvocab.properties.json:

  * applicable_to       — which nodevocab categories can carry this property
  * applicable_subtypes — optional finer-grained subtype restriction
  * precision_tiers     — which precisionvocab tiers this property accepts

Type errors here are the GDU's gatekeeping for the algebra. A "color"
assertion on a "person" entity is structurally invalid; the substrate
refuses it at this layer before any collapse runs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from storage.postgres.db import get_connection


class PropertyTypeError(ValueError):
    """A property assertion violated propertyvocab type rules."""


@dataclass
class Property:
    property_id: int
    property_key: str
    cycle: int
    label: str
    applicable_to: list[str]
    applicable_subtypes: list[str]
    precision_tiers: list[str]
    units: Optional[str]
    deprecated: bool
    replaced_by: Optional[int]


_CACHE: dict[str, Property] = {}


def _row_to_property(row) -> Property:
    return Property(
        property_id=row[0],
        property_key=row[1],
        cycle=row[2],
        label=row[3],
        applicable_to=list(row[4] or []),
        applicable_subtypes=[],
        precision_tiers=list(row[5] or []),
        units=row[6],
        deprecated=bool(row[7]),
        replaced_by=row[8],
    )


def get_by_key(key: str) -> Optional[Property]:
    """Look up a property by its key (e.g. 'color', 'manufacturer').
    Cached process-locally — propertyvocab is immutable within a pack
    version so caching is safe until the next ceremony."""
    if key in _CACHE:
        return _CACHE[key]
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT property_id, property_key, cycle, label,
                          applicable_to, precision_tiers, units,
                          deprecated, replaced_by
                     FROM properties
                    WHERE property_key = %s""",
                (key,),
            )
            row = cur.fetchone()
    if not row:
        return None
    prop = _row_to_property(row)
    _CACHE[key] = prop
    return prop


def get_by_id(property_id: int) -> Optional[Property]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT property_id, property_key, cycle, label,
                          applicable_to, precision_tiers, units,
                          deprecated, replaced_by
                     FROM properties WHERE property_id = %s""",
                (property_id,),
            )
            row = cur.fetchone()
    return _row_to_property(row) if row else None


def is_applicable(prop: Property, node_category: str,
                  node_subtype: Optional[str] = None) -> bool:
    """Is this property valid for a node of this nodevocab category/subtype?

    Empty applicable_to means "any category" — but in practice every property
    in v0.1.0 declares at least one. Subtype restriction is finer-grained
    when present; if applicable_subtypes is empty, any subtype is allowed.
    """
    if prop.deprecated:
        return False
    if prop.applicable_to and node_category not in prop.applicable_to:
        return False
    if prop.applicable_subtypes and node_subtype:
        if node_subtype not in prop.applicable_subtypes:
            return False
    return True


def validate_precision(prop: Property, precision_tier: str) -> None:
    """Raise PropertyTypeError if the precision_tier is not allowed for this
    property. 'unspecified' is always allowed (LDU staging)."""
    if precision_tier == "unspecified":
        return
    if prop.precision_tiers and precision_tier not in prop.precision_tiers:
        raise PropertyTypeError(
            f"property {prop.property_key!r} does not accept precision_tier "
            f"{precision_tier!r}; valid tiers: {prop.precision_tiers}"
        )


# Precision tier → depth mapping. Mirrors precisionvocab.tiers depths.
PRECISION_DEPTH = {
    "unspecified": -1,
    "concept": 0,
    "named": 1,
    "qualified": 2,
    "formal": 3,
    "spectral": 4,
    "enumerated": 999,
}


def depth_for_tier(tier: str) -> int:
    """Get the canonical depth for a precision tier."""
    return PRECISION_DEPTH.get(tier, -1)
