"""
Multi-hop graph reasoning API.

Three primitives over the knowledge_edges graph:

    GET /graph/path?from=<nid>&to=<nid>&max_hops=3
        — shortest path(s) between two nodes, with edge evidence.

    GET /graph/neighborhood?node=<nid>&hops=2&rel_types=met_with,acquired
        — every node reachable within N hops, grouped by distance.

    GET /graph/related?node=<nid>&limit=20
        — top-N related nodes scored by (strength × 1/hop_distance).

All traversal happens in Postgres via recursive CTE over
knowledge_edges — cheaper than loading the graph into Python for
typical 2-3 hop queries. Index on (source_node_id, relationship,
strength) was added in migration 010.

Multi-hop paths include evidence snippets from each edge so UI can
render "Petrov → met_with ("Shanghai summit") → Wei → funded
("via state bank") → Rosneft".
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

log = logging.getLogger("enki.api.graph_reasoning")
router = APIRouter(prefix="/graph", tags=["graph"])


def _get_conn():
    from storage.postgres.db import get_connection
    return get_connection()


def _rel_filter_clause(rel_types: Optional[list[str]]) -> tuple[str, list]:
    if not rel_types:
        return "", []
    placeholders = ",".join(["%s"] * len(rel_types))
    return f" AND relationship IN ({placeholders})", list(rel_types)


@router.get("/path")
async def shortest_path(
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
    max_hops: int = Query(3, ge=1, le=5),
    min_strength: float = Query(0.5, ge=0.0, le=1.0),
    rel_types: Optional[str] = Query(None, description="comma-separated relationship whitelist"),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Return the shortest path(s) between two nodes. Uses a bounded
    recursive CTE. If no path within max_hops, returns {"paths": []}."""
    rel_list = [r.strip() for r in rel_types.split(",")] if rel_types else None
    rel_sql, rel_params = _rel_filter_clause(rel_list)

    sql = f"""
    WITH RECURSIVE walk AS (
        SELECT source_node_id, target_node_id, relationship, strength, evidence,
               1 AS hops,
               ARRAY[source_node_id, target_node_id] AS path,
               ARRAY[jsonb_build_object(
                   'from', source_node_id,
                   'to', target_node_id,
                   'rel', relationship,
                   'strength', strength,
                   'evidence', evidence
               )] AS edges
          FROM knowledge_edges
         WHERE source_node_id = %s
           AND strength >= %s
           {rel_sql}

        UNION ALL

        SELECT e.source_node_id, e.target_node_id, e.relationship, e.strength, e.evidence,
               w.hops + 1,
               w.path || e.target_node_id,
               w.edges || jsonb_build_object(
                   'from', e.source_node_id,
                   'to', e.target_node_id,
                   'rel', e.relationship,
                   'strength', e.strength,
                   'evidence', e.evidence
               )
          FROM knowledge_edges e
          JOIN walk w ON e.source_node_id = w.target_node_id
         WHERE w.hops < %s
           AND e.strength >= %s
           AND NOT (e.target_node_id = ANY(w.path))   -- no cycles
           {rel_sql}
    )
    SELECT hops, path, edges
      FROM walk
     WHERE target_node_id = %s
     ORDER BY hops ASC, (SELECT SUM((e->>'strength')::float) FROM jsonb_array_elements(edges) e) DESC
     LIMIT 5
    """
    params = [from_, min_strength] + rel_params + [max_hops, min_strength] + rel_params + [to]

    with _get_conn() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(sql, params)
                rows = cur.fetchall()
            except Exception as e:
                log.exception("path query failed")
                raise HTTPException(500, detail=str(e))

    paths = [{"hops": r[0], "nodes": r[1], "edges": r[2]} for r in rows]
    return {"from": from_, "to": to, "paths": paths, "count": len(paths)}


@router.get("/neighborhood")
async def neighborhood(
    node: str = Query(...),
    hops: int = Query(2, ge=1, le=3),
    min_strength: float = Query(0.5, ge=0.0, le=1.0),
    rel_types: Optional[str] = Query(None),
    limit: int = Query(200, le=500),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """All nodes within N hops. Returned with distance and a sample
    edge set so the UI can render a radial graph."""
    rel_list = [r.strip() for r in rel_types.split(",")] if rel_types else None
    rel_sql, rel_params = _rel_filter_clause(rel_list)

    sql = f"""
    WITH RECURSIVE walk AS (
        SELECT target_node_id AS node_id, 1 AS distance, source_node_id AS parent,
               relationship, strength, evidence
          FROM knowledge_edges
         WHERE source_node_id = %s
           AND strength >= %s
           {rel_sql}

        UNION ALL

        SELECT e.target_node_id, w.distance + 1, e.source_node_id,
               e.relationship, e.strength, e.evidence
          FROM knowledge_edges e
          JOIN walk w ON e.source_node_id = w.node_id
         WHERE w.distance < %s
           AND e.strength >= %s
           AND e.target_node_id != %s
           {rel_sql}
    )
    SELECT w.node_id, MIN(w.distance) AS d,
           jsonb_agg(
             jsonb_build_object(
               'parent', w.parent,
               'rel', w.relationship,
               'strength', w.strength,
               'evidence', w.evidence
             )
           ) AS edges,
           kn.title, kn.node_type, kn.subtype, kn.description
      FROM walk w
      LEFT JOIN knowledge_nodes kn ON kn.node_id = w.node_id AND kn.tenant_id = %s
     GROUP BY w.node_id, kn.title, kn.node_type, kn.subtype, kn.description
     ORDER BY d ASC, w.node_id
     LIMIT %s
    """
    params = [node, min_strength] + rel_params + [hops, min_strength, node] + rel_params + [auth.tenant_id, limit]

    with _get_conn() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(sql, params)
                rows = cur.fetchall()
            except Exception as e:
                log.exception("neighborhood query failed")
                raise HTTPException(500, detail=str(e))

    by_dist: dict = {}
    for r in rows:
        by_dist.setdefault(r[1], []).append({
            "node_id": r[0],
            "distance": r[1],
            "edges": r[2],
            "title": r[3],
            "node_type": r[4],
            "subtype": r[5],
            "description": (r[6] or "")[:300],
        })
    return {
        "center": node,
        "hops": hops,
        "total": len(rows),
        "by_distance": by_dist,
    }


@router.get("/related")
async def top_related(
    node: str = Query(...),
    limit: int = Query(20, le=100),
    min_strength: float = Query(0.4, ge=0.0, le=1.0),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Top-N related nodes scored by (strength × 1/(1+distance)).
    Traverses 2 hops max, aggregates by target, picks strongest."""
    sql = """
    WITH RECURSIVE walk AS (
        SELECT target_node_id AS node_id, 1 AS distance, strength
          FROM knowledge_edges
         WHERE source_node_id = %s AND strength >= %s

        UNION ALL

        SELECT e.target_node_id, w.distance + 1, e.strength * w.strength
          FROM knowledge_edges e
          JOIN walk w ON e.source_node_id = w.node_id
         WHERE w.distance < 2 AND e.strength >= %s AND e.target_node_id != %s
    )
    SELECT node_id,
           MIN(distance) AS d,
           MAX(strength * (1.0 / (1.0 + MIN(distance) - 1))) AS score
      FROM walk
     GROUP BY node_id
     ORDER BY score DESC
     LIMIT %s
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(sql, (node, min_strength, min_strength, node, limit))
                rows = cur.fetchall()
            except Exception as e:
                log.exception("related query failed")
                raise HTTPException(500, detail=str(e))
            if not rows:
                return {"node": node, "related": []}
            ids = [r[0] for r in rows]
            cur.execute(
                """
                SELECT node_id, title, node_type, subtype
                  FROM knowledge_nodes
                 WHERE tenant_id = %s AND node_id = ANY(%s)
                """,
                (auth.tenant_id, ids),
            )
            meta = {r[0]: {"title": r[1], "node_type": r[2], "subtype": r[3]}
                    for r in cur.fetchall()}

    related = []
    for nid, dist, score in rows:
        m = meta.get(nid, {})
        related.append({
            "node_id": nid, "distance": dist, "score": float(score or 0),
            **m,
        })
    return {"node": node, "related": related}
