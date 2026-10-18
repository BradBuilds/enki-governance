"""
GDU Base-60 Address Assignment

Assigns a unique base-60 address to every node at GDU admission time.
The address encodes the node's ontological position:

    [category_digit].[subtype_digit].[instance_sequence]

- Category digit: 0-8 (fixed, from CATEGORY_ORDER in pack_registry)
- Subtype digit: 0-N (fixed, from subtype order within category in nodevocab)
- Instance sequence: incrementing counter per (category, subtype) pair

Example: Entity→Person instance #47 = "0.0.47"
         Assessment→Claim instance #3 = "7.2.3"
         Event→Strike instance #100 = "2.0.100"

Addresses are permanent. Once assigned, never reused, never renumbered.
The sequence counter is atomically incremented via PostgreSQL to prevent
collisions under concurrent writes.
"""

from __future__ import annotations

import logging
from typing import Optional

from core.gdu.pack_registry import (
    get_category_index,
    get_subtype_index,
    is_valid_node_type,
)

_log = logging.getLogger("enki.gdu.address")


def assign_address(conn, category: str, subtype: str) -> Optional[str]:
    """Atomically assign the next base-60 address for a (category, subtype) pair.

    Uses PostgreSQL UPDATE ... RETURNING to atomically increment the sequence
    counter and return the new value. This is safe under concurrent writes.

    Args:
        conn: Active database connection (must be in a transaction)
        category: Node category (e.g., "entity", "assessment")
        subtype: Node subtype (e.g., "person", "claim")

    Returns:
        Address string like "0.0.47" or None if invalid category/subtype
    """
    cat_idx = get_category_index(category)
    sub_idx = get_subtype_index(category, subtype)

    if cat_idx < 0 or sub_idx < 0:
        _log.warning("Invalid address request: category=%s subtype=%s", category, subtype)
        return None

    with conn.cursor() as cur:
        # Atomically increment and return the sequence
        cur.execute("""
            UPDATE gdu_address_sequences
            SET next_seq = next_seq + 1, updated_at = NOW()
            WHERE category = %s AND subtype = %s
            RETURNING next_seq - 1
        """, (category, subtype))

        row = cur.fetchone()
        if not row:
            # Sequence row doesn't exist — create it (first assignment for this slot)
            cur.execute("""
                INSERT INTO gdu_address_sequences (category, subtype, next_seq)
                VALUES (%s, %s, 2)
                ON CONFLICT (category, subtype) DO UPDATE
                SET next_seq = gdu_address_sequences.next_seq + 1, updated_at = NOW()
                RETURNING next_seq - 1
            """, (category, subtype))
            row = cur.fetchone()

        if not row:
            _log.error("Failed to assign address for %s.%s", category, subtype)
            return None

        seq = row[0]

    address = f"{cat_idx}.{sub_idx}.{seq}"
    return address


def validate_address(address: str) -> bool:
    """Check if an address string points to a valid position in the GDU grid.

    Validates that the category and subtype digits correspond to real
    entries in nodevocab. Does NOT check if the sequence number exists.
    """
    parts = parse_address(address)
    if parts is None:
        return False

    cat_idx, sub_idx, _ = parts

    # Check category is in range
    from core.gdu.pack_registry import CATEGORY_ORDER, get_valid_subtypes
    if cat_idx < 0 or cat_idx >= len(CATEGORY_ORDER):
        return False

    category = CATEGORY_ORDER[cat_idx]
    subtypes = get_valid_subtypes(category)
    if not subtypes:
        return False

    # Check subtype is in range
    from core.gdu.pack_registry import _SUBTYPE_ORDERS
    ordered = _SUBTYPE_ORDERS.get(category, [])
    if sub_idx < 0 or sub_idx >= len(ordered):
        return False

    return True


def parse_address(address: str) -> Optional[tuple[int, int, int]]:
    """Parse an address string into (category_digit, subtype_digit, sequence).

    Returns None if the address format is invalid.
    """
    if not address or not isinstance(address, str):
        return None

    parts = address.split(".")
    if len(parts) != 3:
        return None

    try:
        cat = int(parts[0])
        sub = int(parts[1])
        seq = int(parts[2])
        return (cat, sub, seq)
    except (ValueError, TypeError):
        return None


def address_to_category_subtype(address: str) -> Optional[tuple[str, str]]:
    """Convert an address string back to (category_name, subtype_name).

    Returns None if the address doesn't map to valid vocabulary entries.
    """
    parts = parse_address(address)
    if parts is None:
        return None

    cat_idx, sub_idx, _ = parts

    from core.gdu.pack_registry import CATEGORY_ORDER, _SUBTYPE_ORDERS
    if cat_idx < 0 or cat_idx >= len(CATEGORY_ORDER):
        return None

    category = CATEGORY_ORDER[cat_idx]
    ordered = _SUBTYPE_ORDERS.get(category, [])
    if sub_idx < 0 or sub_idx >= len(ordered):
        return None

    return (category, ordered[sub_idx])


def render_address_glyphs(address: str, style: str = "wedge_ascii") -> str:
    """Render a base-60 address as cuneiform glyphs for human inspection.

    Example: "0.0.47" → "∅ ∅ <<<<|||||||"
             "7.2.3"  → "|||||||  << |||"
    """
    parts = parse_address(address)
    if parts is None:
        return address  # Return raw string if unparseable

    from core.validation.render_glyphs import render_places
    return render_places(list(parts), style)
