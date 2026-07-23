"""
Unknown signal types API — the Alerts tab "Unknown Types" section.

Users see one entry per novel signal_type their system has encountered
but doesn't know how to extract. Two actions:

  POST /unknown-types/{type_id}/ignore  — adds to ignore list, future
                                          signals still LDU-T but no
                                          more alerts
  POST /unknown-types/{type_id}/promote — writes YAML mapping file from
                                          the learner's proposal, future
                                          signals get extracted

This is how the LDU → GDU loop gets closed. A signal with unknown type
goes to LDU-P, the shape profiler observes it, the learner proposes a
mapping, the user reviews + promotes, and from that moment forward the
type is known and extracted.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Header, Query
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

log = logging.getLogger("enki.unknown_types")
router = APIRouter(prefix="/unknown-types", tags=["unknown-types"])


@router.get("")
async def list_unknown_types(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    status: Optional[str] = Query(None, description="pending | ignored | promoted"),
    limit: int = Query(50, le=200),
):
    """List unknown signal types awaiting review."""
    from storage.postgres.db import get_connection

    where = ["tenant_id = %s"]
    params: list = [auth.tenant_id]
    if status:
        where.append("status = %s")
        params.append(status)
    else:
        where.append("status = 'pending'")
    params.append(limit)

    rows: list[dict] = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT type_id, signal_type, status, signal_count,
                           first_seen_at, last_seen_at,
                           sample_payloads, proposed_mapping,
                           resolved_by, resolved_at
                      FROM unknown_signal_types
                     WHERE {' AND '.join(where)}
                     ORDER BY signal_count DESC, last_seen_at DESC
                     LIMIT %s
                    """,
                    params,
                )
                cols = [d[0] for d in cur.description]
                for r in cur.fetchall():
                    rows.append(dict(zip(cols, r)))
    except Exception as e:
        log.exception("list_unknown_types failed")
        raise HTTPException(status_code=500, detail=str(e))

    return {"unknown_types": rows, "count": len(rows)}


@router.post("/{type_id}/ignore")
async def ignore_unknown_type(
    type_id: int,
    auth: UnifiedAuthInfo = Depends(unified_auth),
    reviewer: str = Query("cli", description="reviewer id recorded on the row"),
):
    """Mark a signal type as ignored. Future signals of this type still
    go to LDU-T but no more alerts fire and no worker is built."""
    from storage.postgres.db import get_connection

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE unknown_signal_types
                       SET status = 'ignored',
                           resolved_by = %s,
                           resolved_at = NOW()
                     WHERE tenant_id = %s AND type_id = %s
                     RETURNING signal_type
                    """,
                    (reviewer, auth.tenant_id, type_id),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="not found")
            conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        log.exception("ignore_unknown_type failed")
        raise HTTPException(status_code=500, detail=str(e))

    return {"status": "ignored", "signal_type": row[0]}


@router.post("/{type_id}/promote")
async def promote_unknown_type(
    type_id: int,
    body: dict = Body(default=None),
    auth: UnifiedAuthInfo = Depends(unified_auth),
    reviewer: str = Query("cli"),
):
    """Promote a signal type: hand-edit or accept the proposed mapping,
    write it to storage/mappings/learned/ as a YAML file, and mark the
    unknown_signal_types row as promoted.

    Request body can override the proposed_mapping (for user-edited
    field maps), or be empty to accept the learner's proposal as-is.
    """
    from storage.postgres.db import get_connection

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT type_id, signal_type, proposed_mapping
                      FROM unknown_signal_types
                     WHERE tenant_id = %s AND type_id = %s
                    """,
                    (auth.tenant_id, type_id),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="not found")

                _, signal_type, proposed_mapping = row

                # Accept body override if provided, else use the learner's proposal
                mapping = None
                if isinstance(body, dict) and body:
                    mapping = body
                elif proposed_mapping:
                    mapping = proposed_mapping

                if not mapping:
                    raise HTTPException(
                        status_code=400,
                        detail="no proposed_mapping and no override — let the learner run first",
                    )

        # Write the learned YAML file (delegated to the existing promoter)
        from core.extraction.learning.promoter import promote as learning_promote
        result = learning_promote(conn, signal_type, reviewer=reviewer)
        if result.get("status") not in ("promoted",):
            raise HTTPException(
                status_code=500,
                detail=f"promotion failed: {result.get('reason')}",
            )

        # Update unknown_signal_types to mark as promoted
        with get_connection() as conn2:
            with conn2.cursor() as cur:
                cur.execute(
                    """
                    UPDATE unknown_signal_types
                       SET status = 'promoted',
                           resolved_by = %s,
                           resolved_at = NOW()
                     WHERE tenant_id = %s AND type_id = %s
                    """,
                    (reviewer, auth.tenant_id, type_id),
                )
            conn2.commit()

    except HTTPException:
        raise
    except Exception as e:
        log.exception("promote_unknown_type failed")
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "status": "promoted",
        "signal_type": signal_type,
        "yaml_path": result.get("path"),
    }
