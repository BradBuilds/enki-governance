"""
Node recall — unified "everything we know about this node" endpoint.

    GET /nodes/{node_id}/recall

Returns:
    - node: canonical envelope + knowledge_nodes row
    - timeline: entity_mentions chronologically (what was said, when,
      by which source)
    - related: 2-hop neighborhood (delegated to graph_reasoning)
    - conflicts: open node_conflicts with full source attribution
    - cases, threads: references in case_nodes + thread_nodes
    - chat_context: curated prompt-ready block for LLM grounding

The chat_context is designed to be dropped into a system prompt so
"talk to this node" in the UI sends Gemini a concise, fact-dense
briefing — NOT a json dump. Evidence snippets, quotes, and role
tags are prioritized over raw metadata.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

log = logging.getLogger("enki.api.node_recall")
router = APIRouter(tags=["node-recall"])


def _get_conn():
    from storage.postgres.db import get_connection
    return get_connection()


def _load_node_base(cur, node_id: str, tenant_id: str) -> dict:
    cur.execute(
        """
        SELECT node_id, title, description, node_type, subtype,
               envelope, completeness_score, completeness_tier,
               master_entity_id, master_topic_id,
               latitude, longitude, created_at, updated_at,
               epistemic_type, confidence
          FROM knowledge_nodes
         WHERE node_id = %s AND tenant_id = %s
        """,
        (node_id, tenant_id),
    )
    row = cur.fetchone()
    if not row:
        return {}
    env = row[5] or {}
    if isinstance(env, str):
        try: env = json.loads(env)
        except Exception: env = {}
    return {
        "node_id": row[0],
        "title": row[1],
        "description": row[2],
        "node_type": row[3],
        "subtype": row[4],
        "envelope": env,
        "completeness": {
            "score": float(row[6] or 0),
            "tier": row[7] or "stub",
        },
        "master_entity_id": row[8],
        "master_topic_id": row[9],
        "latitude": row[10],
        "longitude": row[11],
        "created_at": row[12].isoformat() if row[12] else None,
        "updated_at": row[13].isoformat() if row[13] else None,
        "epistemic_type": row[14],
        "confidence": float(row[15] or 0),
    }


def _load_mentions(cur, node_id: str, entity_id: Optional[int],
                   tenant_id: str, limit: int = 40) -> list[dict]:
    """Timeline of mentions for this node. If the node maps to a
    canonical entity (master_entity_id), pull every mention of that
    entity across all signals — otherwise pull by node_id match."""
    if entity_id:
        cur.execute(
            """
            SELECT m.signal_rid, m.name_raw, m.role, m.context_snippet,
                   m.confidence, m.event_time, m.created_at,
                   s.payload->>'url' AS url,
                   COALESCE(s.metadata->>'outlet_name', s.payload->>'source', 'unknown') AS outlet,
                   s.payload->>'title' AS article_title
              FROM entity_mentions m
              LEFT JOIN signals s ON s.rid = m.signal_rid
             WHERE m.entity_id = %s AND m.tenant_id = %s
             ORDER BY COALESCE(m.event_time, m.created_at) DESC
             LIMIT %s
            """,
            (entity_id, tenant_id, limit),
        )
    else:
        cur.execute(
            """
            SELECT m.signal_rid, m.name_raw, m.role, m.context_snippet,
                   m.confidence, m.event_time, m.created_at,
                   s.payload->>'url', COALESCE(s.metadata->>'outlet_name','unknown'),
                   s.payload->>'title'
              FROM entity_mentions m
              LEFT JOIN signals s ON s.rid = m.signal_rid
             WHERE m.node_id = %s AND m.tenant_id = %s
             ORDER BY m.created_at DESC
             LIMIT %s
            """,
            (node_id, tenant_id, limit),
        )
    out = []
    for r in cur.fetchall():
        out.append({
            "signal_rid": r[0],
            "name_used": r[1],
            "role": r[2],
            "context": r[3],
            "confidence": float(r[4] or 0),
            "event_time": r[5].isoformat() if r[5] else None,
            "extracted_at": r[6].isoformat() if r[6] else None,
            "source": {
                "url": r[7],
                "outlet": r[8],
                "title": r[9],
            },
        })
    return out


def _load_conflicts(cur, node_id: str, entity_id: Optional[int]) -> list[dict]:
    clauses = ["(node_id = %s"]
    params = [node_id]
    if entity_id:
        clauses.append("OR entity_id = %s)")
        params.append(entity_id)
    else:
        clauses[0] += ")"
    where = " ".join(clauses)
    cur.execute(
        f"""
        SELECT conflict_id, field_path, field_kind,
               value_a, sources_a, value_b, sources_b,
               resolution, severity, detected_at, detected_by
          FROM node_conflicts
         WHERE {where}
         ORDER BY detected_at DESC
         LIMIT 20
        """,
        params,
    )
    return [
        {
            "conflict_id": r[0],
            "field_path": r[1],
            "field_kind": r[2],
            "value_a": r[3],
            "sources_a": r[4],
            "value_b": r[5],
            "sources_b": r[6],
            "resolution": r[7],
            "severity": r[8],
            "detected_at": r[9].isoformat() if r[9] else None,
            "detected_by": r[10],
        }
        for r in cur.fetchall()
    ]


def _load_neighborhood(cur, node_id: str, hops: int = 2) -> list[dict]:
    sql = """
    WITH RECURSIVE walk AS (
        SELECT target_node_id AS node_id, 1 AS distance,
               relationship, strength, evidence
          FROM knowledge_edges
         WHERE source_node_id = %s AND strength >= 0.5

        UNION ALL

        SELECT e.target_node_id, w.distance + 1,
               e.relationship, e.strength, e.evidence
          FROM knowledge_edges e
          JOIN walk w ON e.source_node_id = w.node_id
         WHERE w.distance < %s AND e.strength >= 0.5
           AND e.target_node_id != %s
    )
    SELECT w.node_id, MIN(w.distance) AS d,
           jsonb_agg(jsonb_build_object(
             'rel', w.relationship, 'strength', w.strength,
             'evidence', w.evidence)) AS edges,
           kn.title, kn.node_type, kn.subtype
      FROM walk w
      LEFT JOIN knowledge_nodes kn ON kn.node_id = w.node_id
     GROUP BY w.node_id, kn.title, kn.node_type, kn.subtype
     ORDER BY d ASC
     LIMIT 40
    """
    cur.execute(sql, (node_id, hops, node_id))
    return [
        {
            "node_id": r[0], "distance": r[1], "edges": r[2],
            "title": r[3], "node_type": r[4], "subtype": r[5],
        }
        for r in cur.fetchall()
    ]


def _build_chat_context(node: dict, mentions: list[dict],
                        conflicts: list[dict],
                        neighborhood: list[dict]) -> str:
    """Produce a LLM-ready briefing. Fact-dense, short, role-tagged."""
    lines = []
    lines.append(f"# {node.get('title', 'Unknown node')} ({node.get('node_type')}/{node.get('subtype')})")
    if node.get("description"):
        lines.append(node["description"][:400])

    tier = node.get("completeness", {}).get("tier")
    if tier:
        lines.append(f"Completeness: {tier} ({int(node['completeness']['score']*100)}%)")

    if mentions:
        lines.append("\n## Recent mentions (most recent first)")
        for m in mentions[:8]:
            src = m.get("source", {})
            outlet = src.get("outlet") or "?"
            ts = m.get("event_time") or m.get("extracted_at") or ""
            ctx = (m.get("context") or "")[:220]
            role = m.get("role") or "mention"
            lines.append(f"- [{outlet} {ts[:10]}] ({role}) {ctx}")

    if conflicts:
        lines.append(f"\n## Open conflicts ({len(conflicts)})")
        for c in conflicts[:5]:
            va, vb = c.get("value_a"), c.get("value_b")
            sa = (c.get("sources_a") or [{}])[0].get("outlet", "?")
            sb = (c.get("sources_b") or [{}])[0].get("outlet", "?")
            lines.append(
                f"- {c.get('field_path')}: {sa} says {va} vs {sb} says {vb} "
                f"[{c.get('resolution')}]"
            )

    if neighborhood:
        lines.append(f"\n## Connected nodes ({len(neighborhood)})")
        for n in neighborhood[:10]:
            rels = [e.get("rel") for e in (n.get("edges") or [])][:3]
            lines.append(
                f"- {n.get('title', n['node_id'])} ({n.get('node_type')}) "
                f"via [{','.join(rels)}] (d={n['distance']})"
            )

    return "\n".join(lines)


@router.get("/nodes/{node_id}/recall")
async def recall_node(
    node_id: str,
    hops: int = Query(2, ge=1, le=3),
    mention_limit: int = Query(40, le=100),
    include_chat_context: bool = Query(True),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Return everything known about this node in one payload."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            node = _load_node_base(cur, node_id, auth.tenant_id)
            if not node:
                return JSONResponse(status_code=404, content={"error": "node not found"})

            entity_id = node.get("master_entity_id")
            mentions = _load_mentions(cur, node_id, entity_id,
                                      auth.tenant_id, limit=mention_limit)
            conflicts = _load_conflicts(cur, node_id, entity_id)
            neighborhood = _load_neighborhood(cur, node_id, hops=hops)

    result = {
        "node": node,
        "timeline": mentions,
        "conflicts": conflicts,
        "neighborhood": neighborhood,
        "counts": {
            "mentions": len(mentions),
            "conflicts": len(conflicts),
            "connected_nodes": len(neighborhood),
        },
    }
    if include_chat_context:
        result["chat_context"] = _build_chat_context(
            node, mentions, conflicts, neighborhood,
        )
    return result
