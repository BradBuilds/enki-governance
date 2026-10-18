"""
Knowledge graph API endpoints.

Projects, nodes, edges, graph traversal, search, AI enrichment.
"""

from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from pydantic import BaseModel

from core.knowledge.projects import create_project, get_project, list_projects
from core.knowledge.graph import (
    create_node, create_edge, get_node, get_node_graph,
    get_project_graph, search_nodes, find_by_concept,
    get_context_graph, serialize_context_for_ai, hybrid_search,
    MAX_DEPTH,
)

router = APIRouter(tags=["knowledge graph"])


# ─── Models ───────────────────────────────────────────────────────────────────

class CreateProjectRequest(BaseModel):
    name: str
    description: Optional[str] = None
    routing_rules: Optional[dict] = None

class CreateNodeRequest(BaseModel):
    project_id: str
    title: str
    description: Optional[str] = None
    epistemic_type: str = "observation"
    concepts: Optional[list[str]] = None
    signal_rid: Optional[int] = None
    parent_node_id: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None

class CreateEdgeRequest(BaseModel):
    project_id: str
    source_node_id: str
    target_node_id: str
    relationship: str
    strength: float = 1.0
    evidence: Optional[str] = None

class EnrichRequest(BaseModel):
    signal_id: str
    project_id: str


# ─── Projects ─────────────────────────────────────────────────────────────────

@router.post("/projects")
async def create_project_endpoint(
    body: CreateProjectRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    result = create_project(auth.tenant_id, auth.subject_id, body.name, body.description, body.routing_rules)
    return JSONResponse(status_code=201, content=result)

@router.get("/projects")
async def list_projects_endpoint(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    return {"projects": list_projects(auth.tenant_id, auth.subject_id)}

@router.get("/projects/{project_id}")
async def get_project_endpoint(
    project_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    project = get_project(project_id, auth.tenant_id)
    if not project:
        return JSONResponse(status_code=404, content={"error": "PROJECT.NOT_FOUND"})
    return project


# ─── Nodes ─────────────────────────────────────────────────────────────────────

@router.post("/knowledge/nodes")
async def create_node_endpoint(
    body: CreateNodeRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    result = create_node(
        body.project_id, auth.tenant_id, body.title, body.description,
        body.epistemic_type, body.concepts, body.signal_rid, body.parent_node_id,
        latitude=body.latitude, longitude=body.longitude,
    )
    if not result:
        return JSONResponse(status_code=400, content={"error": "NODE.CREATE_FAILED"})
    if result.get("error"):
        return JSONResponse(status_code=400, content=result)
    return JSONResponse(status_code=201, content=result)


# ── Static /knowledge/nodes/* routes MUST come before /{node_id} ──────────────

@router.get("/knowledge/nodes/recent")
async def recent_nodes(
    limit: int = Query(10, le=50),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Recent knowledge nodes across all projects, for dashboard feed."""
    from storage.postgres.db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT node_id, title, epistemic_type, description, source_authority,
                       project_id, confidence, contradiction_count, corroboration_count,
                       gap_count, enrichment_status, entity_name, entity_type,
                       place_name, event_time, source_count, created_at,
                       envelope
                FROM knowledge_nodes
                WHERE tenant_id = %s
                ORDER BY created_at DESC LIMIT %s
            """, (auth.tenant_id, limit))
            from core.extraction.envelope import build_envelope_summary
            import json as _json
            nodes = []
            for r in cur.fetchall():
                raw_env = r[17]
                if isinstance(raw_env, memoryview):
                    raw_env = _json.loads(bytes(raw_env))
                elif isinstance(raw_env, str):
                    try: raw_env = _json.loads(raw_env)
                    except: raw_env = None
                nodes.append({
                    "node_id": r[0], "title": r[1], "epistemic_type": r[2],
                    "description": (r[3] or "")[:200], "source_authority": r[4],
                    "project_id": r[5], "confidence": r[6],
                    "contradiction_count": r[7] or 0, "corroboration_count": r[8] or 0,
                    "gap_count": r[9] or 0, "enrichment_status": r[10],
                    # NOTE(phase-3.4): entity_name / entity_type / place_name are
                    # LEGACY flat fields retained for backwards compatibility.
                    # Frontend should prefer envelope_summary for chip navigation.
                    "entity_name": r[11], "entity_type": r[12],
                    "place_name": r[13],
                    "event_time": r[14].isoformat() if r[14] else None,
                    "source_count": r[15] or 0,
                    "created_at": r[16].isoformat() if r[16] else None,
                    "envelope_summary": build_envelope_summary(raw_env),
                })
    return {"nodes": nodes, "count": len(nodes)}


@router.get("/knowledge/nodes/search")
async def global_search_nodes(
    q: str = Query(...),
    limit: int = Query(10, le=50),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Search nodes across ALL projects for a tenant. Used by cross-thread import."""
    from storage.postgres.db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT node_id, title, epistemic_type, description, source_authority,
                       project_id, latitude, longitude
                FROM knowledge_nodes
                WHERE tenant_id = %s
                  AND (title ILIKE %s OR description ILIKE %s)
                ORDER BY created_at DESC LIMIT %s
            """, (auth.tenant_id, f"%{q}%", f"%{q}%", limit))
            nodes = [{
                "node_id": r[0], "title": r[1], "epistemic_type": r[2],
                "description": (r[3] or "")[:200], "source_authority": r[4],
                "project_id": r[5], "latitude": r[6], "longitude": r[7],
            } for r in cur.fetchall()]
    return {"nodes": nodes, "count": len(nodes)}


@router.get("/knowledge/nodes/{node_id}")
async def get_node_endpoint(
    node_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    node = get_node(node_id, auth.tenant_id)
    if not node:
        return JSONResponse(status_code=404, content={"error": "NODE.NOT_FOUND"})
    return node


# ─── Edges ─────────────────────────────────────────────────────────────────────

@router.post("/knowledge/edges")
async def create_edge_endpoint(
    body: CreateEdgeRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    result = create_edge(
        body.project_id, body.source_node_id, body.target_node_id,
        body.relationship, body.strength, body.evidence,
    )
    return JSONResponse(status_code=201, content=result)


# ─── Graph Traversal ──────────────────────────────────────────────────────────

@router.get("/knowledge/nodes/{node_id}/graph")
async def node_graph_endpoint(
    node_id: str,
    depth: int = Query(4, le=4),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """
    Get a node's connected subgraph (max 4 depth).

    Returns nodes + edges for visual graph rendering.
    Every node is selectable. Click a node to recenter the graph on it.
    """
    return get_node_graph(node_id, auth.tenant_id, max_depth=depth)

@router.get("/projects/{project_id}/graph")
async def project_graph_endpoint(
    project_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Get the entire project graph for visual mapping."""
    return get_project_graph(project_id, auth.tenant_id)


# ─── Search ────────────────────────────────────────────────────────────────────

@router.get("/knowledge/nodes/{node_id}/context")
async def node_context_endpoint(
    node_id: str,
    project_id: str = Query(...),
    depth: int = Query(4, le=4),
    min_strength: float = Query(0.3, ge=0.0, le=1.0),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """
    Get AI-ready context from the graph: scored traversal up to 4 levels deep.

    Returns nodes + edges sorted by relevance distance from the focal node,
    filtered by edge strength. Use this to build AI prompts.
    """
    return get_context_graph(node_id, project_id, auth.tenant_id, depth, min_strength)


@router.get("/knowledge/search")
async def search_nodes_endpoint(
    project_id: str = Query(...),
    q: str = Query(...),
    limit: int = Query(50, le=200),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Hybrid search: text + semantic (if embeddings available)."""
    # Try to get embedding for semantic search
    query_embedding = None
    try:
        from core.ai.config import get_tenant_provider
        provider = get_tenant_provider(auth.tenant_id)
        if provider and hasattr(provider, "embed"):
            query_embedding = provider.embed(q)
    except Exception:
        pass  # Fall back to text-only

    if query_embedding:
        return {"results": hybrid_search(project_id, auth.tenant_id, q, query_embedding, limit)}
    return {"results": search_nodes(project_id, auth.tenant_id, q, limit)}

@router.get("/knowledge/concepts/{concept}")
async def concept_lookup_endpoint(
    concept: str,
    project_id: str = Query(...),
):
    """Find all nodes that reference a specific concept."""
    return {"concept": concept, "nodes": find_by_concept(project_id, concept)}


# ─── AI Enrichment ─────────────────────────────────────────────────────────────

@router.post("/knowledge/enrich")
async def enrich_signal_endpoint(
    body: EnrichRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """AI-enrich a signal and add to knowledge graph."""
    from storage.signals.store import get_signal
    from core.ai.config import get_tenant_provider
    from core.knowledge.enrich import enrich_signal

    signal = get_signal(body.signal_id, auth.tenant_id)
    if not signal:
        return JSONResponse(status_code=404, content={"error": "SIGNAL.NOT_FOUND"})

    provider = get_tenant_provider(auth.tenant_id)
    if not provider:
        return JSONResponse(status_code=400, content={
            "error": "AI.NOT_CONFIGURED",
            "detail": "Configure AI provider in Settings → AI before enriching."
        })

    # Build embed function if provider supports it
    embed_fn = None
    if hasattr(provider, "embed"):
        embed_fn = provider.embed

    node = enrich_signal(signal, provider, body.project_id, auth.tenant_id, embed_fn=embed_fn)
    if not node:
        return JSONResponse(status_code=500, content={"error": "ENRICH.FAILED"})

    return JSONResponse(status_code=201, content=node)


# ─── Node Enrichment (full L→D→U pipeline) ──────────────────────────────────

@router.post("/knowledge/nodes/{node_id}/enrich")
async def enrich_node_full(
    node_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Run the full L→D→U enrichment pipeline on a node.

    Localizes (geo, temporal, source), Describes (entity, contradictions,
    corroborations, gaps, confidence), and Utilizes (case matching, alerts).
    """
    from core.enrichment.node_enrich import enrich_node
    from core.ai.config import get_tenant_provider

    provider = get_tenant_provider(auth.tenant_id)
    result = enrich_node(node_id, auth.tenant_id, provider=provider)
    return result


# ─── Case ↔ Node Links ────────────────────────────────────────────────────────

# ─── Geo Enrichment ────────────────────────────────────────────────────────────

@router.post("/knowledge/nodes/{node_id}/enrich-location")
async def enrich_node_location(
    node_id: str,
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Auto-enrich a knowledge node with contextual data from public APIs.

    Calls elevation, address, census, flood zone, weather, etc. in parallel.
    Stores results in node.metadata.enrichment.

    Optional body: { "sources": ["elevation", "address", ...] }
    If no sources specified, runs all enabled sources.
    """
    import json as _json
    from core.enrichment.geo_enrich import auto_enrich_location
    from storage.postgres.db import get_connection

    # Get node location
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT latitude, longitude, metadata FROM knowledge_nodes WHERE node_id = %s AND tenant_id = %s",
                (node_id, auth.tenant_id),
            )
            row = cur.fetchone()
            if not row:
                return JSONResponse(status_code=404, content={"error": "NODE.NOT_FOUND"})
            lat, lon = row[0], row[1]
            if lat is None or lon is None:
                return JSONResponse(status_code=400, content={"error": "NODE.NO_LOCATION"})

            metadata = row[2]
            if isinstance(metadata, memoryview):
                metadata = _json.loads(bytes(metadata))
            elif isinstance(metadata, str):
                metadata = _json.loads(metadata)
            elif metadata is None:
                metadata = {}

    # Parse optional source filter
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    sources = body.get("sources")

    # Run enrichment
    results = await auto_enrich_location(lat, lon, enabled_sources=sources)

    # Merge into metadata
    enrichment = metadata.get("enrichment", {})
    for source_id, data in results.items():
        if "_error" not in data:
            enrichment[source_id] = data

    # Store address at top level for easy display
    if "address" in results and "_error" not in results.get("address", {}):
        metadata["address"] = results["address"].get("display_name", "")

    metadata["enrichment"] = enrichment
    metadata["last_enriched"] = __import__('datetime').datetime.now(
        __import__('datetime').timezone.utc
    ).isoformat()

    # Save
    import json as _json2
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE knowledge_nodes SET metadata = %s, updated_at = NOW() WHERE node_id = %s AND tenant_id = %s",
                (_json2.dumps(metadata), node_id, auth.tenant_id),
            )
        conn.commit()

    return {
        "node_id": node_id,
        "enriched_sources": [k for k in results if "_error" not in results[k]],
        "failed_sources": [k for k in results if "_error" in results[k]],
        "results": results,
    }


@router.post("/knowledge/resolve")
async def resolve_entity(
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Resolve an entity (address, company, person) and discover connections.

    Body: {
        "type": "address" | "company" | "person",
        "value": "1247 Harbor Blvd, Tampa, FL" | "Acme Corp" | "John Smith",
        "latitude": 27.94,  // for address type
        "longitude": -82.45,
        "case_id": "case_xxx",  // optional, auto-adds watchlist
        "project_id": "proj_xxx"  // optional
    }
    """
    body = await request.json()
    entity_type = body.get("type", "company")
    value = body.get("value", "")

    if not value:
        return JSONResponse(status_code=400, content={"error": "value is required"})

    from core.enrichment.entity_resolve import resolve_address, resolve_company, resolve_person

    if entity_type == "address":
        lat = body.get("latitude")
        lon = body.get("longitude")
        if lat is None or lon is None:
            # Try geocoding
            import httpx as _httpx
            async with _httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://nominatim.openstreetmap.org/search",
                    params={"q": value, "format": "json", "limit": 1},
                    headers={"User-Agent": "EnkiCommandCenter/1.0"},
                )
                if resp.status_code == 200:
                    results = resp.json()
                    if results:
                        lat = float(results[0]["lat"])
                        lon = float(results[0]["lon"])
            if lat is None:
                return JSONResponse(status_code=400, content={"error": "Could not geocode address"})

        result = await resolve_address(
            value, lat, lon,
            tenant_id=auth.tenant_id,
            case_id=body.get("case_id"),
            project_id=body.get("project_id"),
        )
    elif entity_type == "company":
        result = await resolve_company(value, tenant_id=auth.tenant_id)
    elif entity_type == "person":
        result = await resolve_person(value, tenant_id=auth.tenant_id)
    else:
        return JSONResponse(status_code=400, content={"error": f"Unknown type: {entity_type}"})

    # Build knowledge graph from resolution results
    graph_result = {}
    project_id = body.get("project_id", "proj_default")
    case_id = body.get("case_id")

    try:
        from core.enrichment.graph_builder import build_entity_graph
        graph_result = build_entity_graph(
            resolution_result=result,
            entity_type=entity_type,
            entity_value=value,
            project_id=project_id,
            tenant_id=auth.tenant_id,
            case_id=case_id,
        )
    except Exception as e:
        graph_result = {"error": str(e)}

    return {
        "type": entity_type,
        "query": value,
        "result": result,
        "graph": graph_result,
    }


def _auto_add_watchlist(case_id: str, tenant_id: str, entity_type: str, value: str, result: dict):
    """Auto-add discovered entities as case watchlist observables."""
    from storage.postgres.db import get_connection
    observables = []

    # Add the seed entity
    if entity_type == "company":
        observables.append(("company_name", value))
    elif entity_type == "person":
        observables.append(("person_name", value))

    # Add discovered entities
    for company in result.get("companies", []):
        if company.get("name"):
            observables.append(("company_name", company["name"]))
        if company.get("cik"):
            observables.append(("sec_cik", company["cik"]))
    for asset in result.get("assets", []):
        if asset.get("type") == "aircraft" and asset.get("n_number"):
            observables.append(("aircraft_registration", asset["n_number"]))
    if result.get("gleif", {}).get("lei"):
        observables.append(("lei", result["gleif"]["lei"]))
    if result.get("nonprofit", {}).get("ein"):
        observables.append(("ein", result["nonprofit"]["ein"]))

    with get_connection() as conn:
        with conn.cursor() as cur:
            for obs_type, obs_value in observables:
                try:
                    cur.execute("""
                        INSERT INTO case_observables (case_id, tenant_id, observable_type,
                            observable_value, normalized_value, added_by, notes)
                        VALUES (%s, %s, %s, %s, %s, 'entity_resolver', 'Auto-discovered')
                        ON CONFLICT (case_id, observable_type, normalized_value) DO NOTHING
                    """, (case_id, tenant_id, obs_type, obs_value, obs_value.lower().strip()))
                except Exception:
                    pass
        conn.commit()


@router.get("/enrichment/sources")
async def list_enrichment_sources_endpoint():
    """List all available enrichment data sources."""
    from core.enrichment.geo_enrich import list_enrichment_sources
    return {"sources": list_enrichment_sources()}


@router.get("/enrichment/card-config")
async def enrichment_card_config():
    """Return card display configuration for all enrichment sources.
    The frontend uses this to dynamically render enrichment data."""
    from core.enrichment.card_config import get_card_config
    return {"config": get_card_config()}


@router.get("/data-sources")
async def list_data_sources(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """List all configurable data sources with their status."""
    from storage.postgres.db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT source_id, name, category, enabled, requires_key,
                       api_key IS NOT NULL as has_key, coverage, description,
                       registration_url, key_instructions
                FROM data_source_config
                WHERE tenant_id = %s
                ORDER BY category, name
            """, (auth.tenant_id,))
            sources = [{
                "source_id": r[0], "name": r[1], "category": r[2],
                "enabled": r[3], "requires_key": r[4], "has_key": r[5],
                "coverage": r[6], "description": r[7],
                "registration_url": r[8], "key_instructions": r[9],
            } for r in cur.fetchall()]
    return {"sources": sources}


@router.patch("/data-sources/{source_id}")
async def update_data_source(
    source_id: str,
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Enable/disable a data source or update its API key.

    Body: { "enabled": true/false, "api_key": "...", "config": {...} }
    """
    body = await request.json()
    from storage.postgres.db import get_connection

    updates = []
    params = []

    if "enabled" in body:
        updates.append("enabled = %s")
        params.append(body["enabled"])
    if "api_key" in body:
        updates.append("api_key = %s")
        params.append(body["api_key"])
    if "config" in body:
        import json as _json
        updates.append("config = %s")
        params.append(_json.dumps(body["config"]))

    if not updates:
        return JSONResponse(status_code=400, content={"error": "No updates provided"})

    updates.append("updated_at = NOW()")
    params.extend([source_id, auth.tenant_id])

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE data_source_config SET {', '.join(updates)}
                WHERE source_id = %s AND tenant_id = %s
            """, params)
            if cur.rowcount == 0:
                return JSONResponse(status_code=404, content={"error": "SOURCE.NOT_FOUND"})
        conn.commit()

    return {"source_id": source_id, "updated": True}


@router.post("/knowledge/nodes/{node_id}/link-case")
async def link_node_to_case(
    node_id: str,
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Link a knowledge node to a case."""
    body = await request.json()
    case_id = body.get("case_id")
    if not case_id:
        return JSONResponse(status_code=400, content={"error": "case_id required"})

    from storage.postgres.db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO case_nodes (case_id, node_id, tenant_id, linked_by, link_reason)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (case_id, node_id) DO NOTHING
            """, (case_id, node_id, auth.tenant_id,
                  body.get("linked_by", "manual"),
                  body.get("reason", "")))
        conn.commit()
    return {"linked": True, "case_id": case_id, "node_id": node_id}


@router.get("/knowledge/nodes/{node_id}/cases")
async def get_node_cases(
    node_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """List all cases linked to a knowledge node."""
    from storage.postgres.db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT cn.case_id, c.title, c.status, c.priority, cn.linked_at, cn.link_reason
                FROM case_nodes cn
                JOIN cases c ON c.case_id = cn.case_id AND c.tenant_id = cn.tenant_id
                WHERE cn.node_id = %s AND cn.tenant_id = %s
                ORDER BY cn.linked_at DESC
            """, (node_id, auth.tenant_id))
            cases = [{
                "case_id": r[0], "title": r[1], "status": r[2],
                "priority": r[3], "linked_at": r[4].isoformat() if r[4] else None,
                "link_reason": r[5],
            } for r in cur.fetchall()]
    return {"node_id": node_id, "cases": cases}


@router.get("/cases/{case_id}/nodes")
async def get_case_nodes(
    case_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """List all knowledge nodes linked to a case."""
    from storage.postgres.db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT cn.node_id, kn.title, kn.epistemic_type, kn.latitude, kn.longitude,
                       kn.description, kn.concepts, kn.images, cn.linked_at
                FROM case_nodes cn
                JOIN knowledge_nodes kn ON kn.node_id = cn.node_id
                WHERE cn.case_id = %s AND cn.tenant_id = %s
                ORDER BY cn.linked_at DESC
            """, (case_id, auth.tenant_id))
            nodes = [{
                "node_id": r[0], "title": r[1], "epistemic_type": r[2],
                "latitude": r[3], "longitude": r[4], "description": r[5],
                "concepts": r[6], "images": r[7],
                "linked_at": r[8].isoformat() if r[8] else None,
            } for r in cur.fetchall()]
    return {"case_id": case_id, "nodes": nodes}


# ─── Node Attachments ──────────────────────────────────────────────────────────

@router.post("/knowledge/nodes/{node_id}/attachments")
async def add_node_attachment(
    node_id: str,
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Add an attachment (photo, document, link) to a knowledge node.

    Body: {
        "type": "image" | "document" | "link",
        "url": "https://..." or "/uploads/...",
        "name": "Photo of building",
        "caption": "Front entrance",
        "source": "manual" | "osint" | "public_records"
    }
    """
    body = await request.json()
    att_type = body.get("type", "link")
    url = body.get("url", "")
    name = body.get("name", "")

    if not url and not name:
        return JSONResponse(status_code=400, content={"error": "url or name required"})

    import json as _json
    from storage.postgres.db import get_connection

    attachment = {
        "type": att_type,
        "url": url,
        "name": name,
        "caption": body.get("caption", ""),
        "source": body.get("source", "manual"),
        "added_at": __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
    }

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Get current metadata
            cur.execute("SELECT metadata FROM knowledge_nodes WHERE node_id = %s AND tenant_id = %s",
                       (node_id, auth.tenant_id))
            row = cur.fetchone()
            if not row:
                return JSONResponse(status_code=404, content={"error": "NODE.NOT_FOUND"})

            metadata = row[0]
            if isinstance(metadata, memoryview):
                metadata = _json.loads(bytes(metadata))
            elif isinstance(metadata, str):
                metadata = _json.loads(metadata)
            elif metadata is None:
                metadata = {}

            # Append to attachments list
            attachments = metadata.get("attachments", [])
            attachments.append(attachment)
            metadata["attachments"] = attachments

            cur.execute("""
                UPDATE knowledge_nodes SET metadata = %s, updated_at = NOW()
                WHERE node_id = %s AND tenant_id = %s
            """, (_json.dumps(metadata), node_id, auth.tenant_id))
        conn.commit()

    return {"node_id": node_id, "attachment": attachment, "total": len(attachments)}


@router.get("/knowledge/nodes/{node_id}/attachments")
async def get_node_attachments(
    node_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """List all attachments on a knowledge node."""
    import json as _json
    from storage.postgres.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT metadata FROM knowledge_nodes WHERE node_id = %s AND tenant_id = %s",
                       (node_id, auth.tenant_id))
            row = cur.fetchone()
            if not row:
                return JSONResponse(status_code=404, content={"error": "NODE.NOT_FOUND"})

            metadata = row[0]
            if isinstance(metadata, memoryview):
                metadata = _json.loads(bytes(metadata))
            elif isinstance(metadata, str):
                metadata = _json.loads(metadata)
            elif metadata is None:
                metadata = {}

    return {"node_id": node_id, "attachments": metadata.get("attachments", [])}


# ─── Nearby Signals ────────────────────────────────────────────────────────────

@router.get("/knowledge/nodes/{node_id}/nearby")
async def get_nearby_signals(
    node_id: str,
    radius_m: float = Query(500, description="Radius in meters"),
    limit: int = Query(50),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Find signals near a knowledge node using PostGIS spatial query."""
    from storage.postgres.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Get node location
            cur.execute("""
                SELECT latitude, longitude FROM knowledge_nodes
                WHERE node_id = %s AND tenant_id = %s
            """, (node_id, auth.tenant_id))
            row = cur.fetchone()
            if not row or row[0] is None or row[1] is None:
                return JSONResponse(status_code=404, content={
                    "error": "NODE.NO_LOCATION", "detail": "Node has no lat/lon"
                })

            node_lat, node_lon = row[0], row[1]

            # PostGIS spatial query
            cur.execute("""
                SELECT signal_id, signal_type, device_id, severity, timestamp,
                       latitude, longitude,
                       ST_Distance(
                           ST_MakePoint(longitude, latitude)::geography,
                           ST_MakePoint(%s, %s)::geography
                       ) as distance_m
                FROM signals
                WHERE tenant_id = %s
                  AND latitude IS NOT NULL AND longitude IS NOT NULL
                  AND ST_DWithin(
                      ST_MakePoint(longitude, latitude)::geography,
                      ST_MakePoint(%s, %s)::geography,
                      %s
                  )
                ORDER BY distance_m ASC
                LIMIT %s
            """, (node_lon, node_lat, auth.tenant_id, node_lon, node_lat, radius_m, limit))

            signals = [{
                "signal_id": r[0], "signal_type": r[1], "device_id": r[2],
                "severity": r[3],
                "timestamp": r[4].isoformat() if r[4] else None,
                "latitude": r[5], "longitude": r[6],
                "distance_m": round(r[7], 1),
            } for r in cur.fetchall()]

    return {
        "node_id": node_id,
        "center": {"lat": node_lat, "lon": node_lon},
        "radius_m": radius_m,
        "signals": signals,
        "count": len(signals),
    }
