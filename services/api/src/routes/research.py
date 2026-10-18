"""
Deep-Research API.

POST /research/deep            — start a research run (async, returns run_id)
GET  /research/{id}           — run status + progress steps + cited report
GET  /research                — list runs
GET  /research/list           — alias of the above

Worker management:
POST /research/worker/start   — start the background research worker
POST /research/worker/stop    — stop it
GET  /research/worker/status  — worker status

A run is LLM-directed: a strong external "director" model plans the topic,
web-searches for sources, and Enki grabs those URLs and collapses them into an
auto-created topic library before synthesizing a cited report. Slow → poll GET.
"""

from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

router = APIRouter(prefix="/research", tags=["research"])


class DeepResearchRequest(BaseModel):
    goal: str                              # the research question / topic
    library_id: Optional[int] = None       # reuse an existing corpus, else auto-create
    max_iterations: int = 4


@router.post("/deep")
async def start_deep_research(
    body: DeepResearchRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Start a deep-research run. Creates the run + auto-creates a topic
    library, enqueues it for the background worker, returns immediately."""
    goal = (body.goal or "").strip()
    if not goal:
        return JSONResponse(status_code=400, content={"error": "RESEARCH.GOAL_REQUIRED"})

    from core.workers.research import queue_research_run
    run_id, library_id = queue_research_run(
        goal=goal, tenant_id=auth.tenant_id, identity_id=auth.identity_id,
        library_id=body.library_id, max_iterations=body.max_iterations,
    )
    return {"run_id": run_id, "library_id": library_id, "status": "queued"}


@router.get("/list")
async def list_research_alias(
    status: Optional[str] = None, limit: int = 50,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    from core.workers.research import list_research_runs
    runs = list_research_runs(auth.tenant_id, status=status, limit=limit)
    return {"runs": runs, "total": len(runs)}


@router.get("")
async def list_research(
    status: Optional[str] = None, limit: int = 50,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    from core.workers.research import list_research_runs
    runs = list_research_runs(auth.tenant_id, status=status, limit=limit)
    return {"runs": runs, "total": len(runs)}


# ── Worker management (declared before /{run_id} so paths don't collide) ──────

@router.post("/worker/start")
async def start_research_worker(auth: UnifiedAuthInfo = Depends(unified_auth)):
    from core.workers.research import start_worker
    return start_worker(auth.tenant_id)


@router.post("/worker/stop")
async def stop_research_worker(auth: UnifiedAuthInfo = Depends(unified_auth)):
    from core.workers.research import stop_worker
    return stop_worker()


@router.get("/worker/status")
async def research_worker_status(auth: UnifiedAuthInfo = Depends(unified_auth)):
    from core.workers.research import get_status
    return get_status()


@router.get("/{run_id}")
async def get_research(run_id: int, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Full run status: header + progress steps + cited report."""
    from core.workers.research import load_research_results
    result = load_research_results(run_id, auth.tenant_id)
    if result.get("error") == "not_found":
        return JSONResponse(status_code=404, content={"error": "RESEARCH.NOT_FOUND"})
    return result
