"""
Manual classification override endpoints — Phase 5C.

Every user action here writes TWO rows:
  * kb_user_contributions  — the RULE (used by validator + resolver going forward)
  * manual_override_log     — the EVENT (audit trail + undo)

Authority: user contributions win over upstream providers. Once a user
reclassifies "Strait of Hormuz" as strait, Wikidata can't silently retype
it on the next re-validation — the validator's Layer C lookup will return
the user's contribution first.

Endpoints (all under /nodes/:node_id):
  POST /reclassify — change type (+ optionally move to a different kind/table)
  POST /reject     — mark as not-a-real-entity
  POST /confirm    — human-approve the current auto-classification
  POST /unlock     — reopen for re-validation (clears validation_locked)

Every endpoint returns the new state so the UI can re-render in place.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Header
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

log = logging.getLogger("enki.overrides")
router = APIRouter(prefix="/nodes", tags=["overrides"])

ALLOWED_TYPES = {
    # People & orgs
    "person", "organization", "military_unit", "government_agency",
    "university", "mass_media", "research_institute",
    # Places — political
    "country", "city", "region",
    # Places — natural
    "strait", "bay", "gulf", "canal", "peninsula", "archipelago",
    "island", "island_group", "mountain", "mountain_range",
    "desert", "river", "lake", "sea", "ocean", "reef", "continent",
    # Infrastructure
    "airport", "seaport", "railway_station", "pipeline",
    "bridge", "dam", "power_plant", "refinery",
    # Equipment
    "aircraft", "helicopter", "airliner", "vessel", "vehicle",
    "weapon", "spacecraft", "unmanned_aerial_vehicle",
    # Abstract
    "event", "topic", "document", "chronicle", "assessment", "pattern",
    # Fallback
    "other",
}

# Which types live in which master table
_TYPE_TO_KIND = {t: "place" for t in {
    "country", "city", "region", "strait", "bay", "gulf", "canal",
    "peninsula", "archipelago", "island", "island_group",
    "mountain", "mountain_range", "desert", "river", "lake",
    "sea", "ocean", "reef", "continent", "airport", "seaport",
    "railway_station", "pipeline", "bridge", "dam",
    "power_plant", "refinery",
}}
_TYPE_TO_KIND.update({t: "entity" for t in {
    "person", "organization", "military_unit", "government_agency",
    "university", "mass_media", "research_institute",
    "aircraft", "helicopter", "airliner", "vessel", "vehicle",
    "weapon", "spacecraft", "unmanned_aerial_vehicle",
}})
_TYPE_TO_KIND.update({t: "topic" for t in {
    "topic", "event", "document", "chronicle", "assessment", "pattern", "other",
}})


def _parse_node_id(node_id: str) -> tuple[str, int]:
    """entity:42 → ('entity', 42). Raises 400 on invalid."""
    if ":" not in node_id:
        raise HTTPException(400, detail="invalid node_id format")
    prefix, _, rest = node_id.partition(":")
    kind_map = {"entity": "entity", "place": "place", "topic": "topic"}
    if prefix not in kind_map:
        raise HTTPException(400, detail=f"unsupported node kind: {prefix}")
    try:
        return kind_map[prefix], int(rest)
    except ValueError:
        raise HTTPException(400, detail=f"invalid id in node_id: {rest}")


def _table_for(kind: str) -> tuple[str, str]:
    return {
        "entity": ("entities", "entity_id"),
        "place":  ("places",   "place_id"),
        "topic":  ("topics",   "topic_id"),
    }[kind]


def _fetch_current(conn, kind: str, row_id: int, tenant_id: str) -> dict:
    table, pk = _table_for(kind)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {pk}, canonical_name, canonical_type, validation_status,
                   validated_by_source, validation_source_id, validation_locked,
                   tenant_id
              FROM {table}
             WHERE {pk} = %s AND tenant_id = %s
            """,
            (row_id, tenant_id),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, detail=f"{kind} {row_id} not found")
    return {
        "id": row[0], "canonical_name": row[1], "canonical_type": row[2],
        "validation_status": row[3], "validated_by_source": row[4],
        "validation_source_id": row[5], "validation_locked": row[6],
        "tenant_id": row[7],
    }


def _log_action(
    conn, *, tenant_id: str, user_id: str,
    target_kind: str, target_id: int, target_node_id: str,
    action: str, before: dict, after_type: Optional[str] = None,
    after_name: Optional[str] = None, after_status: Optional[str] = None,
    after_target_kind: Optional[str] = None, after_target_node_id: Optional[str] = None,
    note: Optional[str] = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO manual_override_log (
                tenant_id, user_id,
                target_kind, target_id, target_node_id,
                action,
                before_canonical_name, before_canonical_type, before_status,
                before_source, before_source_id,
                after_canonical_name, after_canonical_type, after_status,
                after_target_kind, after_target_node_id,
                note
            ) VALUES (
                %s, %s,
                %s, %s, %s,
                %s,
                %s, %s, %s,
                %s, %s,
                %s, %s, %s,
                %s, %s,
                %s
            )
            """,
            (
                tenant_id, user_id,
                target_kind, target_id, target_node_id,
                action,
                before.get("canonical_name"), before.get("canonical_type"),
                before.get("validation_status"),
                before.get("validated_by_source"), before.get("validation_source_id"),
                after_name, after_type, after_status,
                after_target_kind, after_target_node_id,
                note,
            ),
        )
    # caller is expected to commit


# ── POST /nodes/:id/reclassify ────────────────────────────────────────
@router.post("/{node_id}/reclassify")
async def reclassify(
    node_id: str,
    body: dict = Body(...),
    auth: UnifiedAuthInfo = Depends(unified_auth),
    x_user_id: str = Header("user", alias="X-User-ID"),
):
    """Change the type (and optionally the master table) of a node.
    Body: { new_type: str, note?: str }
    If new_type maps to a different kind than the current node, the
    caller should be aware a follow-up migration (Phase 5C.6) will
    re-point all edges; for now we just update type in place."""
    from storage.postgres.db import get_connection
    from core.entity_validation.cache import record_user_contribution

    new_type = (body.get("new_type") or "").strip().lower()
    note = (body.get("note") or "")[:1000]

    if new_type not in ALLOWED_TYPES:
        raise HTTPException(400, detail=f"new_type must be one of {sorted(ALLOWED_TYPES)}")

    kind, row_id = _parse_node_id(node_id)
    table, pk = _table_for(kind)

    with get_connection() as conn:
        before = _fetch_current(conn, kind, row_id, auth.tenant_id)

        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {table} SET
                    canonical_type         = %s,
                    validation_status      = 'human_validated',
                    validated_by_source    = %s,
                    validation_source_id   = NULL,
                    validation_source_url  = NULL,
                    validation_confidence  = 1.0,
                    validated_at           = NOW(),
                    validation_locked      = TRUE,
                    validation_last_error  = NULL,
                    updated_at             = NOW()
                 WHERE {pk} = %s AND tenant_id = %s
                """,
                (new_type, f"user:{x_user_id}", row_id, auth.tenant_id),
            )

        _log_action(
            conn, tenant_id=auth.tenant_id, user_id=x_user_id,
            target_kind=kind, target_id=row_id, target_node_id=node_id,
            action="classify", before=before,
            after_type=new_type, after_name=before["canonical_name"],
            after_status="human_validated", note=note,
        )

        # Record as a rule for future candidates with the same normalized name
        record_user_contribution(
            conn,
            user_id=x_user_id,
            raw_text=before["canonical_name"] or "",
            kind=kind,
            action="classify",
            canonical_name=before["canonical_name"],
            canonical_type=new_type,
            target_node_id=node_id,
            note=note,
            tenant_id=auth.tenant_id,
        )
        conn.commit()

        # Return the new state
        after = _fetch_current(conn, kind, row_id, auth.tenant_id)

    return {
        "status": "ok",
        "node_id": node_id,
        "before": {"canonical_type": before["canonical_type"],
                   "validation_status": before["validation_status"]},
        "after":  {"canonical_type": after["canonical_type"],
                   "validation_status": after["validation_status"],
                   "validated_by_source": after["validated_by_source"],
                   "validation_locked": after["validation_locked"]},
    }


# ── POST /nodes/:id/reject ────────────────────────────────────────────
@router.post("/{node_id}/reject")
async def reject(
    node_id: str,
    body: dict = Body(default={}),
    auth: UnifiedAuthInfo = Depends(unified_auth),
    x_user_id: str = Header("user", alias="X-User-ID"),
):
    """Mark as not-a-real-entity. Keeps the row for audit but hides
    from graph + feed. Locks so validator doesn't retry."""
    from storage.postgres.db import get_connection
    from core.entity_validation.cache import record_user_contribution

    note = (body.get("note") or "")[:1000]
    kind, row_id = _parse_node_id(node_id)
    table, pk = _table_for(kind)

    with get_connection() as conn:
        before = _fetch_current(conn, kind, row_id, auth.tenant_id)

        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {table} SET
                    validation_status      = 'rejected',
                    validated_by_source    = %s,
                    validation_locked      = TRUE,
                    validation_last_error  = %s,
                    updated_at             = NOW()
                 WHERE {pk} = %s AND tenant_id = %s
                """,
                (f"user:{x_user_id}", f"user_rejected: {note[:400]}",
                 row_id, auth.tenant_id),
            )

        _log_action(
            conn, tenant_id=auth.tenant_id, user_id=x_user_id,
            target_kind=kind, target_id=row_id, target_node_id=node_id,
            action="reject", before=before,
            after_status="rejected", note=note,
        )
        record_user_contribution(
            conn,
            user_id=x_user_id,
            raw_text=before["canonical_name"] or "",
            kind=kind,
            action="reject",
            note=note,
            tenant_id=auth.tenant_id,
        )
        conn.commit()

    return {"status": "rejected", "node_id": node_id}


# ── POST /nodes/:id/confirm ───────────────────────────────────────────
@router.post("/{node_id}/confirm")
async def confirm(
    node_id: str,
    body: dict = Body(default={}),
    auth: UnifiedAuthInfo = Depends(unified_auth),
    x_user_id: str = Header("user", alias="X-User-ID"),
):
    """Human-approve the current auto-classification. Locks the row so
    the validator won't reopen it, and records the confirmation as a
    user contribution so future candidates with the same name inherit
    the user-approved type."""
    from storage.postgres.db import get_connection
    from core.entity_validation.cache import record_user_contribution

    note = (body.get("note") or "")[:1000]
    kind, row_id = _parse_node_id(node_id)
    table, pk = _table_for(kind)

    with get_connection() as conn:
        before = _fetch_current(conn, kind, row_id, auth.tenant_id)

        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {table} SET
                    validation_status     = 'human_validated',
                    validation_locked     = TRUE,
                    validated_at          = NOW(),
                    updated_at            = NOW()
                 WHERE {pk} = %s AND tenant_id = %s
                """,
                (row_id, auth.tenant_id),
            )

        _log_action(
            conn, tenant_id=auth.tenant_id, user_id=x_user_id,
            target_kind=kind, target_id=row_id, target_node_id=node_id,
            action="confirm", before=before,
            after_type=before["canonical_type"],
            after_status="human_validated", note=note,
        )
        record_user_contribution(
            conn,
            user_id=x_user_id,
            raw_text=before["canonical_name"] or "",
            kind=kind,
            action="confirm",
            canonical_name=before["canonical_name"],
            canonical_type=before["canonical_type"],
            target_node_id=node_id,
            note=note,
            tenant_id=auth.tenant_id,
        )
        conn.commit()

    return {"status": "confirmed", "node_id": node_id}


# ── POST /nodes/:id/unlock ────────────────────────────────────────────
@router.post("/{node_id}/unlock")
async def unlock(
    node_id: str,
    body: dict = Body(default={}),
    auth: UnifiedAuthInfo = Depends(unified_auth),
    x_user_id: str = Header("user", alias="X-User-ID"),
):
    """Reopen for re-validation. Clears validation_locked + bumps status
    back to 'pending' so the validator worker picks it up again."""
    from storage.postgres.db import get_connection

    note = (body.get("note") or "")[:1000]
    kind, row_id = _parse_node_id(node_id)
    table, pk = _table_for(kind)

    with get_connection() as conn:
        before = _fetch_current(conn, kind, row_id, auth.tenant_id)

        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {table} SET
                    validation_status      = 'pending',
                    validation_locked      = FALSE,
                    validation_attempts    = 0,
                    validation_last_error  = NULL,
                    updated_at             = NOW()
                 WHERE {pk} = %s AND tenant_id = %s
                """,
                (row_id, auth.tenant_id),
            )

        _log_action(
            conn, tenant_id=auth.tenant_id, user_id=x_user_id,
            target_kind=kind, target_id=row_id, target_node_id=node_id,
            action="unlock", before=before,
            after_status="pending", note=note,
        )
        conn.commit()

    return {"status": "unlocked", "node_id": node_id}


# ── GET /audit/overrides ──────────────────────────────────────────────
@router.get("/_audit/overrides")
async def recent_overrides(
    limit: int = 50,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Return the most recent manual override events for the tenant."""
    from storage.postgres.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT log_id, user_id,
                       target_kind, target_id, target_node_id,
                       action,
                       before_canonical_name, before_canonical_type, before_status,
                       after_canonical_type, after_status,
                       note, created_at
                  FROM manual_override_log
                 WHERE tenant_id = %s
                 ORDER BY created_at DESC
                 LIMIT %s
                """,
                (auth.tenant_id, min(limit, 200)),
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    for row in rows:
        if row.get("created_at"):
            row["created_at"] = row["created_at"].isoformat()

    return {"overrides": rows, "count": len(rows)}
