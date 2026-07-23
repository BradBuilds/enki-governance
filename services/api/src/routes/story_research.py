"""
Story research — kick off the read-summarize-cross-check pipeline on
any knowledge_nodes row, poll the result.

    POST /stories/{node_id}/research  → enqueues, returns research row
    GET  /stories/{node_id}/research  → current state, result if done
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

log = logging.getLogger("enki.api.story_research")

router = APIRouter(tags=["story-research"])


def _get_conn():
    from storage.postgres.db import get_connection
    return get_connection()


def _row_to_dict(row) -> dict:
    return {
        "research_id": row[0],
        "node_id": row[1],
        "status": row[2],
        "current_stage": row[3],
        "stages_done": row[4],
        "stages_total": row[5],
        "error": row[6],
        "summary": row[7],
        "claims": row[8] or [],
        "conflicts": row[9] or [],
        "sources_used": row[10] or [],
        "entities_found": row[11] or [],
        "started_at": row[12].isoformat() if row[12] else None,
        "completed_at": row[13].isoformat() if row[13] else None,
        "gemma_calls": row[14],
        "source_count": row[15],
        "updated_at": row[16].isoformat() if row[16] else None,
    }


_SELECT = """
    SELECT research_id, node_id, status, current_stage,
           stages_done, stages_total, error,
           summary, claims, conflicts, sources_used, entities_found,
           started_at, completed_at, gemma_calls, source_count, updated_at
      FROM story_research
     WHERE tenant_id = %s AND node_id = %s
"""


def _start_in_thread(research_id: int, node_id: str, tenant_id: str):
    """Spawn the orchestrator in a daemon thread so the HTTP response
    returns immediately. FastAPI BackgroundTasks would tie up the
    request worker for ~60s; a thread is cheap and proper."""
    from core.research.story_research import run_story_research

    def _target():
        try:
            run_story_research(research_id, node_id, tenant_id)
        except Exception:
            log.exception("research thread crashed (research_id=%s)", research_id)

    t = threading.Thread(target=_target, daemon=True, name=f"story-research-{research_id}")
    t.start()


@router.post("/stories/{node_id}/research")
async def start_research(
    node_id: str,
    force: bool = False,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Start (or restart) research on a story node. Returns the
    research row immediately; the worker runs in the background.

    If a run is already in flight, returns its current state without
    starting a new one. Pass ?force=true to restart."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT, (auth.tenant_id, node_id))
            existing = cur.fetchone()

            if existing and existing[2] == "running" and not force:
                return _row_to_dict(existing)

            if existing:
                cur.execute(
                    """
                    UPDATE story_research
                       SET status = 'pending',
                           current_stage = NULL,
                           stages_done = 0,
                           error = NULL,
                           started_at = NULL,
                           completed_at = NULL,
                           summary = NULL,
                           claims = NULL,
                           conflicts = NULL,
                           sources_used = NULL,
                           entities_found = NULL,
                           gemma_calls = 0,
                           source_count = 0,
                           updated_at = NOW()
                     WHERE research_id = %s
                     RETURNING research_id
                    """,
                    (existing[0],),
                )
                research_id = cur.fetchone()[0]
            else:
                cur.execute(
                    """
                    INSERT INTO story_research (tenant_id, node_id, status, stages_total)
                    VALUES (%s, %s, 'pending', 6)
                    RETURNING research_id
                    """,
                    (auth.tenant_id, node_id),
                )
                research_id = cur.fetchone()[0]
            conn.commit()

    _start_in_thread(research_id, node_id, auth.tenant_id)

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT, (auth.tenant_id, node_id))
            row = cur.fetchone()
    return _row_to_dict(row) if row else {"research_id": research_id, "status": "pending"}


@router.get("/stories/{node_id}/research")
async def get_research(
    node_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Get the current research state for a story. Returns 404 if no
    research has been started."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT, (auth.tenant_id, node_id))
            row = cur.fetchone()
    if not row:
        return JSONResponse(status_code=404, content={"error": "no research for this node"})
    return _row_to_dict(row)
