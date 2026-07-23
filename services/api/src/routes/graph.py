"""
Graph Explorer endpoints — unified workspace view.
GET /graph/workspaces — list all workspaces (topics, projects, cases)
GET /graph/nodes     — nodes for a specific workspace
"""

from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

router = APIRouter(prefix="/graph", tags=["graph"])


def _get_conn():
    from storage.postgres.db import get_connection
    return get_connection()


@router.get("/workspaces")
async def list_workspaces(auth: UnifiedAuthInfo = Depends(unified_auth)):
    workspaces = []
    with _get_conn() as conn:
        with conn.cursor() as cur:
            # Watch Topics
            cur.execute("""
                SELECT topic_id, title, status, keywords, description, updated_at
                FROM watch_topics WHERE tenant_id = %s ORDER BY updated_at DESC
            """, (auth.tenant_id,))
            for r in cur.fetchall():
                workspaces.append({
                    "id": r[0], "type": "watch", "title": r[1], "status": r[2],
                    "node_count": 0, "edge_count": 0, "color": "#3b82f6",
                    "description": r[4], "updated_at": r[5].isoformat() if r[5] else None,
                    "tags": r[3] or [],
                })

            # Projects
            cur.execute("""
                SELECT project_id, name, status, description, updated_at,
                    (SELECT COUNT(*) FROM knowledge_nodes WHERE project_id = p.project_id AND tenant_id = p.tenant_id) as node_count
                FROM projects p WHERE tenant_id = %s ORDER BY updated_at DESC
            """, (auth.tenant_id,))
            for r in cur.fetchall():
                workspaces.append({
                    "id": r[0], "type": "project", "title": r[1], "status": r[2],
                    "node_count": r[5] or 0, "edge_count": 0, "color": "#a855f7",
                    "description": r[3], "updated_at": r[4].isoformat() if r[4] else None,
                })

            # Cases
            cur.execute("""
                SELECT case_id, title, status, priority, description, updated_at
                FROM cases WHERE tenant_id = %s AND status IN ('open', 'investigating')
                ORDER BY updated_at DESC
            """, (auth.tenant_id,))
            for r in cur.fetchall():
                workspaces.append({
                    "id": r[0], "type": "case", "title": r[1], "status": r[2],
                    "priority": r[3], "node_count": 0, "edge_count": 0, "color": "#f59e0b",
                    "description": r[4], "updated_at": r[5].isoformat() if r[5] else None,
                })

    return {"workspaces": workspaces, "links": []}


@router.get("/nodes")
async def list_workspace_nodes(
    workspace: str = Query(...),
    type: str = Query("project"),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    nodes = []
    edges = []
    with _get_conn() as conn:
        with conn.cursor() as cur:
            if type == "project":
                cur.execute("""
                    SELECT node_id, title, epistemic_type, description, source_authority,
                        metadata, latitude, longitude
                    FROM knowledge_nodes WHERE project_id = %s AND tenant_id = %s LIMIT 50
                """, (workspace, auth.tenant_id))
            elif type == "watch":
                cur.execute("""
                    SELECT kn.node_id, kn.title, kn.epistemic_type, kn.description, kn.source_authority,
                        kn.metadata, kn.latitude, kn.longitude
                    FROM knowledge_nodes kn
                    JOIN watch_topic_projects wtp ON wtp.project_id = kn.project_id AND wtp.tenant_id = kn.tenant_id
                    WHERE wtp.topic_id = %s AND wtp.tenant_id = %s LIMIT 50
                """, (workspace, auth.tenant_id))
            elif type == "case":
                cur.execute("""
                    SELECT kn.node_id, kn.title, kn.epistemic_type, kn.description, kn.source_authority,
                        kn.metadata, kn.latitude, kn.longitude
                    FROM knowledge_nodes kn
                    JOIN case_nodes cn ON cn.node_id = kn.node_id AND cn.tenant_id = kn.tenant_id
                    WHERE cn.case_id = %s AND cn.tenant_id = %s LIMIT 50
                """, (workspace, auth.tenant_id))
            else:
                return {"nodes": [], "edges": []}

            for r in cur.fetchall():
                meta = r[5] or {}
                nodes.append({
                    "id": r[0], "title": r[1], "epistemic_type": r[2], "description": r[3],
                    "source_authority": r[4], "entity_type": meta.get("entity_type"),
                    "mention_count": meta.get("mention_count", 1),
                    "connections": 0, "confidence": meta.get("confidence", 0.5),
                    "latitude": r[6], "longitude": r[7],
                    "workspace_id": workspace,
                })

            # Get edges between these nodes
            node_ids = [n["id"] for n in nodes]
            if node_ids:
                placeholders = ",".join(["%s"] * len(node_ids))
                cur.execute(f"""
                    SELECT source_node_id, target_node_id, relationship, strength
                    FROM knowledge_edges
                    WHERE tenant_id = %s AND source_node_id IN ({placeholders}) AND target_node_id IN ({placeholders})
                """, [auth.tenant_id] + node_ids + node_ids)
                edges = [{"source": r[0], "target": r[1], "relationship": r[2],
                          "strength": float(r[3]) if r[3] else 0.5} for r in cur.fetchall()]

    return {"nodes": nodes, "edges": edges}
