"""
Project/workspace management.

Projects are isolated context spaces. Each project has its own
knowledge graph, chat history, and AI context.
"""

import json
import uuid
from typing import Optional

from storage.postgres.db import get_connection


def create_project(
    tenant_id: str,
    identity_id: str,
    name: str,
    description: Optional[str] = None,
    routing_rules: Optional[dict] = None,
    dsn: Optional[str] = None,
) -> dict:
    project_id = f"proj_{uuid.uuid4().hex[:12]}"
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO projects (project_id, tenant_id, identity_id, name, description, routing_rules)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (project_id, tenant_id, identity_id, name, description,
                  json.dumps(routing_rules or {})))
            conn.commit()
    return {"project_id": project_id, "name": name, "status": "active"}


def get_project(project_id: str, tenant_id: str, dsn: Optional[str] = None) -> Optional[dict]:
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.project_id, p.name, p.description, p.status, p.routing_rules,
                       p.created_at, p.identity_id,
                       (SELECT COUNT(*) FROM knowledge_nodes kn WHERE kn.project_id = p.project_id) as node_count,
                       (SELECT COUNT(*) FROM knowledge_edges ke WHERE ke.project_id = p.project_id) as edge_count
                FROM projects p
                WHERE p.project_id = %s AND p.tenant_id = %s
            """, (project_id, tenant_id))
            r = cur.fetchone()
            if not r:
                return None
            return {
                "project_id": r[0], "name": r[1], "description": r[2],
                "status": r[3], "routing_rules": r[4],
                "created_at": r[5].isoformat(), "owner": r[6],
                "node_count": r[7], "edge_count": r[8],
            }


def list_projects(tenant_id: str, identity_id: Optional[str] = None, dsn: Optional[str] = None) -> list[dict]:
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            if identity_id:
                cur.execute("""
                    SELECT project_id, name, status, created_at,
                           (SELECT COUNT(*) FROM knowledge_nodes kn WHERE kn.project_id = p.project_id)
                    FROM projects p
                    WHERE tenant_id = %s AND identity_id = %s ORDER BY updated_at DESC
                """, (tenant_id, identity_id))
            else:
                cur.execute("""
                    SELECT project_id, name, status, created_at,
                           (SELECT COUNT(*) FROM knowledge_nodes kn WHERE kn.project_id = p.project_id)
                    FROM projects p
                    WHERE tenant_id = %s ORDER BY updated_at DESC
                """, (tenant_id,))
            return [
                {"project_id": r[0], "name": r[1], "status": r[2],
                 "created_at": r[3].isoformat(), "node_count": r[4]}
                for r in cur.fetchall()
            ]
