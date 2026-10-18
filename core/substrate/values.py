"""Value lattice operations.

A value is a node in a property's lattice with:
  * property_id      — which lattice it lives in
  * parent_value_id  — refinement parent (NULL = top of property's lattice)
  * depth            — precision-tier depth (from precisionvocab)
  * label            — canonical name ('red', 'candy red', 'Pantone 199C')
  * normalized_label — lowercased, trimmed (for dedup)

Two values are in the SAME CHAIN when one is an ancestor of the other via
parent_value_id pointers. Two values at the same depth in the same property
with different normalized_labels are ALTERNATIVES — they can't both be true
of the same (node, property) simultaneously.

`find_or_create` is the main entry point: extractors pass a property + a
candidate label + a precision tier, and we either return an existing value
or admit a new one into the lattice.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from storage.postgres.db import get_connection
from core.substrate.properties import Property, depth_for_tier


@dataclass
class Value:
    value_id: int
    property_id: int
    parent_value_id: Optional[int]
    depth: int
    precision_tier: str
    label: str
    normalized_label: str
    formal_id: Optional[str]
    numeric_value: Optional[float]
    units: Optional[str]


def _normalize(label: str) -> str:
    return re.sub(r"\s+", " ", label or "").strip().lower()


def _row_to_value(row) -> Value:
    return Value(
        value_id=row[0],
        property_id=row[1],
        parent_value_id=row[2],
        depth=row[3],
        precision_tier=row[4],
        label=row[5],
        normalized_label=row[6],
        formal_id=row[7],
        numeric_value=float(row[8]) if row[8] is not None else None,
        units=row[9],
    )


def get_by_id(value_id: int) -> Optional[Value]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT value_id, property_id, parent_value_id, depth,
                          precision_tier, label, normalized_label,
                          formal_id, numeric_value, units
                     FROM property_values WHERE value_id = %s""",
                (value_id,),
            )
            row = cur.fetchone()
    return _row_to_value(row) if row else None


def find(prop: Property, label: str, depth: int) -> Optional[Value]:
    """Find an existing value in `prop`'s lattice at the given depth with the
    given normalized label. Returns None if not found."""
    norm = _normalize(label)
    if not norm:
        return None
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT value_id, property_id, parent_value_id, depth,
                          precision_tier, label, normalized_label,
                          formal_id, numeric_value, units
                     FROM property_values
                    WHERE property_id = %s
                      AND normalized_label = %s
                      AND depth = %s
                    LIMIT 1""",
                (prop.property_id, norm, depth),
            )
            row = cur.fetchone()
    return _row_to_value(row) if row else None


def find_or_create(
    prop: Property, label: str, precision_tier: str,
    *, parent_value_id: Optional[int] = None,
    formal_id: Optional[str] = None,
    numeric_value: Optional[float] = None,
    tenant_id: str = "default",
) -> Value:
    """Find an existing value or admit a new one into prop's lattice.

    Idempotent on (property_id, normalized_label, depth). Concurrent writes
    are protected by the UNIQUE constraint — second writer hits ON CONFLICT
    and re-fetches the existing row.
    """
    depth = depth_for_tier(precision_tier)
    if depth < 0 and precision_tier != "unspecified":
        raise ValueError(f"unknown precision_tier: {precision_tier!r}")

    existing = find(prop, label, depth)
    if existing:
        # Bump the mention counter — useful for downstream relevance signals
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE property_values
                          SET mention_count = mention_count + 1,
                              last_seen = NOW()
                        WHERE value_id = %s""",
                    (existing.value_id,),
                )
            conn.commit()
        return existing

    norm = _normalize(label)
    base60 = f"{prop.cycle}.{depth}.{prop.property_id}"  # cycle.depth.instance pattern
    node_id = f"value:{prop.property_key}:{norm.replace(' ', '_')[:32]}"

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO property_values (
                     tenant_id, property_id, parent_value_id, depth,
                     precision_tier, label, normalized_label,
                     formal_id, numeric_value, units,
                     base60_address, node_id, mention_count
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1)
                   ON CONFLICT (tenant_id, property_id, normalized_label, depth)
                   DO UPDATE SET mention_count = property_values.mention_count + 1,
                                  last_seen = NOW()
                   RETURNING value_id, property_id, parent_value_id, depth,
                             precision_tier, label, normalized_label,
                             formal_id, numeric_value, units""",
                (tenant_id, prop.property_id, parent_value_id, depth,
                 precision_tier, label, norm,
                 formal_id, numeric_value, prop.units,
                 base60, node_id),
            )
            row = cur.fetchone()
        conn.commit()
    return _row_to_value(row)


def walk_chain(value_id: int) -> list[Value]:
    """Walk from a value up to the root of its lattice. Returns the chain
    deepest-first (the value itself, then its parent, then grandparent, …)."""
    chain: list[Value] = []
    seen: set[int] = set()
    current = get_by_id(value_id)
    while current and current.value_id not in seen:
        chain.append(current)
        seen.add(current.value_id)
        if current.parent_value_id is None:
            break
        current = get_by_id(current.parent_value_id)
    return chain


def is_ancestor(ancestor_id: int, descendant_id: int) -> bool:
    """Is `ancestor_id` an ancestor of `descendant_id` in the value lattice?"""
    if ancestor_id == descendant_id:
        return False
    chain = walk_chain(descendant_id)
    return any(v.value_id == ancestor_id for v in chain)


def in_same_chain(value_a_id: int, value_b_id: int) -> bool:
    """True iff value_a and value_b are in the same refinement chain
    (one is an ancestor of the other, or they're the same value)."""
    if value_a_id == value_b_id:
        return True
    return is_ancestor(value_a_id, value_b_id) or is_ancestor(value_b_id, value_a_id)
