"""Shared node-redirect helpers for non-entity substrate routes.

Mirrors ``_entity_redirect.py`` but works against any substrate table
that carries an envelope JSONB column (topics, places, future tables).
Events / documents / chronicle_entries don't have envelope and don't
cross-categorize today, so they stay on the plain 404 path.

Usage:
    from services.api.src._node_redirect import resolve_node_or_410
    resolve_node_or_410(conn, table="topics", id_col="topic_id", id_value=topic_id)

Raises HTTPException(410) if envelope.reclassification is present, with
the same body shape as _entity_redirect so the UI's existing 410
handler works unchanged.
"""
from __future__ import annotations

from fastapi import HTTPException

# Explicit whitelist of (table, id_col) pairs callers may use. Prevents
# accidental injection via untrusted route input.
_ALLOWED = {
    ("topics", "topic_id"),
    ("places", "place_id"),
    ("entities", "entity_id"),
}


def resolve_node_or_410(conn, *, table: str, id_col: str, id_value: int) -> int:
    """Check envelope.reclassification on (table, id_value). Raise 410
    Gone with full reclassification context if reclassified. Otherwise
    return the original id_value unchanged.

    Unlike resolve_entity_or_410, this does NOT walk a redirect chain —
    only entities have entity_redirects today. If/when topics or places
    grow merge chains, add a node_redirects table and follow the same
    pattern.
    """
    pair = (table, id_col)
    if pair not in _ALLOWED:
        # Refuse unknown table/column pairs — caller bug, not a runtime
        # condition the user should see.
        raise ValueError(f"resolve_node_or_410: unsupported pair {pair}")

    sql = f"""
        SELECT envelope ? 'reclassification' AS has_reclass,
               envelope->'reclassification'->>'reclassified_to',
               envelope->'reclassification'->>'target_category',
               envelope->'reclassification'->>'target_subtype',
               envelope->'reclassification'->>'reclassification_pending',
               envelope->'reclassification'->>'action',
               envelope->'reclassification'->>'reason'
          FROM {table}
         WHERE {id_col} = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (id_value,))
        row = cur.fetchone()
    if row and row[0]:
        _, reclassed_to, target_cat, target_sub, pending, action, reason = row
        raise HTTPException(
            status_code=410,
            detail={
                "reason": f"{table[:-1]}_reclassified",  # 'topic_reclassified' etc.
                "id": id_value,
                "reclassified_to": reclassed_to,
                "target_category": target_cat,
                "target_subtype": target_sub,
                "pending": pending == "true",
                "sweep_action": action,
                "sweep_reason": reason,
            },
        )
    return id_value


# Reusable WHERE-clause fragments for list/aggregation queries that
# need to hide reclassified rows without raising 410 on the whole call.
TOPIC_NOT_RECLASSIFIED = "NOT (COALESCE(envelope, '{}'::jsonb) ? 'reclassification')"
PLACE_NOT_RECLASSIFIED = "NOT (COALESCE(envelope, '{}'::jsonb) ? 'reclassification')"
