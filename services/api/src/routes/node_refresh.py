"""
Node Refresh — check for new data since a node was last viewed.

Called when a user opens a node detail page. Checks for:
  - New signals mentioning this entity since last_checked
  - New edges formed since last_checked
  - Updated completeness score
  - New investigation findings

Returns a delta summary so the UI can show "3 new signals, 1 new connection."
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from storage.postgres.db import get_connection

router = APIRouter(tags=["nodes"])


@router.post("/nodes/{node_id}/refresh")
async def refresh_node(
    node_id: str,
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Check for new data on a node since last_checked timestamp.

    Body (optional): {"last_checked": "2026-04-28T10:00:00Z"}
    If omitted, returns full current state.
    """
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    last_checked_str = body.get("last_checked", "")
    last_checked = None
    if last_checked_str:
        try:
            last_checked = datetime.fromisoformat(last_checked_str.replace("Z", "+00:00"))
        except Exception:
            pass

    tenant_id = auth.tenant_id
    result = {
        "node_id": node_id,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "new_signals": 0,
        "new_edges": 0,
        "new_findings": [],
        "completeness_score": None,
        "completeness_tier": None,
        "source_count": None,
        "needs_update": False,
        "last_checked": last_checked_str or None,
    }

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Get current node state
                cur.execute("""
                    SELECT completeness_score, completeness_tier, source_count,
                           updated_at, node_type, subtype, title, master_entity_id
                    FROM knowledge_nodes
                    WHERE node_id = %s AND tenant_id = %s
                """, (node_id, tenant_id))
                row = cur.fetchone()
                if not row:
                    return JSONResponse(status_code=404, content={"error": "Node not found"})

                result["completeness_score"] = float(row[0]) if row[0] else 0
                result["completeness_tier"] = row[1]
                result["source_count"] = row[2] or 0
                node_updated = row[3]
                node_type = row[4]
                entity_id = row[7]

                if last_checked:
                    # Count new signals mentioning this entity since last_checked
                    if entity_id:
                        cur.execute("""
                            SELECT COUNT(*) FROM entity_events
                            WHERE entity_id = %s AND tenant_id = %s
                              AND created_at > %s
                        """, (entity_id, tenant_id, last_checked))
                        result["new_signals"] = cur.fetchone()[0]

                    # Count new edges since last_checked
                    cur.execute("""
                        SELECT COUNT(*) FROM knowledge_edges
                        WHERE (source_node_id = %s OR target_node_id = %s)
                          AND created_at > %s
                    """, (node_id, node_id, last_checked))
                    edge_row = cur.fetchone()
                    result["new_edges"] = edge_row[0] if edge_row else 0

                    # Check for new investigation findings
                    if entity_id:
                        cur.execute("""
                            SELECT f.source, f.finding_type, f.title, f.created_at
                            FROM investigation_findings f
                            JOIN entity_investigations i ON f.investigation_id = i.investigation_id
                            WHERE i.entity_id = %s AND i.tenant_id = %s
                              AND f.created_at > %s
                            ORDER BY f.created_at DESC LIMIT 5
                        """, (entity_id, tenant_id, last_checked))
                        for fr in cur.fetchall():
                            result["new_findings"].append({
                                "source": fr[0],
                                "type": fr[1],
                                "title": fr[2],
                                "timestamp": fr[3].isoformat() if fr[3] else None,
                            })

                    # Check if node was updated after last_checked
                    if node_updated and node_updated > last_checked:
                        result["needs_update"] = True

                # Determine if anything is new
                if result["new_signals"] > 0 or result["new_edges"] > 0 or result["new_findings"]:
                    result["needs_update"] = True

    except Exception as e:
        result["error"] = str(e)

    return result
