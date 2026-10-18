"""
Knowledge graph — nodes, edges, traversal.

Nodes are enriched signals (or manual ideas) with descriptions and concepts.
Edges connect nodes through typed relationships.
Max depth: 4 levels from any root node.
Nodes are selectable for visual mapping.

Graph structure:
  Root (depth 0) ── Child (depth 1) ── Grandchild (depth 2) ── Great (depth 3) ── Leaf (depth 4)

  No node can exceed depth 4. This keeps the graph navigable and
  prevents infinite expansion. Users can always create a NEW root
  node and reference the old graph.
"""

import json
import uuid
from typing import Optional

from storage.postgres.db import get_connection

MAX_DEPTH = 4


def create_node(
    project_id: str,
    tenant_id: str,
    title: str,
    description: Optional[str] = None,
    epistemic_type: str = "observation",
    concepts: Optional[list[str]] = None,
    signal_rid: Optional[int] = None,
    parent_node_id: Optional[str] = None,
    source_authority: str = "unverified",
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    metadata: Optional[dict] = None,
    dsn: Optional[str] = None,
) -> Optional[dict]:
    """Create a knowledge node. Enforces max depth of 4."""
    node_id = f"node_{uuid.uuid4().hex[:12]}"

    # Calculate depth
    depth = 0
    if parent_node_id:
        depth = _get_node_depth(parent_node_id, dsn)
        if depth is None:
            return None  # parent doesn't exist
        depth += 1
        if depth > MAX_DEPTH:
            return {"error": f"Max depth {MAX_DEPTH} exceeded. Create a new root node instead."}

    # Build search vector from title + description + concepts
    search_text = f"{title} {description or ''} {' '.join(concepts or [])}"

    # Admit through GDU gateway
    from core.gdu.enforce import admit_node as gdu_admit_node
    gdu_admit_node(
        tenant_id=tenant_id,
        category="assessment",
        subtype=epistemic_type if epistemic_type in ("fact", "observation", "claim", "hypothesis", "question") else "observation",
        title=title[:200],
        description=(description or "")[:500],
        signal_rid=signal_rid or 0,
        collapse_key=node_id,
    )

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            import json as _json
            cur.execute("""
                INSERT INTO knowledge_nodes
                    (node_id, project_id, tenant_id, signal_rid, title, description,
                     epistemic_type, concepts, source_authority, depth, parent_node_id,
                     latitude, longitude, metadata, search_vector, gdu_canonical)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, to_tsvector('english', %s), TRUE)
                ON CONFLICT (node_id) DO UPDATE SET
                    title = EXCLUDED.title, description = EXCLUDED.description,
                    gdu_canonical = TRUE, updated_at = NOW()
            """, (
                node_id, project_id, tenant_id, signal_rid, title, description,
                epistemic_type, concepts or [], source_authority, depth,
                parent_node_id, latitude, longitude,
                _json.dumps(metadata) if metadata else '{}',
                search_text,
            ))

            # Index concepts
            for concept in (concepts or []):
                cur.execute("""
                    INSERT INTO concept_index (concept, node_id, project_id)
                    VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
                """, (concept.lower().strip(), node_id, project_id))

            # Create edge to parent if exists
            if parent_node_id:
                cur.execute("""
                    INSERT INTO knowledge_edges
                        (edge_id, project_id, source_node_id, target_node_id, relationship)
                    VALUES (%s, %s, %s, %s, 'extends')
                    ON CONFLICT DO NOTHING
                """, (f"edge_{uuid.uuid4().hex[:12]}", project_id, parent_node_id, node_id))

            conn.commit()

    return {
        "node_id": node_id, "title": title, "depth": depth,
        "epistemic_type": epistemic_type, "project_id": project_id,
    }


def create_edge(
    project_id: str,
    source_node_id: str,
    target_node_id: str,
    relationship: str,
    strength: float = 1.0,
    evidence: Optional[str] = None,
    ai_generated: bool = False,
    dsn: Optional[str] = None,
) -> dict:
    """Create an edge between two nodes."""
    edge_id = f"edge_{uuid.uuid4().hex[:12]}"
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO knowledge_edges
                    (edge_id, project_id, source_node_id, target_node_id,
                     relationship, strength, evidence, ai_generated)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_node_id, target_node_id, relationship) DO NOTHING
                RETURNING edge_id
            """, (edge_id, project_id, source_node_id, target_node_id,
                  relationship, strength, evidence, ai_generated))
            row = cur.fetchone()
            conn.commit()
    return {"edge_id": row[0] if row else None, "relationship": relationship, "created": row is not None}


def get_node(node_id: str, tenant_id: str, dsn: Optional[str] = None) -> Optional[dict]:
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT node_id, project_id, title, description, epistemic_type,
                       concepts, source_authority, depth, parent_node_id,
                       ai_enriched, signal_rid, created_at, latitude, longitude,
                       entity_type, entity_name, entity_identifiers,
                       event_time, event_end_time, temporal_precision,
                       place_name, place_country,
                       source_count, source_types, confidence, confidence_factors,
                       contradiction_count, corroboration_count,
                       gaps, gap_count, enrichment_status, promotion_eligible
                FROM knowledge_nodes
                WHERE node_id = %s AND tenant_id = %s
            """, (node_id, tenant_id))
            r = cur.fetchone()
            if not r:
                return None
            import json as _json
            def _j(v):
                if v is None: return {}
                if isinstance(v, dict): return v
                if isinstance(v, memoryview): return _json.loads(bytes(v))
                if isinstance(v, str):
                    try: return _json.loads(v)
                    except: return {}
                return {}
            return {
                "node_id": r[0], "project_id": r[1], "title": r[2],
                "description": r[3], "epistemic_type": r[4], "concepts": r[5] or [],
                "source_authority": r[6], "depth": r[7], "parent_node_id": r[8],
                "ai_enriched": r[9], "signal_rid": r[10],
                "created_at": r[11].isoformat() if r[11] else None,
                "latitude": r[12], "longitude": r[13],
                "entity_type": r[14], "entity_name": r[15],
                "entity_identifiers": _j(r[16]),
                "event_time": r[17].isoformat() if r[17] else None,
                "event_end_time": r[18].isoformat() if r[18] else None,
                "temporal_precision": r[19],
                "place_name": r[20], "place_country": r[21],
                "source_count": r[22] or 0, "source_types": r[23] or [],
                "confidence": r[24], "confidence_factors": _j(r[25]),
                "contradiction_count": r[26] or 0, "corroboration_count": r[27] or 0,
                "gaps": r[28] or [], "gap_count": r[29] or 0,
                "enrichment_status": r[30], "promotion_eligible": r[31],
            }


def get_node_graph(
    node_id: str,
    tenant_id: str,
    max_depth: int = 4,
    dsn: Optional[str] = None,
) -> dict:
    """
    Get a node and its connected subgraph (max 4 levels deep).

    Returns the node, its edges, and connected nodes — everything
    needed for visual graph rendering. Selectable nodes.

    The result is a flat structure (nodes + edges) that the frontend
    renders as a visual graph (D3.js, Cytoscape, or similar).
    """
    max_depth = min(max_depth, MAX_DEPTH)
    visited_nodes = set()
    all_nodes = []
    all_edges = []

    _traverse(node_id, tenant_id, 0, max_depth, visited_nodes, all_nodes, all_edges, dsn)

    return {
        "root_node_id": node_id,
        "nodes": all_nodes,
        "edges": all_edges,
        "node_count": len(all_nodes),
        "edge_count": len(all_edges),
        "max_depth": max_depth,
    }


def _traverse(
    node_id: str,
    tenant_id: str,
    current_depth: int,
    max_depth: int,
    visited: set,
    nodes: list,
    edges: list,
    dsn: Optional[str] = None,
):
    """Recursive graph traversal with depth limit."""
    if node_id in visited or current_depth > max_depth:
        return

    visited.add(node_id)

    node = get_node(node_id, tenant_id, dsn)
    if not node:
        return

    node["_graph_depth"] = current_depth
    node["_selectable"] = True
    nodes.append(node)

    # Get edges from this node
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT edge_id, source_node_id, target_node_id, relationship,
                       strength, evidence, ai_generated
                FROM knowledge_edges
                WHERE source_node_id = %s OR target_node_id = %s
            """, (node_id, node_id))

            for r in cur.fetchall():
                edge = {
                    "edge_id": r[0], "source": r[1], "target": r[2],
                    "relationship": r[3], "strength": r[4],
                    "evidence": r[5], "ai_generated": r[6],
                }

                # Avoid duplicate edges
                if edge["edge_id"] not in {e["edge_id"] for e in edges}:
                    edges.append(edge)

                # Traverse connected node
                other_id = r[2] if r[1] == node_id else r[1]
                if other_id not in visited:
                    _traverse(other_id, tenant_id, current_depth + 1,
                              max_depth, visited, nodes, edges, dsn)


def get_project_graph(
    project_id: str,
    tenant_id: str,
    dsn: Optional[str] = None,
) -> dict:
    """Get the entire project graph (all nodes + edges) for visual mapping."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT node_id, title, description, epistemic_type, concepts,
                       depth, parent_node_id, source_authority, ai_enriched
                FROM knowledge_nodes
                WHERE project_id = %s AND tenant_id = %s
                ORDER BY depth ASC, created_at ASC
            """, (project_id, tenant_id))
            nodes = [
                {
                    "node_id": r[0], "title": r[1], "description": r[2],
                    "epistemic_type": r[3], "concepts": r[4], "depth": r[5],
                    "parent_node_id": r[6], "source_authority": r[7],
                    "ai_enriched": r[8], "_selectable": True,
                }
                for r in cur.fetchall()
            ]

            cur.execute("""
                SELECT edge_id, source_node_id, target_node_id, relationship,
                       strength, evidence, ai_generated
                FROM knowledge_edges
                WHERE project_id = %s
            """, (project_id,))
            edges = [
                {
                    "edge_id": r[0], "source": r[1], "target": r[2],
                    "relationship": r[3], "strength": r[4],
                    "evidence": r[5], "ai_generated": r[6],
                }
                for r in cur.fetchall()
            ]

    return {"project_id": project_id, "nodes": nodes, "edges": edges,
            "node_count": len(nodes), "edge_count": len(edges)}


def search_nodes(
    project_id: str,
    tenant_id: str,
    query: str,
    limit: int = 50,
    dsn: Optional[str] = None,
) -> list[dict]:
    """Full-text search across knowledge nodes in a project."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT node_id, title, description, epistemic_type, concepts, depth,
                       ts_rank(search_vector, plainto_tsquery('english', %s)) as rank
                FROM knowledge_nodes
                WHERE project_id = %s AND tenant_id = %s
                  AND search_vector @@ plainto_tsquery('english', %s)
                ORDER BY rank DESC
                LIMIT %s
            """, (query, project_id, tenant_id, query, min(limit, 200)))
            return [
                {"node_id": r[0], "title": r[1], "description": r[2],
                 "epistemic_type": r[3], "concepts": r[4], "depth": r[5],
                 "relevance": float(r[6])}
                for r in cur.fetchall()
            ]


def find_by_concept(
    project_id: str,
    concept: str,
    dsn: Optional[str] = None,
) -> list[dict]:
    """Find all nodes that reference a specific concept."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT kn.node_id, kn.title, kn.epistemic_type, kn.depth
                FROM concept_index ci
                JOIN knowledge_nodes kn ON kn.node_id = ci.node_id
                WHERE ci.concept = %s AND ci.project_id = %s
                ORDER BY kn.created_at DESC
            """, (concept.lower().strip(), project_id))
            return [
                {"node_id": r[0], "title": r[1], "epistemic_type": r[2], "depth": r[3]}
                for r in cur.fetchall()
            ]


def get_context_graph(
    node_id: str,
    project_id: str,
    tenant_id: str,
    max_depth: int = 4,
    min_strength: float = 0.3,
    dsn: Optional[str] = None,
) -> dict:
    """
    Build AI context from the graph: scored traversal up to 4 levels deep.

    Uses a recursive CTE that follows edges filtered by strength,
    with cycle prevention. Returns nodes + edges sorted by traversal
    distance so the AI gets the most relevant context first.
    """
    max_depth = min(max_depth, MAX_DEPTH)

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                WITH RECURSIVE context_graph AS (
                    -- Anchor: focal node
                    SELECT
                        kn.node_id, kn.title, kn.description, kn.epistemic_type,
                        kn.concepts, kn.source_authority, kn.depth AS node_depth,
                        0 AS traverse_depth,
                        ARRAY[kn.node_id] AS path
                    FROM knowledge_nodes kn
                    WHERE kn.node_id = %s AND kn.project_id = %s AND kn.tenant_id = %s

                    UNION ALL

                    -- Expand via edges, filtered by strength
                    SELECT
                        kn.node_id, kn.title, kn.description, kn.epistemic_type,
                        kn.concepts, kn.source_authority, kn.depth AS node_depth,
                        cg.traverse_depth + 1,
                        cg.path || kn.node_id
                    FROM context_graph cg
                    JOIN knowledge_edges ke
                        ON (ke.source_node_id = cg.node_id OR ke.target_node_id = cg.node_id)
                        AND ke.strength >= %s
                    JOIN knowledge_nodes kn
                        ON kn.node_id = CASE
                            WHEN ke.source_node_id = cg.node_id THEN ke.target_node_id
                            ELSE ke.source_node_id
                        END
                    WHERE cg.traverse_depth < %s
                        AND NOT kn.node_id = ANY(cg.path)
                        AND kn.project_id = %s
                        AND kn.tenant_id = %s
                )
                SELECT DISTINCT ON (node_id)
                    node_id, title, description, epistemic_type, concepts,
                    source_authority, node_depth, traverse_depth
                FROM context_graph
                ORDER BY node_id, traverse_depth ASC
            """, (node_id, project_id, tenant_id, min_strength,
                  max_depth, project_id, tenant_id))

            nodes = []
            for r in cur.fetchall():
                nodes.append({
                    "node_id": r[0], "title": r[1], "description": r[2],
                    "epistemic_type": r[3], "concepts": r[4],
                    "source_authority": r[5], "node_depth": r[6],
                    "traverse_depth": r[7],
                })

            # Get edges between the context nodes
            node_ids = [n["node_id"] for n in nodes]
            edges = []
            if node_ids:
                cur.execute("""
                    SELECT edge_id, source_node_id, target_node_id, relationship,
                           strength, evidence, ai_generated
                    FROM knowledge_edges
                    WHERE project_id = %s
                      AND source_node_id = ANY(%s)
                      AND target_node_id = ANY(%s)
                """, (project_id, node_ids, node_ids))
                edges = [
                    {
                        "edge_id": r[0], "source": r[1], "target": r[2],
                        "relationship": r[3], "strength": r[4],
                        "evidence": r[5], "ai_generated": r[6],
                    }
                    for r in cur.fetchall()
                ]

    # Sort by traverse_depth so closest context comes first
    nodes.sort(key=lambda n: n["traverse_depth"])

    return {
        "focal_node_id": node_id,
        "nodes": nodes,
        "edges": edges,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "max_depth": max_depth,
        "min_strength": min_strength,
    }


def serialize_context_for_ai(context: dict, token_budget: int = 4000) -> str:
    """
    Serialize a context graph into a text prompt for the AI.

    Allocates token budget:
      - 40% depth 0-1 (direct context)
      - 30% depth 2-3 (supporting)
      - 20% contrasting/questioning nodes
      - 10% edge metadata
    """
    nodes = context.get("nodes", [])
    edges = context.get("edges", [])
    if not nodes:
        return ""

    # Rough estimate: 1 token ~= 4 chars
    char_budget = token_budget * 4

    sections = []

    # Focal node (always included in full)
    focal = nodes[0] if nodes else None
    if focal:
        sections.append(
            f"## Focus: {focal['title']}\n"
            f"Type: {focal['epistemic_type']} | Authority: {focal['source_authority']}\n"
            f"{focal.get('description') or ''}\n"
            f"Concepts: {', '.join(focal.get('concepts') or [])}"
        )

    # Group nodes by traverse depth
    by_depth: dict[int, list] = {}
    for n in nodes[1:]:
        d = n["traverse_depth"]
        by_depth.setdefault(d, []).append(n)

    # Direct context (depth 1)
    direct = by_depth.get(1, [])
    if direct:
        lines = ["## Direct Context"]
        for n in direct:
            lines.append(f"- [{n['epistemic_type']}] {n['title']}: {(n.get('description') or '')[:200]}")
        sections.append("\n".join(lines))

    # Supporting (depth 2-3)
    supporting = by_depth.get(2, []) + by_depth.get(3, [])
    if supporting:
        lines = ["## Supporting Context"]
        for n in supporting:
            lines.append(f"- [{n['epistemic_type']}] {n['title']}: {(n.get('description') or '')[:150]}")
        sections.append("\n".join(lines))

    # Depth 4 (conclusions/leaf)
    deep = by_depth.get(4, [])
    if deep:
        lines = ["## Deep Context"]
        for n in deep:
            lines.append(f"- [{n['epistemic_type']}] {n['title']}: {(n.get('description') or '')[:100]}")
        sections.append("\n".join(lines))

    # Edges summary
    if edges:
        lines = ["## Relationships"]
        for e in edges[:20]:
            lines.append(f"- {e['source']} --[{e['relationship']} ({e['strength']:.1f})]-> {e['target']}")
            if e.get("evidence"):
                lines.append(f"  Evidence: {e['evidence'][:100]}")
        sections.append("\n".join(lines))

    result = "\n\n".join(sections)

    # Truncate to budget
    if len(result) > char_budget:
        result = result[:char_budget] + "\n\n[Context truncated]"

    return result


def hybrid_search(
    project_id: str,
    tenant_id: str,
    query: str,
    query_embedding: Optional[list[float]] = None,
    limit: int = 20,
    dsn: Optional[str] = None,
) -> list[dict]:
    """
    Hybrid search: combines tsvector full-text + pgvector semantic similarity
    using Reciprocal Rank Fusion (RRF). Falls back to text-only if no embedding.
    """
    k = 60  # RRF constant

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            if query_embedding:
                cur.execute("""
                    WITH text_ranked AS (
                        SELECT node_id, title, description, epistemic_type, concepts, depth,
                               ROW_NUMBER() OVER (
                                   ORDER BY ts_rank(search_vector, plainto_tsquery('english', %s)) DESC
                               ) AS text_rank
                        FROM knowledge_nodes
                        WHERE project_id = %s AND tenant_id = %s
                          AND search_vector @@ plainto_tsquery('english', %s)
                    ),
                    vector_ranked AS (
                        SELECT node_id, title, description, epistemic_type, concepts, depth,
                               ROW_NUMBER() OVER (
                                   ORDER BY embedding <=> %s::vector
                               ) AS vec_rank
                        FROM knowledge_nodes
                        WHERE project_id = %s AND tenant_id = %s
                          AND embedding IS NOT NULL
                    ),
                    combined AS (
                        SELECT
                            COALESCE(t.node_id, v.node_id) AS node_id,
                            COALESCE(t.title, v.title) AS title,
                            COALESCE(t.description, v.description) AS description,
                            COALESCE(t.epistemic_type, v.epistemic_type) AS epistemic_type,
                            COALESCE(t.concepts, v.concepts) AS concepts,
                            COALESCE(t.depth, v.depth) AS depth,
                            COALESCE(1.0 / (%s + t.text_rank), 0) +
                            COALESCE(1.0 / (%s + v.vec_rank), 0) AS rrf_score
                        FROM text_ranked t
                        FULL OUTER JOIN vector_ranked v ON t.node_id = v.node_id
                    )
                    SELECT node_id, title, description, epistemic_type, concepts, depth, rrf_score
                    FROM combined
                    ORDER BY rrf_score DESC
                    LIMIT %s
                """, (query, project_id, tenant_id, query,
                      str(query_embedding), project_id, tenant_id,
                      k, k, min(limit, 200)))
            else:
                # Text-only fallback
                return search_nodes(project_id, tenant_id, query, limit, dsn)

            return [
                {"node_id": r[0], "title": r[1], "description": r[2],
                 "epistemic_type": r[3], "concepts": r[4], "depth": r[5],
                 "relevance": float(r[6])}
                for r in cur.fetchall()
            ]


def find_similar_nodes(
    project_id: str,
    tenant_id: str,
    embedding: list[float],
    threshold: float = 0.85,
    limit: int = 5,
    exclude_node_id: Optional[str] = None,
    dsn: Optional[str] = None,
) -> list[dict]:
    """Find nodes similar by embedding. Used for deduplication."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT node_id, title, epistemic_type, concepts,
                       1 - (embedding <=> %s::vector) AS similarity
                FROM knowledge_nodes
                WHERE project_id = %s AND tenant_id = %s
                  AND embedding IS NOT NULL
                  AND 1 - (embedding <=> %s::vector) > %s
                  AND (%s IS NULL OR node_id != %s)
                ORDER BY similarity DESC
                LIMIT %s
            """, (str(embedding), project_id, tenant_id,
                  str(embedding), threshold,
                  exclude_node_id, exclude_node_id,
                  min(limit, 50)))
            return [
                {"node_id": r[0], "title": r[1], "epistemic_type": r[2],
                 "concepts": r[3], "similarity": float(r[4])}
                for r in cur.fetchall()
            ]


def _get_node_depth(node_id: str, dsn: Optional[str] = None) -> Optional[int]:
    """Get the depth of an existing node."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT depth FROM knowledge_nodes WHERE node_id = %s", (node_id,))
            row = cur.fetchone()
            return row[0] if row else None
