"""
Chat Threads API — multi-thread, multi-model research conversations.

POST   /threads              Create a new thread
GET    /threads              List threads (with filters)
GET    /threads/:id          Get thread with messages
PUT    /threads/:id          Update thread (title, provider, link to case/project)
DELETE /threads/:id          Archive thread
POST   /threads/:id/messages Send message in thread → AI responds + produces nodes
DELETE /threads/:id/messages Clear thread history
GET    /threads/:id/nodes    List knowledge nodes produced by thread
"""

from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from pydantic import BaseModel

import json
import uuid
from datetime import datetime

from storage.postgres.db import get_connection
from core.ai.config import get_tenant_provider
from core.ai.chat import process_chat_message
from core.ai.executor import execute_actions
from core.ai.provider import LLMMessage

router = APIRouter(prefix="/threads", tags=["threads"])


# ─── Models ──────────────────────────────────────────────────────────────────

class CreateThreadRequest(BaseModel):
    title: str
    description: Optional[str] = None
    provider_id: Optional[str] = None
    thread_type: str = "research"       # research, monitoring, triage, analysis
    project_id: Optional[str] = None
    case_id: Optional[str] = None
    auto_run: bool = False
    watch_filter: Optional[dict] = None


class UpdateThreadRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    provider_id: Optional[str] = None
    status: Optional[str] = None
    project_id: Optional[str] = None
    case_id: Optional[str] = None
    auto_run: Optional[bool] = None
    watch_filter: Optional[dict] = None


class SendMessageRequest(BaseModel):
    message: str
    provider_id: Optional[str] = None   # override thread's provider for this message
    context: Optional[dict] = None      # frontend UI context (current page, selected item)


# ─── Thread CRUD ─────────────────────────────────────────────────────────────

@router.post("")
async def create_thread(
    body: CreateThreadRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    thread_id = f"thread-{uuid.uuid4().hex[:12]}"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO chat_threads
                    (thread_id, tenant_id, identity_id, title, description,
                     provider_id, thread_type, project_id, case_id, auto_run, watch_filter)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING thread_id, created_at
            """, (
                thread_id, auth.tenant_id, auth.subject_id,
                body.title, body.description, body.provider_id,
                body.thread_type, body.project_id, body.case_id,
                body.auto_run,
                json.dumps(body.watch_filter) if body.watch_filter else None,
            ))
            row = cur.fetchone()
        conn.commit()

    return {
        "thread_id": row[0],
        "title": body.title,
        "thread_type": body.thread_type,
        "created_at": row[1].isoformat() if row[1] else None,
    }


@router.get("")
async def list_threads(
    status: Optional[str] = Query(None),
    thread_type: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    case_id: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    conditions = ["t.tenant_id = %s"]
    params = [auth.tenant_id]

    if status:
        conditions.append("t.status = %s")
        params.append(status)
    if thread_type:
        conditions.append("t.thread_type = %s")
        params.append(thread_type)
    if project_id:
        conditions.append("t.project_id = %s")
        params.append(project_id)
    if case_id:
        conditions.append("t.case_id = %s")
        params.append(case_id)

    where = " AND ".join(conditions)
    params.append(limit)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT t.thread_id, t.title, t.description, t.provider_id,
                       t.status, t.thread_type, t.project_id, t.case_id,
                       t.auto_run, t.message_count, t.node_count,
                       t.created_at, t.updated_at,
                       p.display_name as provider_name, p.provider as provider_type, p.model as model_name,
                       (SELECT content FROM thread_messages
                        WHERE thread_id = t.thread_id ORDER BY created_at DESC LIMIT 1) as last_message
                FROM chat_threads t
                LEFT JOIN ai_providers p ON p.provider_id = t.provider_id
                WHERE {where}
                ORDER BY t.updated_at DESC
                LIMIT %s
            """, params)
            rows = cur.fetchall()

    threads = []
    for r in rows:
        threads.append({
            "thread_id": r[0], "title": r[1], "description": r[2],
            "provider_id": r[3], "status": r[4], "thread_type": r[5],
            "project_id": r[6], "case_id": r[7], "auto_run": r[8],
            "message_count": r[9], "node_count": r[10],
            "created_at": r[11].isoformat() if r[11] else None,
            "updated_at": r[12].isoformat() if r[12] else None,
            "provider_name": r[13], "provider_type": r[14], "model_name": r[15],
            "last_message": (r[16] or "")[:120],
        })

    return {"threads": threads, "count": len(threads)}


@router.get("/{thread_id}")
async def get_thread(
    thread_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Thread metadata
            cur.execute("""
                SELECT t.thread_id, t.title, t.description, t.provider_id,
                       t.status, t.thread_type, t.project_id, t.case_id,
                       t.auto_run, t.watch_filter, t.message_count, t.node_count,
                       t.created_at, t.updated_at,
                       p.display_name, p.provider, p.model
                FROM chat_threads t
                LEFT JOIN ai_providers p ON p.provider_id = t.provider_id
                WHERE t.thread_id = %s AND t.tenant_id = %s
            """, (thread_id, auth.tenant_id))
            t = cur.fetchone()
            if not t:
                return JSONResponse(status_code=404, content={"error": "Thread not found"})

            # Messages
            cur.execute("""
                SELECT rid, role, content, provider_id, model_used,
                       actions, action_results, nodes_produced, edges_produced,
                       tokens_in, tokens_out, created_at
                FROM thread_messages
                WHERE thread_id = %s AND tenant_id = %s
                ORDER BY created_at ASC
            """, (thread_id, auth.tenant_id))
            messages = []
            for m in cur.fetchall():
                messages.append({
                    "rid": m[0], "role": m[1], "content": m[2],
                    "provider_id": m[3], "model_used": m[4],
                    "actions": m[5], "action_results": m[6],
                    "nodes_produced": m[7] or [], "edges_produced": m[8] or [],
                    "tokens_in": m[9], "tokens_out": m[10],
                    "created_at": m[11].isoformat() if m[11] else None,
                })

    watch_filter = t[9]
    if isinstance(watch_filter, memoryview):
        watch_filter = json.loads(bytes(watch_filter))
    elif isinstance(watch_filter, str):
        watch_filter = json.loads(watch_filter)

    return {
        "thread_id": t[0], "title": t[1], "description": t[2],
        "provider_id": t[3], "status": t[4], "thread_type": t[5],
        "project_id": t[6], "case_id": t[7], "auto_run": t[8],
        "watch_filter": watch_filter, "message_count": t[10], "node_count": t[11],
        "created_at": t[12].isoformat() if t[12] else None,
        "updated_at": t[13].isoformat() if t[13] else None,
        "provider_name": t[14], "provider_type": t[15], "model_name": t[16],
        "messages": messages,
    }


@router.put("/{thread_id}")
async def update_thread(
    thread_id: str,
    body: UpdateThreadRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    sets = []
    params = []
    for field, col in [
        ("title", "title"), ("description", "description"),
        ("provider_id", "provider_id"), ("status", "status"),
        ("project_id", "project_id"), ("case_id", "case_id"),
        ("auto_run", "auto_run"),
    ]:
        val = getattr(body, field)
        if val is not None:
            sets.append(f"{col} = %s")
            params.append(val)
    if body.watch_filter is not None:
        sets.append("watch_filter = %s")
        params.append(json.dumps(body.watch_filter))

    if not sets:
        return {"updated": False}

    sets.append("updated_at = NOW()")
    params.extend([thread_id, auth.tenant_id])

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE chat_threads SET {', '.join(sets)}
                WHERE thread_id = %s AND tenant_id = %s
            """, params)
        conn.commit()

    return {"updated": True, "thread_id": thread_id}


@router.delete("/{thread_id}")
async def archive_thread(
    thread_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE chat_threads SET status = 'archived', updated_at = NOW()
                WHERE thread_id = %s AND tenant_id = %s
            """, (thread_id, auth.tenant_id))
        conn.commit()
    return {"archived": True, "thread_id": thread_id}


# ─── Messages + AI Processing ───────────────────────────────────────────────

@router.post("/{thread_id}/messages")
async def send_message(
    thread_id: str,
    body: SendMessageRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Send a message in a thread. AI responds, executes actions, produces nodes."""

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Verify thread exists
            cur.execute("""
                SELECT thread_id, provider_id, project_id, case_id, thread_type
                FROM chat_threads WHERE thread_id = %s AND tenant_id = %s
            """, (thread_id, auth.tenant_id))
            thread = cur.fetchone()
            if not thread:
                return JSONResponse(status_code=404, content={"error": "Thread not found"})

            # Load conversation history (last 20 messages for context)
            cur.execute("""
                SELECT role, content FROM thread_messages
                WHERE thread_id = %s ORDER BY created_at DESC LIMIT 20
            """, (thread_id,))
            history_rows = cur.fetchall()

    thread_provider_id = body.provider_id or thread[1]
    project_id = thread[2]
    case_id = thread[3]
    thread_type = thread[4]

    # Resolve provider: message override > thread pinned > tenant default
    provider = _resolve_provider(auth.tenant_id, thread_provider_id)
    if not provider:
        return JSONResponse(status_code=200, content={
            "response": "No AI provider configured. Go to Settings → AI to add one.",
            "actions": [], "action_results": [], "nodes_produced": [],
        })

    # Build conversation history (reverse to chronological)
    conversation_history = [
        LLMMessage(role=r[0], content=r[1]) for r in reversed(history_rows)
    ]

    # Save user message
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO thread_messages (thread_id, tenant_id, role, content)
                VALUES (%s, %s, 'user', %s)
            """, (thread_id, auth.tenant_id, body.message))
        conn.commit()

    # Build context for the AI — include pinned/active knowledge nodes
    tenant_context = {
        "tenant_id": auth.tenant_id,
        "thread_type": thread_type,
    }
    if project_id:
        tenant_context["project_id"] = project_id
    if case_id:
        tenant_context["case_id"] = case_id

    # Merge frontend UI context (current page, selected item)
    if body.context:
        tenant_context["ui_context"] = body.context

    # Load thread's knowledge context (pinned nodes always, active nodes summarized)
    node_context = _build_node_context(thread_id, auth.tenant_id)
    if node_context:
        tenant_context["knowledge_context"] = node_context

    # Process through AI
    result = process_chat_message(
        user_message=body.message,
        provider=provider["instance"],
        conversation_history=conversation_history,
        tenant_context=tenant_context,
    )

    # Execute actions
    action_results = []
    if result.get("actions"):
        action_results = execute_actions(
            actions=result["actions"],
            tenant_id=auth.tenant_id,
            identity_id=auth.subject_id,
        )

    # Auto-produce knowledge nodes from action results
    nodes_produced = []
    edges_produced = []
    if project_id and action_results:
        nodes_produced, edges_produced = _auto_produce_nodes(
            action_results=action_results,
            ai_response=result.get("response", ""),
            project_id=project_id,
            case_id=case_id,
            tenant_id=auth.tenant_id,
            thread_id=thread_id,
        )

    # Save assistant message with results
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO thread_messages
                    (thread_id, tenant_id, role, content, provider_id, model_used,
                     actions, action_results, nodes_produced, edges_produced,
                     tokens_in, tokens_out)
                VALUES (%s, %s, 'assistant', %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                thread_id, auth.tenant_id, result.get("response", ""),
                provider.get("provider_id"), result.get("model"),
                json.dumps(result.get("actions", [])),
                json.dumps(action_results),
                nodes_produced or [],
                edges_produced or [],
                result.get("usage", {}).get("prompt_tokens"),
                result.get("usage", {}).get("completion_tokens"),
            ))

            # Update thread stats
            cur.execute("""
                UPDATE chat_threads SET
                    message_count = message_count + 2,
                    node_count = node_count + %s,
                    updated_at = NOW()
                WHERE thread_id = %s
            """, (len(nodes_produced), thread_id))
        conn.commit()

    # Trigger async enrichment on produced nodes (best-effort, non-blocking)
    if nodes_produced:
        import threading
        def _enrich_background():
            try:
                from core.enrichment.node_enrich import enrich_node
                for nid in nodes_produced:
                    enrich_node(nid, auth.tenant_id, provider=provider.get("instance"))
            except Exception:
                pass
        threading.Thread(target=_enrich_background, daemon=True).start()

    return {
        "response": result.get("response", ""),
        "model": result.get("model"),
        "provider": result.get("provider"),
        "actions": result.get("actions", []),
        "action_results": action_results,
        "nodes_produced": nodes_produced,
        "edges_produced": edges_produced,
        "usage": result.get("usage"),
    }


@router.delete("/{thread_id}/messages")
async def clear_messages(
    thread_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM thread_messages WHERE thread_id = %s AND tenant_id = %s
            """, (thread_id, auth.tenant_id))
            cur.execute("""
                UPDATE chat_threads SET message_count = 0, updated_at = NOW()
                WHERE thread_id = %s AND tenant_id = %s
            """, (thread_id, auth.tenant_id))
        conn.commit()
    return {"cleared": True}


@router.get("/{thread_id}/nodes")
async def list_thread_nodes(
    thread_id: str,
    include_unlinked: bool = Query(False),
    rescore: bool = Query(False),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    # Optionally rescore all nodes before returning (triggered on first load or manual refresh)
    if rescore:
        try:
            from core.knowledge.context_scoring import score_thread_nodes
            score_thread_nodes(thread_id, auth.tenant_id)
        except Exception:
            pass  # best-effort, return stale scores if scoring fails

    relevance_filter = "" if include_unlinked else "AND tn.relevance != 'unlinked'"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT kn.node_id, kn.title, kn.epistemic_type, kn.description,
                       kn.source_authority, kn.latitude, kn.longitude, kn.concepts,
                       tn.auto_generated, tn.relevance, tn.user_note,
                       tn.position_x, tn.position_y, tn.created_at,
                       kn.confidence as evidence_quality,
                       tn.context_confidence, tn.context_status, tn.context_evidence,
                       tn.context_contradictions, tn.context_corroborations,
                       tn.context_gaps, tn.context_scored_at,
                       kn.envelope
                FROM thread_nodes tn
                JOIN knowledge_nodes kn ON kn.node_id = tn.node_id
                WHERE tn.thread_id = %s AND kn.tenant_id = %s {relevance_filter}
                ORDER BY
                    CASE tn.relevance
                        WHEN 'pinned' THEN 0
                        WHEN 'active' THEN 1
                        WHEN 'dimmed' THEN 2
                        WHEN 'unlinked' THEN 3
                    END,
                    tn.created_at DESC
            """, (thread_id, auth.tenant_id))
            from core.extraction.envelope import build_envelope_summary
            nodes = []
            for r in cur.fetchall():
                ctx_evidence = r[17]
                if isinstance(ctx_evidence, memoryview):
                    ctx_evidence = json.loads(bytes(ctx_evidence))
                elif isinstance(ctx_evidence, str):
                    try: ctx_evidence = json.loads(ctx_evidence)
                    except: ctx_evidence = {}

                raw_envelope = r[22]
                if isinstance(raw_envelope, memoryview):
                    raw_envelope = json.loads(bytes(raw_envelope))
                elif isinstance(raw_envelope, str):
                    try: raw_envelope = json.loads(raw_envelope)
                    except: raw_envelope = None

                nodes.append({
                    "node_id": r[0], "title": r[1], "epistemic_type": r[2],
                    "description": (r[3] or "")[:200], "source_authority": r[4],
                    "latitude": r[5], "longitude": r[6], "concepts": r[7] or [],
                    "auto_generated": r[8], "relevance": r[9], "user_note": r[10],
                    "position_x": r[11], "position_y": r[12],
                    "created_at": r[13].isoformat() if r[13] else None,
                    # Dual scoring: baseline + contextual
                    "evidence_quality": r[14],
                    "context_confidence": r[15],
                    "context_status": r[16],
                    "context_evidence": ctx_evidence or {},
                    "context_contradictions": r[18] or 0,
                    "context_corroborations": r[19] or 0,
                    "context_gaps": r[20] or [],
                    "context_scored_at": r[21].isoformat() if r[21] else None,
                    # Phase 3.4 — envelope-first contract for chip navigation.
                    # Frontend reads chips from envelope_summary; flat fields
                    # (entity_name, place_name) are legacy and will be removed.
                    "envelope_summary": build_envelope_summary(raw_envelope),
                })

            # Also fetch edges between these nodes
            node_ids = [n["node_id"] for n in nodes]
            edges = []
            if node_ids:
                cur.execute("""
                    SELECT edge_id, source_node_id, target_node_id, relationship, strength
                    FROM knowledge_edges
                    WHERE source_node_id = ANY(%s) AND target_node_id = ANY(%s)
                """, (node_ids, node_ids))
                edges = [{
                    "edge_id": r[0], "source": r[1], "target": r[2],
                    "relationship": r[3], "strength": r[4],
                } for r in cur.fetchall()]

    return {"nodes": nodes, "edges": edges, "count": len(nodes)}


@router.post("/{thread_id}/rescore")
async def rescore_thread(
    thread_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Rescore all nodes in a thread. Called after graph changes or on-demand."""
    from core.knowledge.context_scoring import score_thread_nodes
    results = score_thread_nodes(thread_id, auth.tenant_id)
    return {"rescored": len(results), "results": results}


class UpdateNodeRelevance(BaseModel):
    relevance: str          # pinned, active, dimmed, unlinked
    user_note: Optional[str] = None


class UpdateNodePosition(BaseModel):
    position_x: float
    position_y: float


class BatchNodePositions(BaseModel):
    positions: list[dict]   # [{node_id, position_x, position_y}, ...]


@router.put("/{thread_id}/nodes/{node_id}/relevance")
async def set_node_relevance(
    thread_id: str,
    node_id: str,
    body: UpdateNodeRelevance,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Set node relevance: pinned, active, dimmed, or unlinked."""
    if body.relevance not in ("pinned", "active", "dimmed", "unlinked"):
        return JSONResponse(status_code=400, content={"error": "Invalid relevance"})

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE thread_nodes SET relevance = %s, user_note = %s, updated_at = NOW()
                WHERE thread_id = %s AND node_id = %s
            """, (body.relevance, body.user_note, thread_id, node_id))

            # Update thread node_count (exclude unlinked)
            cur.execute("""
                UPDATE chat_threads SET node_count = (
                    SELECT COUNT(*) FROM thread_nodes
                    WHERE thread_id = %s AND relevance != 'unlinked'
                ) WHERE thread_id = %s
            """, (thread_id, thread_id))
        conn.commit()

    return {"updated": True, "node_id": node_id, "relevance": body.relevance}


@router.put("/{thread_id}/nodes/{node_id}/position")
async def set_node_position(
    thread_id: str,
    node_id: str,
    body: UpdateNodePosition,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Save user-arranged node position in the thread graph."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE thread_nodes SET position_x = %s, position_y = %s, updated_at = NOW()
                WHERE thread_id = %s AND node_id = %s
            """, (body.position_x, body.position_y, thread_id, node_id))
        conn.commit()
    return {"updated": True}


@router.put("/{thread_id}/nodes/positions")
async def batch_update_positions(
    thread_id: str,
    body: BatchNodePositions,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Batch save node positions after drag-and-drop rearranging."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            for pos in body.positions:
                cur.execute("""
                    UPDATE thread_nodes SET position_x = %s, position_y = %s, updated_at = NOW()
                    WHERE thread_id = %s AND node_id = %s
                """, (pos.get("position_x"), pos.get("position_y"), thread_id, pos.get("node_id")))
        conn.commit()
    return {"updated": len(body.positions)}


@router.post("/{thread_id}/nodes/{node_id}/link")
async def link_existing_node(
    thread_id: str,
    node_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Link an existing knowledge node to this thread (e.g., pull in from another thread or project)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO thread_nodes (thread_id, node_id, auto_generated, relevance)
                VALUES (%s, %s, FALSE, 'active')
                ON CONFLICT (thread_id, node_id) DO UPDATE SET relevance = 'active', updated_at = NOW()
            """, (thread_id, node_id))
            cur.execute("""
                UPDATE chat_threads SET node_count = (
                    SELECT COUNT(*) FROM thread_nodes
                    WHERE thread_id = %s AND relevance != 'unlinked'
                ), updated_at = NOW() WHERE thread_id = %s
            """, (thread_id, thread_id))
        conn.commit()
    return {"linked": True, "node_id": node_id}


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _resolve_provider(tenant_id: str, provider_id: str = None) -> Optional[dict]:
    """Resolve an AI provider: specific ID > tenant default > legacy ai_config."""
    from core.ai.provider import get_provider
    from storage.secrets import resolve_secrets

    with get_connection() as conn:
        with conn.cursor() as cur:
            if provider_id:
                cur.execute("""
                    SELECT provider_id, provider, model, config, display_name
                    FROM ai_providers WHERE provider_id = %s AND tenant_id = %s AND enabled = TRUE
                """, (provider_id, tenant_id))
            else:
                # Try default from ai_providers first
                cur.execute("""
                    SELECT provider_id, provider, model, config, display_name
                    FROM ai_providers WHERE tenant_id = %s AND is_default = TRUE AND enabled = TRUE
                    LIMIT 1
                """, (tenant_id,))

            row = cur.fetchone()

    if row:
        config = row[3]
        if isinstance(config, memoryview):
            config = json.loads(bytes(config))
        elif isinstance(config, str):
            config = json.loads(config)
        config = resolve_secrets(config, tenant_id)
        config["model"] = row[2]
        instance = get_provider(row[1], config)
        if instance:
            return {
                "provider_id": row[0],
                "provider_name": row[1],
                "model": row[2],
                "display_name": row[4],
                "instance": instance,
            }

    # Fallback to legacy ai_config
    provider = get_tenant_provider(tenant_id, role="chat")
    if provider:
        return {
            "provider_id": None,
            "provider_name": provider.provider_name,
            "model": getattr(provider, "model", ""),
            "display_name": None,
            "instance": provider,
        }

    return None


def _auto_produce_nodes(
    action_results: list[dict],
    ai_response: str,
    project_id: str,
    case_id: str,
    tenant_id: str,
    thread_id: str,
) -> tuple[list[str], list[str]]:
    """
    Auto-generate knowledge nodes from AI action results.

    When the AI searches signals, finds data, or makes observations,
    we create knowledge nodes so the research is durable — not just chat text.
    """
    nodes = []
    edges = []

    for action in action_results:
        if not action.get("success"):
            continue

        action_type = action.get("action", "")
        data = action.get("data", {})

        # Signal searches → observation nodes summarizing what was found
        if action_type == "search_signals" and data.get("count", 0) > 0:
            # Extract signal RIDs from results for provenance
            signal_rids = []
            for sig in (data.get("signals") or [])[:10]:
                rid = sig.get("rid") or sig.get("signal_rid")
                if rid:
                    signal_rids.append(int(rid))
            node_id = _create_research_node(
                project_id=project_id,
                tenant_id=tenant_id,
                title=f"Signal search: {data.get('count')} results",
                description=ai_response[:500],
                epistemic_type="observation",
                concepts=["signal-search"],
                thread_id=thread_id,
                source_signal_rids=signal_rids,
            )
            if node_id:
                nodes.append(node_id)

        # Case lookups → link thread findings to the case
        elif action_type in ("get_case", "create_case") and data.get("case_id"):
            found_case_id = data["case_id"]
            node_id = _create_research_node(
                project_id=project_id,
                tenant_id=tenant_id,
                title=f"Case reference: {data.get('title', found_case_id)}",
                description=f"Referenced case {found_case_id} during research.",
                epistemic_type="fact",
                concepts=["case-reference"],
                thread_id=thread_id,
            )
            if node_id:
                nodes.append(node_id)

        # Nearby signals → observation about spatial proximity
        elif action_type == "nearby_signals" and data.get("count", 0) > 0:
            node_id = _create_research_node(
                project_id=project_id,
                tenant_id=tenant_id,
                title=f"Proximity analysis: {data['count']} signals nearby",
                description=ai_response[:500],
                epistemic_type="observation",
                concepts=["proximity", "spatial-analysis"],
                thread_id=thread_id,
            )
            if node_id:
                nodes.append(node_id)

        # Live aircraft → observation
        elif action_type == "live_aircraft" and data.get("count", 0) > 0:
            node_id = _create_research_node(
                project_id=project_id,
                tenant_id=tenant_id,
                title=f"Aircraft observation: {data['count']} tracked",
                description=ai_response[:500],
                epistemic_type="observation",
                concepts=["aircraft", "adsb", "live-data"],
                thread_id=thread_id,
            )
            if node_id:
                nodes.append(node_id)

    # If the AI's text response contains analytical language, create a claim/hypothesis node
    if ai_response and project_id and not nodes:
        analysis_markers = ["suggest", "indicat", "likely", "pattern", "anomal",
                           "contradict", "inconsisten", "correlat", "hypothesis"]
        response_lower = ai_response.lower()
        if any(marker in response_lower for marker in analysis_markers):
            node_id = _create_research_node(
                project_id=project_id,
                tenant_id=tenant_id,
                title=f"AI analysis",
                description=ai_response[:800],
                epistemic_type="claim",
                concepts=["ai-analysis"],
                thread_id=thread_id,
            )
            if node_id:
                nodes.append(node_id)

    return nodes, edges


def _build_node_context(thread_id: str, tenant_id: str) -> Optional[str]:
    """Build knowledge context string with contextual scoring.

    Includes per-thread status (confirmed/contested/stale) so the AI knows
    which findings to trust and which to question.
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT kn.title, kn.description, kn.epistemic_type,
                           tn.relevance, tn.context_status, tn.context_confidence,
                           tn.context_contradictions, tn.context_corroborations
                    FROM thread_nodes tn
                    JOIN knowledge_nodes kn ON kn.node_id = tn.node_id
                    WHERE tn.thread_id = %s AND kn.tenant_id = %s
                      AND tn.relevance IN ('pinned', 'active')
                    ORDER BY
                        CASE tn.relevance WHEN 'pinned' THEN 0 ELSE 1 END,
                        COALESCE(tn.context_confidence, 0.5) DESC
                    LIMIT 30
                """, (thread_id, tenant_id))
                rows = cur.fetchall()

        if not rows:
            return None

        parts = ["THREAD KNOWLEDGE (established findings — status shows reliability in THIS investigation):"]
        for title, desc, etype, relevance, status, conf, contradictions, corroborations in rows:
            status_tag = (status or "unscored").upper()
            conf_pct = f"{int((conf or 0.5) * 100)}%" if conf else ""

            if relevance == "pinned":
                line = f"[PINNED {etype.upper()} — {status_tag} {conf_pct}] {title}"
                if desc:
                    line += f": {desc[:300]}"
                if contradictions and contradictions > 0:
                    line += f" ⚠ {contradictions} contradiction(s) in this thread"
            else:
                line = f"[{etype} — {status_tag}] {title}"

            parts.append(line)

        parts.append("")
        parts.append("SCORING KEY: CONFIRMED = multiple corroborations, no contradictions. "
                     "SUPPORTED = some corroboration. CONTESTED = contradictions exist — investigate. "
                     "UNVERIFIED = needs more evidence. STALE = may be outdated.")
        parts.append("Prioritize CONFIRMED and PINNED findings. Flag CONTESTED findings explicitly. "
                     "Do not treat STALE data as current without fresh evidence.")
        return "\n".join(parts)
    except Exception:
        return None


def _create_research_node(
    project_id: str,
    tenant_id: str,
    title: str,
    description: str,
    epistemic_type: str,
    concepts: list[str],
    thread_id: str,
    source_signal_rids: list[int] = None,
) -> Optional[str]:
    """Create a knowledge node through the intake gateway (dedup + filter + evidence profile)."""
    try:
        from core.knowledge.intake import submit_node
        result = submit_node(
            tenant_id=tenant_id,
            title=title,
            description=description,
            epistemic_type=epistemic_type,
            concepts=concepts,
            source_signal_rids=source_signal_rids,
            source_thread_id=thread_id,
            project_id=project_id,
            source_authority="unverified",
        )
        if result.action in ("created", "merged", "review_created"):
            return result.node_id
        return None
    except Exception:
        return None
