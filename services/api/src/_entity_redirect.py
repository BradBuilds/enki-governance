"""Shared entity-redirect helpers for API routes.

Two redirect dimensions:
  1. entity_redirects (same-table merge): old_entity_id → new_entity_id.
     Walked up to 5 hops to handle merge chains.
  2. Cross-category reclassification (entity → place / topic / event /
     etc.): tracked via envelope.reclassification.{reclassified_to,
     target_category, target_subtype, reclassification_pending}.

Use ``resolve_entity_or_410`` in any route that fetches by entity_id
and returns dossier-style data. The 410 response carries the target
substrate info so the UI can navigate to /places/X, /topics/X, etc.

For listing/aggregation endpoints (feed counts, dropdowns, neighbor
graphs), use ``entity_not_reclassified_clause`` to filter the rows
out at SELECT time — these endpoints shouldn't return a hard 410 for
the whole response, just hide the ghost row.
"""
from __future__ import annotations

from fastapi import HTTPException


def follow_entity_redirect(conn, entity_id: int) -> int:
    """Walk the entity_redirects chain (max 5 hops). Returns the canonical
    entity_id at the end of the chain (which may be the original)."""
    current = entity_id
    for _ in range(5):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT new_entity_id FROM entity_redirects WHERE old_entity_id = %s",
                (current,),
            )
            r = cur.fetchone()
        if not r or r[0] == current:
            break
        current = r[0]
    return current


def resolve_entity_or_410(conn, entity_id: int) -> int:
    """Resolve entity_id through same-table merges AND surface cross-
    category reclassifications as HTTP 410 Gone.

    Three reclassification shapes the helper recognises:
      1. Tier-2 migration to another substrate (e.g. place:5432) —
         carries reclassified_to + target_category + target_subtype.
      2. Tier-1 marker (target_category set, pending=true, no
         target_id yet) — substrate migrator not yet built.
      3. Decontamination-sweep tombstones (action=unmapped_residual
         or action=dropped_garbage from 2026-05-30 round2 sweeps) —
         envelope has reclassification metadata but no target_category.
         These are 'we tried; no good target' rows; should not
         render as a dossier.

    Raises HTTPException(410) for ALL three. Response body carries
    whichever fields are populated so the UI handler can route the
    user appropriately or show a 'this row was reclassified as
    <reason>, no dossier exists' placeholder.
    """
    current = follow_entity_redirect(conn, entity_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT envelope ? 'reclassification' AS has_reclass,
                   envelope->'reclassification'->>'reclassified_to',
                   envelope->'reclassification'->>'target_category',
                   envelope->'reclassification'->>'target_subtype',
                   envelope->'reclassification'->>'reclassification_pending',
                   envelope->'reclassification'->>'action',
                   envelope->'reclassification'->>'reason'
              FROM entities
             WHERE entity_id = %s
            """,
            (current,),
        )
        row = cur.fetchone()
    if row:
        has_reclass, reclassed_to, target_cat, target_sub, pending, action, reason = row
        if has_reclass:
            raise HTTPException(
                status_code=410,
                detail={
                    "reason": "entity_reclassified",
                    "entity_id": current,
                    "reclassified_to": reclassed_to,
                    "target_category": target_cat,
                    "target_subtype": target_sub,
                    "pending": pending == "true",
                    "sweep_action": action,        # unmapped_residual | dropped_garbage | None
                    "sweep_reason": reason,
                },
            )
    return current


def is_entity_reclassified(conn, entity_id: int) -> bool:
    """Cheap boolean check: has this entity been reclassified?

    For use in routes that need to short-circuit non-fatally (e.g.
    refuse to merge into a reclassified target) without raising 410.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT (envelope ? 'reclassification') FROM entities WHERE entity_id = %s",
            (entity_id,),
        )
        row = cur.fetchone()
    return bool(row and row[0])


# SQL fragment that callers can paste into a WHERE clause to filter out
# reclassified rows from list/aggregation queries. Self-contained so the
# fragment can drop into any query touching the entities table.
ENTITY_NOT_RECLASSIFIED = "NOT (COALESCE(envelope, '{}'::jsonb) ? 'reclassification')"
