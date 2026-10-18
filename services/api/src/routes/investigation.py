"""
Entity Investigation API.

POST /investigation/run           — trigger investigation (sync, returns results)
POST /investigation/queue         — queue investigation (async, returns ID)
GET  /investigation/{id}          — get investigation results
GET  /investigation/entity/{eid}  — list investigations for an entity
GET  /investigation/list          — list all investigations
GET  /investigation/sources       — list available sources

Worker management:
POST /investigation/worker/start  — start background investigation worker
POST /investigation/worker/stop   — stop worker
GET  /investigation/worker/status — get worker status
"""

from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

router = APIRouter(prefix="/investigation", tags=["investigation"])


class InvestigationRequest(BaseModel):
    name: str                            # search term (person name, company, address)
    entity_type: str = "person"          # person, organization, address
    sources: Optional[list[str]] = None  # specific sources, or None for all
    entity_id: Optional[int] = None      # link to existing entity


class QueueRequest(BaseModel):
    name: str
    entity_type: str = "person"
    sources: Optional[list[str]] = None
    entity_id: Optional[int] = None


# ─── Sources ──────────────────────────────────────────────────────────────────

@router.get("/sources")
async def list_sources(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """List all available investigation sources with descriptions."""
    return {
        "sources": [
            {"id": "sec_edgar", "name": "SEC EDGAR",
             "description": "Corporate filings, officers, financials, beneficial ownership",
             "entity_types": ["person", "organization"], "rate_limit": "10 req/s"},
            {"id": "fec", "name": "FEC (Campaign Finance)",
             "description": "Political committees, candidates, donation patterns",
             "entity_types": ["person"], "rate_limit": "30 req/hr (DEMO_KEY)"},
            {"id": "usaspending", "name": "USAspending",
             "description": "Government contracts and grants",
             "entity_types": ["organization"], "rate_limit": "generous"},
            {"id": "propublica_nonprofits", "name": "ProPublica Nonprofits",
             "description": "IRS 990 filings, nonprofit financials",
             "entity_types": ["person", "organization"], "rate_limit": "generous"},
            {"id": "courtlistener", "name": "CourtListener",
             "description": "Federal court cases, dockets, opinions",
             "entity_types": ["person", "organization"], "rate_limit": "5000 req/hr"},
            {"id": "fdic", "name": "FDIC BankFind",
             "description": "Banking institution data, financials",
             "entity_types": ["organization"], "rate_limit": "generous"},
            {"id": "google_books", "name": "Google Books",
             "description": "Published works, biographies, corporate histories",
             "entity_types": ["person", "organization"], "rate_limit": "100 req/day"},
            {"id": "fda", "name": "FDA (openFDA)",
             "description": "Drug/device recalls, enforcement actions, adverse events",
             "entity_types": ["organization"], "rate_limit": "240 req/min (with key)"},
            {"id": "epa_echo", "name": "EPA ECHO",
             "description": "Environmental violations, facilities, compliance history",
             "entity_types": ["organization"], "rate_limit": "generous"},
            {"id": "ofac", "name": "OFAC Sanctions",
             "description": "Treasury sanctions list, PEP screening, global watchlists",
             "entity_types": ["person", "organization"], "rate_limit": "generous"},
            {"id": "congress", "name": "Congress.gov",
             "description": "Legislators, bills, committee records",
             "entity_types": ["person"], "rate_limit": "5000 req/hr"},
            {"id": "loc", "name": "Library of Congress",
             "description": "Historical records, photos, newspapers, manuscripts",
             "entity_types": ["person", "organization"], "rate_limit": "generous"},
            {"id": "sam_gov", "name": "SAM.gov",
             "description": "Federal contractor registration, UEI, CAGE, exclusions",
             "entity_types": ["organization"], "rate_limit": "10 req/day (needs SAM key)"},
        ]
    }


# ─── Run Investigation (Synchronous) ─────────────────────────────────────────

@router.post("/run")
async def run_investigation(
    body: InvestigationRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Run a full investigation synchronously. Returns when complete.

    This blocks until all sources are queried. For large investigations,
    use /investigation/queue instead.
    """
    from core.workers.investigation import run_investigation_sync

    result = run_investigation_sync(
        name=body.name,
        entity_type=body.entity_type,
        tenant_id=auth.tenant_id,
        sources=body.sources,
        entity_id=body.entity_id,
    )

    return result


# ─── Queue Investigation (Async) ─────────────────────────────────────────────

@router.post("/queue")
async def queue_investigation(
    body: QueueRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Queue an investigation for background processing.

    Returns immediately with the investigation ID. Use
    GET /investigation/{id} to check status and results.

    Cooldown rule: if a recent run exists, returns the existing
    investigation_id with a status hint so the UI can deep-link to
    results instead of queueing a redundant run.
    """
    from storage.postgres.db import get_connection
    from core.research.investigation_queue import try_queue_investigation

    # Requests with a known entity_id go through the dedupe helper.
    # Requests with only a seed_name (cascade-style) still fall through
    # to the legacy path — the partial unique index doesn't cover NULL
    # entity_id rows.
    if body.entity_id is not None:
        inv_id, outcome = try_queue_investigation(
            entity_id=body.entity_id,
            tenant_id=auth.tenant_id,
            seed_name=body.name,
            entity_type=body.entity_type,
            sources=body.sources,
            discovered_from="api_queue",
        )
        return {
            "investigation_id": inv_id,
            "status": outcome,
        }

    sources = body.sources or []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO entity_investigations
                    (tenant_id, entity_id, seed_name, seed_type,
                     sources_requested, status, discovered_from)
                VALUES (%s, NULL, %s, %s, %s, 'pending', 'api_queue_by_name')
                RETURNING investigation_id
            """, (
                auth.tenant_id, body.name, body.entity_type, sources,
            ))
            inv_id = cur.fetchone()[0]
        conn.commit()

    return {
        "investigation_id": inv_id,
        "status": "queued",
    }


# ─── Get Investigation Results ────────────────────────────────────────────────

@router.get("/{investigation_id}")
async def get_investigation(
    investigation_id: int,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Get full investigation results including findings and discovered entities."""
    from core.workers.investigation import load_investigation_results

    result = load_investigation_results(investigation_id, auth.tenant_id)
    if result.get("error") == "not_found":
        return JSONResponse(status_code=404, content={"error": "INVESTIGATION.NOT_FOUND"})
    return result


# ─── List Investigations for Entity ──────────────────────────────────────────

@router.get("/entity/{entity_id}")
async def list_entity_investigations(
    entity_id: int,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """List all investigations for a specific entity."""
    from storage.postgres.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT investigation_id, seed_name, seed_type, status,
                       sources_completed, findings_count, entities_discovered,
                       started_at, completed_at
                FROM entity_investigations
                WHERE tenant_id = %s AND entity_id = %s
                ORDER BY created_at DESC
                LIMIT 20
            """, (auth.tenant_id, entity_id))

            investigations = []
            for r in cur.fetchall():
                investigations.append({
                    "investigation_id": r[0],
                    "seed_name": r[1],
                    "seed_type": r[2],
                    "status": r[3],
                    "sources_completed": r[4] or [],
                    "findings_count": r[5],
                    "entities_discovered": r[6],
                    "started_at": r[7].isoformat() if r[7] else None,
                    "completed_at": r[8].isoformat() if r[8] else None,
                })

    return {"entity_id": entity_id, "investigations": investigations}


# ─── List All Investigations ─────────────────────────────────────────────────

@router.get("/list")
async def list_investigations(
    status: Optional[str] = None,
    limit: int = 50,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """List all investigations, optionally filtered by status."""
    from storage.postgres.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            if status:
                cur.execute("""
                    SELECT investigation_id, entity_id, seed_name, seed_type, status,
                           sources_completed, findings_count, entities_discovered,
                           started_at, completed_at
                    FROM entity_investigations
                    WHERE tenant_id = %s AND status = %s
                    ORDER BY created_at DESC LIMIT %s
                """, (auth.tenant_id, status, limit))
            else:
                cur.execute("""
                    SELECT investigation_id, entity_id, seed_name, seed_type, status,
                           sources_completed, findings_count, entities_discovered,
                           started_at, completed_at
                    FROM entity_investigations
                    WHERE tenant_id = %s
                    ORDER BY created_at DESC LIMIT %s
                """, (auth.tenant_id, limit))

            investigations = []
            for r in cur.fetchall():
                investigations.append({
                    "investigation_id": r[0],
                    "entity_id": r[1],
                    "seed_name": r[2],
                    "seed_type": r[3],
                    "status": r[4],
                    "sources_completed": r[5] or [],
                    "findings_count": r[6],
                    "entities_discovered": r[7],
                    "started_at": r[8].isoformat() if r[8] else None,
                    "completed_at": r[9].isoformat() if r[9] else None,
                })

    return {"investigations": investigations, "total": len(investigations)}


# ─── Worker Management ────────────────────────────────────────────────────────

@router.post("/worker/start")
async def start_investigation_worker(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Start the background investigation worker daemon."""
    from core.workers.investigation import start_worker
    return start_worker(auth.tenant_id)


@router.post("/worker/stop")
async def stop_investigation_worker(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Stop the investigation worker."""
    from core.workers.investigation import stop_worker
    return stop_worker()


@router.get("/worker/status")
async def investigation_worker_status(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Get investigation worker status."""
    from core.workers.investigation import get_status
    return get_status()


@router.get("/rate-limits")
async def rate_limit_stats(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Get rate limiter stats for all investigation sources."""
    from core.investigation.sources import get_rate_limiter_stats
    return {"rate_limiters": get_rate_limiter_stats()}
