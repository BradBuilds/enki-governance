"""
Worker Management API.

GET  /workers              — list all workers with status + stats
GET  /workers/{id}         — get worker detail
POST /workers/{id}/start   — start a worker
POST /workers/{id}/stop    — stop a worker
POST /workers/{id}/pause   — pause a worker
PATCH /workers/{id}        — update config (instances, batch_size, interval)
POST /workers/{id}/tick    — manually trigger one processing tick
POST /workers/start-all    — start all workers
POST /workers/stop-all     — stop all workers
POST /workers/create       — create a custom worker
DELETE /workers/{id}       — delete a custom worker
"""

from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from pydantic import BaseModel

from storage.signals.workers import (
    list_workers, get_worker, update_worker,
    ensure_default_workers, run_worker_tick,
    create_custom_worker, delete_worker,
)

router = APIRouter(prefix="/workers", tags=["workers"])


class UpdateWorkerRequest(BaseModel):
    instances: Optional[int] = None
    batch_size: Optional[int] = None
    interval_ms: Optional[int] = None
    config: Optional[dict] = None

class CreateWorkerRequest(BaseModel):
    worker_id: str
    worker_type: str
    picks_up_status: str
    sets_status: str
    batch_size: int = 10
    interval_ms: int = 1000
    filter_format: Optional[str] = None
    config: Optional[dict] = None


@router.get("")
async def list_all(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """List all workers with status and stats."""
    ensure_default_workers(auth.tenant_id)
    workers = list_workers(auth.tenant_id)
    return {"workers": workers, "total": len(workers)}


# ─── Feed Enrichment Worker (must be before /{worker_id} routes) ─────
@router.post("/feed-enrichment/start")
async def start_feed_enrichment(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Start the continuous feed enrichment worker (Tier 2 LLM on feed articles)."""
    from core.workers.feed_enrichment import start_worker as start_feed_worker
    return start_feed_worker(auth.tenant_id)


@router.post("/feed-enrichment/stop")
async def stop_feed_enrichment(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Stop the feed enrichment worker."""
    from core.workers.feed_enrichment import stop_worker as stop_feed_worker
    return stop_feed_worker()


@router.get("/feed-enrichment/status")
async def feed_enrichment_status(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Get feed enrichment worker status."""
    from core.workers.feed_enrichment import get_status
    return get_status()


@router.get("/enrichment-queue/status")
async def enrichment_queue_status(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Get the research enrichment-queue worker status (Pipeline B drain)."""
    from core.workers.enrichment_queue_worker import get_status
    return get_status()


@router.get("/investigation-quality/status")
async def investigation_quality_status(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Get the Phase 7c investigation quality validator worker status."""
    from core.workers.investigation_quality_validator import get_status
    return get_status()


@router.get("/finding-relevance/status")
async def finding_relevance_status(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Get the Phase 7d per-finding LLM relevance scorer worker status."""
    from core.workers.finding_relevance_scorer import get_status
    return get_status()


# Phase 3 boundary workers (May 23 2026).
@router.get("/investigation-sweep/status")
async def investigation_sweep_status(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Threshold-cross trigger for entity investigations.

    Closes the boundary failure where _maybe_queue_investigation only
    fired on first-admit. Queues investigations for entities whose
    signal_count crossed 5 after admission and have no active or
    recent (24h cooldown) investigation."""
    from core.workers.investigation_sweep import get_status
    return get_status()


@router.get("/mention-backfill/status")
async def mention_backfill_status(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Resolve document_mentions.entity_id outside the library workflow.

    Closes the boundary failure where collapse_worker._resolve_mentions
    only ran for documents in the library_members table — leaving
    bulk-ingested PDFs unresolved at 2.6%."""
    from core.workers.mention_backfill import get_status
    return get_status()


@router.get("/entity-dedup-sweep/status")
async def entity_dedup_sweep_status(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Auto-merge entity duplicates with matching (normalized_name,
    canonical_type). Replaces the manual scripts/merge_entity_forks.py.
    Dry-run mode controlled by ENKI_DEDUP_DRY_RUN env var."""
    from core.workers.entity_dedup_sweep import get_status
    return get_status()


@router.get("/topic-clusterer/status")
async def topic_clusterer_status(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Get the topic auto-clusterer worker status. The clusterer walks
    signal_clusters, libraries, and high-signal entities every 5 minutes
    and proposes topics through the GDU gateway."""
    from core.workers.topic_auto_clusterer import get_status
    return get_status()


@router.post("/topic-clusterer/start")
async def topic_clusterer_start(auth: UnifiedAuthInfo = Depends(unified_auth)):
    from core.workers.topic_auto_clusterer import start_worker
    return start_worker(auth.tenant_id)


@router.post("/topic-clusterer/stop")
async def topic_clusterer_stop(auth: UnifiedAuthInfo = Depends(unified_auth)):
    from core.workers.topic_auto_clusterer import stop_worker
    return stop_worker()


@router.post("/topic-clusterer/tick")
async def topic_clusterer_tick(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Force a single tick of the topic auto-clusterer (admin/debug)."""
    from core.workers.topic_auto_clusterer import run_tick
    created = run_tick(tenant_id=auth.tenant_id, max_new=10)
    return {"created": created}


@router.get("/fission-detector/status")
async def fission_detector_status(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Get the fission detector worker status."""
    from core.workers.fission_detector import get_status
    return get_status()


@router.post("/fission-detector/start")
async def fission_detector_start(auth: UnifiedAuthInfo = Depends(unified_auth)):
    from core.workers.fission_detector import start_worker
    return start_worker(auth.tenant_id)


@router.post("/fission-detector/stop")
async def fission_detector_stop(auth: UnifiedAuthInfo = Depends(unified_auth)):
    from core.workers.fission_detector import stop_worker
    return stop_worker()


@router.post("/fission-detector/tick")
async def fission_detector_tick(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Force a single tick of the fission detector (admin/debug)."""
    from core.workers.fission_detector import run_tick
    result = run_tick(max_candidates=5, tenant_id=auth.tenant_id)
    return result


@router.get("/federation-subscriber/status")
async def federation_subscriber_status(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Federation subscriber worker status."""
    from core.workers.federation_subscriber import get_status
    return get_status()


@router.post("/federation-subscriber/tick")
async def federation_subscriber_tick(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Force a tick of the federation subscriber (pull recent redirects + apply)."""
    from core.workers.federation_subscriber import run_tick
    return run_tick(tenant_id=auth.tenant_id)


# ─── LLM Entity Confirmer ──────────────────────────────────────────
@router.post("/llm-confirmer/start")
async def start_llm_confirmer(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Start the LLM entity confirmer worker (uses Gemma to merge duplicates)."""
    from core.workers.llm_entity_confirmer import start_worker as start_lc
    return start_lc(auth.tenant_id)


@router.post("/llm-confirmer/stop")
async def stop_llm_confirmer(auth: UnifiedAuthInfo = Depends(unified_auth)):
    from core.workers.llm_entity_confirmer import stop_worker as stop_lc
    return stop_lc()


@router.get("/llm-confirmer/status")
async def llm_confirmer_status(auth: UnifiedAuthInfo = Depends(unified_auth)):
    from core.workers.llm_entity_confirmer import get_status as get_lc_status
    return get_lc_status()


@router.post("/llm-confirmer/run")
async def run_llm_confirmer_once(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Run one cycle of LLM confirmation."""
    from core.workers.llm_entity_confirmer import run_once
    return run_once(auth.tenant_id)


# ─── LLM Entity Validator ──────────────────────────────────────────
@router.post("/llm-validator/start")
async def start_llm_validator(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Start LLM validator (Gemma checks if entities are real or fragments)."""
    from core.workers.llm_entity_validator import start_worker as start_lv
    return start_lv(auth.tenant_id)


@router.post("/llm-validator/stop")
async def stop_llm_validator(auth: UnifiedAuthInfo = Depends(unified_auth)):
    from core.workers.llm_entity_validator import stop_worker as stop_lv
    return stop_lv()


@router.get("/llm-validator/status")
async def llm_validator_status(auth: UnifiedAuthInfo = Depends(unified_auth)):
    from core.workers.llm_entity_validator import get_status as get_lv_status
    return get_lv_status()


@router.post("/llm-validator/run")
async def run_llm_validator_once(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Run one validator cycle."""
    from core.workers.llm_entity_validator import run_once
    return run_once(auth.tenant_id)


# ─── Candidate Promoter Worker ─────────────────────────────────────
@router.post("/candidate-promoter/start")
async def start_candidate_promoter(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Start the candidate promoter worker (promotes entity candidates to real entities)."""
    from core.workers.candidate_promoter import start_worker as start_cp
    return start_cp(auth.tenant_id)


@router.post("/candidate-promoter/stop")
async def stop_candidate_promoter(auth: UnifiedAuthInfo = Depends(unified_auth)):
    from core.workers.candidate_promoter import stop_worker as stop_cp
    return stop_cp()


@router.get("/candidate-promoter/status")
async def candidate_promoter_status(auth: UnifiedAuthInfo = Depends(unified_auth)):
    from core.workers.candidate_promoter import get_status as get_cp_status
    return get_cp_status()


@router.post("/candidate-promoter/run")
async def run_candidate_promoter_once(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Run promotion once (manual trigger)."""
    from core.workers.candidate_promoter import promote_pending
    return promote_pending(auth.tenant_id)


# ─── Node Builder Worker ──────────────────────────────────────────
@router.post("/node-builder/start")
async def start_node_builder(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Start the node builder worker (enrichments → entities → knowledge nodes)."""
    from core.workers.node_builder import start_worker
    return start_worker(auth.tenant_id)


@router.post("/node-builder/stop")
async def stop_node_builder(auth: UnifiedAuthInfo = Depends(unified_auth)):
    from core.workers.node_builder import stop_worker
    return stop_worker()


@router.get("/node-builder/status")
async def node_builder_status(auth: UnifiedAuthInfo = Depends(unified_auth)):
    from core.workers.node_builder import get_status
    return get_status()


# ──�� Investigation Worker ─────────────────────────────────────────
# ─── Article Clusterer Worker ─────────────────────────────────────
@router.post("/article-clusterer/start")
async def start_article_clusterer(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Start the article collapse worker (groups related articles into story clusters)."""
    from core.workers.article_clusterer import start_worker
    return start_worker(auth.tenant_id)


@router.post("/article-clusterer/stop")
async def stop_article_clusterer(auth: UnifiedAuthInfo = Depends(unified_auth)):
    from core.workers.article_clusterer import stop_worker
    return stop_worker()


@router.get("/article-clusterer/status")
async def article_clusterer_status(auth: UnifiedAuthInfo = Depends(unified_auth)):
    from core.workers.article_clusterer import get_status
    return get_status()


@router.post("/article-clusterer/recluster/{topic_id}")
async def recluster_topic(topic_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Clear and rebuild all clusters for a topic from scratch."""
    from core.workers.article_clusterer import recluster_topic
    return recluster_topic(topic_id, auth.tenant_id)


# ─── Geocoder Worker ──────────────────────────────────────────────
@router.post("/geocoder/start")
async def start_geocoder(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Start the geocoder worker (resolves addresses to lat/lon via Nominatim)."""
    from core.workers.geocoder import start_worker
    return start_worker(auth.tenant_id)


@router.post("/geocoder/stop")
async def stop_geocoder(auth: UnifiedAuthInfo = Depends(unified_auth)):
    from core.workers.geocoder import stop_worker
    return stop_worker()


@router.get("/geocoder/status")
async def geocoder_status(auth: UnifiedAuthInfo = Depends(unified_auth)):
    from core.workers.geocoder import get_status
    return get_status()


@router.post("/investigation/start")
async def start_investigation_worker(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Start the entity investigation worker (SEC, FEC, courts, etc.)."""
    from core.workers.investigation import start_worker
    return start_worker(auth.tenant_id)


@router.post("/investigation/stop")
async def stop_investigation_worker(auth: UnifiedAuthInfo = Depends(unified_auth)):
    from core.workers.investigation import stop_worker
    return stop_worker()


@router.get("/investigation/status")
async def investigation_worker_status(auth: UnifiedAuthInfo = Depends(unified_auth)):
    from core.workers.investigation import get_status
    return get_status()


# ─── SIGINT Worker (ESP32 Sentinel) ───────────────────────────────
@router.post("/sigint/start")
async def start_sigint(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Start the SIGINT worker (processes WiFi probe + BLE signals from ESP32 sentinels)."""
    from core.workers.sigint_worker import start_worker as start_sw
    return start_sw(auth.tenant_id)


@router.post("/sigint/stop")
async def stop_sigint(auth: UnifiedAuthInfo = Depends(unified_auth)):
    from core.workers.sigint_worker import stop_worker as stop_sw
    return stop_sw()


@router.get("/sigint/status")
async def sigint_status(auth: UnifiedAuthInfo = Depends(unified_auth)):
    from core.workers.sigint_worker import get_status as get_sw_status
    return get_sw_status()


# ─── Personal Signal Workers ─────────────────────────────────────
@router.post("/personal/start-all")
async def start_all_personal(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Start all 4 personal signal extraction workers (location, contacts, comms, calendar)."""
    from core.workers.personal_supervisor import start_all_personal_workers
    return start_all_personal_workers(auth.tenant_id)


@router.post("/personal/stop-all")
async def stop_all_personal(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Stop all personal signal extraction workers."""
    from core.workers.personal_supervisor import stop_all_personal_workers
    return stop_all_personal_workers()


@router.get("/personal/status")
async def personal_workers_status(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Get unified status of all personal signal extraction workers."""
    from core.workers.personal_supervisor import get_personal_workers_status
    return get_personal_workers_status()


@router.get("/live")
def workers_live(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Lightweight aggregate for the always-visible processing strip.

    Auth-gated (May 28 2026 lockdown): worker counts + throughput would
    otherwise leak operational state to anonymous callers. Polled every
    5-10s by the layout component.
    Defined BEFORE /{worker_id} so FastAPI matches the static path first.
    """
    out = {
        "workers_running": 0,
        "workers_total": 0,
        # Names of workers the supervisor knows about but that are not currently
        # running. Drives the /hub "Needs Attention" panel — we surface specific
        # worker names rather than a "N/M workers" count so operators can act.
        "workers_down": [],
        "throughput_5m": {"statements": 0, "findings": 0, "investigations": 0},
        "queue_pending": {"investigations": 0, "enrichments": 0},
        "last_activity": None,
        "geocoder": {"running": False, "geocoded": 0},
    }
    try:
        from core.workers.supervisor import get_supervisor_status
        s = get_supervisor_status()
        worker_states = s.get("worker_states", {})
        out["workers_total"] = len(worker_states)
        out["workers_running"] = sum(1 for w in worker_states.values() if w.get("running"))
        out["workers_down"] = [
            name for name, w in worker_states.items() if not w.get("running")
        ]
    except Exception:
        pass

    try:
        from core.workers.geocoder import get_status as get_geo_status
        g = get_geo_status()
        out["geocoder"] = {
            "running": bool(g.get("running")),
            "geocoded": int(g.get("geocoded") or 0),
            "phase": g.get("phase"),
        }
    except Exception:
        pass

    try:
        from storage.postgres.db import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                      (SELECT COUNT(*) FROM statements WHERE created_at > NOW() - INTERVAL '5 minutes'),
                      (SELECT COUNT(*) FROM investigation_findings WHERE created_at > NOW() - INTERVAL '5 minutes'),
                      (SELECT COUNT(*) FROM entity_investigations WHERE completed_at > NOW() - INTERVAL '5 minutes'),
                      (SELECT COUNT(*) FROM entity_investigations WHERE status IN ('pending','running'))
                """)
                r = cur.fetchone()
                out["throughput_5m"] = {
                    "statements": int(r[0] or 0),
                    "findings":   int(r[1] or 0),
                    "investigations": int(r[2] or 0),
                }
                out["queue_pending"]["investigations"] = int(r[3] or 0)

                cur.execute("""
                    SELECT MAX(t) FROM (
                      SELECT MAX(created_at) AS t FROM statements
                      UNION ALL
                      SELECT MAX(created_at) AS t FROM investigation_findings
                      UNION ALL
                      SELECT MAX(completed_at) AS t FROM entity_investigations
                    ) x
                """)
                lt = cur.fetchone()[0]
                if lt:
                    out["last_activity"] = lt.isoformat()
    except Exception:
        pass

    return out


@router.get("/{worker_id}")
async def get_one(worker_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    w = get_worker(worker_id, auth.tenant_id)
    if not w:
        return JSONResponse(status_code=404, content={"error": "WORKER.NOT_FOUND"})
    return w


@router.post("/{worker_id}/start")
async def start_worker(worker_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    ok = update_worker(worker_id, auth.tenant_id, status="on")
    if not ok:
        return JSONResponse(status_code=404, content={"error": "WORKER.NOT_FOUND"})
    return {"worker_id": worker_id, "status": "on"}


@router.post("/{worker_id}/stop")
async def stop_worker(worker_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    ok = update_worker(worker_id, auth.tenant_id, status="off")
    if not ok:
        return JSONResponse(status_code=404, content={"error": "WORKER.NOT_FOUND"})
    return {"worker_id": worker_id, "status": "off"}


@router.post("/{worker_id}/pause")
async def pause_worker(worker_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    ok = update_worker(worker_id, auth.tenant_id, status="paused")
    if not ok:
        return JSONResponse(status_code=404, content={"error": "WORKER.NOT_FOUND"})
    return {"worker_id": worker_id, "status": "paused"}


@router.patch("/{worker_id}")
async def update_config(
    worker_id: str,
    body: UpdateWorkerRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    ok = update_worker(
        worker_id, auth.tenant_id,
        instances=body.instances,
        batch_size=body.batch_size,
        interval_ms=body.interval_ms,
        config=body.config,
    )
    if not ok:
        return JSONResponse(status_code=404, content={"error": "WORKER.NOT_FOUND"})
    return get_worker(worker_id, auth.tenant_id)


@router.post("/{worker_id}/tick")
async def manual_tick(worker_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Manually trigger one processing tick. Useful for testing and debugging."""
    # Temporarily set worker to "on" for this tick
    worker = get_worker(worker_id, auth.tenant_id)
    if not worker:
        return JSONResponse(status_code=404, content={"error": "WORKER.NOT_FOUND"})

    # Allow manual tick even if worker is off/paused
    original_status = worker["status"]
    if original_status != "on":
        update_worker(worker_id, auth.tenant_id, status="on")

    result = run_worker_tick(worker_id, auth.tenant_id)

    # Restore original status
    if original_status != "on":
        update_worker(worker_id, auth.tenant_id, status=original_status)

    return result


@router.post("/start-all")
async def start_all(auth: UnifiedAuthInfo = Depends(unified_auth)):
    ensure_default_workers(auth.tenant_id)
    workers = list_workers(auth.tenant_id)
    started = []
    for w in workers:
        update_worker(w["worker_id"], auth.tenant_id, status="on")
        started.append(w["worker_id"])
    return {"started": started, "count": len(started)}


@router.post("/stop-all")
async def stop_all(auth: UnifiedAuthInfo = Depends(unified_auth)):
    workers = list_workers(auth.tenant_id)
    stopped = []
    for w in workers:
        if w["status"] != "off":
            update_worker(w["worker_id"], auth.tenant_id, status="off")
            stopped.append(w["worker_id"])
    return {"stopped": stopped, "count": len(stopped)}


@router.post("/create")
async def create_worker_endpoint(
    body: CreateWorkerRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    result = create_custom_worker(
        worker_id=body.worker_id,
        worker_type=body.worker_type,
        picks_up_status=body.picks_up_status,
        sets_status=body.sets_status,
        tenant_id=auth.tenant_id,
        batch_size=body.batch_size,
        interval_ms=body.interval_ms,
        filter_format=body.filter_format,
        config=body.config,
    )
    return JSONResponse(status_code=201, content=result)


@router.delete("/{worker_id}")
async def delete_worker_endpoint(worker_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    ok = delete_worker(worker_id, auth.tenant_id)
    if not ok:
        return JSONResponse(status_code=400, content={"error": "Cannot delete default workers"})
    return {"deleted": worker_id}


@router.get("/processing/status")
async def processing_status(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Get live status of all background processing: reconciliation, backfill, enrichment."""
    status = {"reconciliation": {}, "backfill": {}, "llm": {}, "connectors": {}}

    # Reconciliation worker
    try:
        from core.workers.reconciliation import get_worker_status, get_llm_load
        status["reconciliation"] = get_worker_status()
        status["llm"] = get_llm_load()
    except Exception:
        status["reconciliation"] = {"running": False, "phase": "not started"}

    # LLM extraction worker (Tier 2)
    try:
        from core.workers.llm_extraction import get_worker_status as get_llm_status
        status["tier2_extraction"] = get_llm_status()
    except Exception:
        status["tier2_extraction"] = {"running": False, "phase": "not started"}

    # Feed enrichment worker (continuous Tier 2 for feed articles)
    try:
        from core.workers.feed_enrichment import get_status as get_feed_enrich_status
        status["feed_enrichment"] = get_feed_enrich_status()
    except Exception:
        status["feed_enrichment"] = {"running": False, "phase": "not started"}

    # Active backfills
    try:
        from storage.postgres.db import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT topic_id, title, backfill_status, backfill_progress
                    FROM watch_topics
                    WHERE tenant_id = %s AND backfill_status = 'running'
                """, (auth.tenant_id,))
                active = []
                for r in cur.fetchall():
                    progress = r[3] or {}
                    if isinstance(progress, str):
                        import json
                        progress = json.loads(progress)
                    active.append({
                        "topic_id": r[0], "title": r[1],
                        "phase": progress.get("phase", ""),
                        "months_completed": progress.get("months_completed", 0),
                        "months_total": progress.get("months_total", 0),
                        "articles": progress.get("fetched", 0),
                        "entities": progress.get("entities_found", 0),
                    })
                status["backfill"]["active"] = active

                # Enrichment queue depth
                try:
                    cur.execute("""
                        SELECT status, count(*) FROM entity_enrichment_queue
                        WHERE tenant_id = %s GROUP BY status
                    """, (auth.tenant_id,))
                    status["enrichment"] = dict(cur.fetchall())
                except Exception:
                    status["enrichment"] = {}

                # Connector stats
                cur.execute("""
                    SELECT count(*) FILTER (WHERE status = 'active') as active,
                           sum(items_ingested) as total_ingested,
                           max(last_fetch_at) as last_fetch
                    FROM connectors WHERE tenant_id = %s
                """, (auth.tenant_id,))
                r = cur.fetchone()
                status["connectors"] = {
                    "active": r[0] or 0,
                    "total_ingested": r[1] or 0,
                    "last_fetch": r[2].isoformat() if r[2] else None,
                }

    except Exception as e:
        status["error"] = str(e)

    # Node builder worker
    try:
        from core.workers.node_builder import get_status as get_nb_status
        status["node_builder"] = get_nb_status()
    except Exception:
        status["node_builder"] = {"running": False, "phase": "not started"}

    # Worker supervisor (auto-restart)
    try:
        from core.workers.supervisor import get_supervisor_status
        status["supervisor"] = get_supervisor_status()
    except Exception:
        status["supervisor"] = {"running": False}

    # Library router worker (signal → library matching)
    try:
        from core.workers.library_router import get_status as get_lr_status
        status["library_router"] = get_lr_status()
    except Exception:
        status["library_router"] = {"running": False, "phase": "not started"}

    # Signal statement extractor (RSS → statements)
    try:
        from core.workers.signal_statement_extractor import get_status as get_sse_status
        status["signal_statement_extractor"] = get_sse_status()
    except Exception:
        status["signal_statement_extractor"] = {"running": False, "phase": "not started"}

    # Predicate auto-extender (grows predicate_dictionary)
    try:
        from core.workers.predicate_extender import get_status as get_pe_status
        status["predicate_extender"] = get_pe_status()
    except Exception:
        status["predicate_extender"] = {"running": False, "phase": "not started"}

    # Library manager (orchestrator) + Discovery continuous (acquire+ingest)
    try:
        from core.workers.library_manager import get_status as get_lm_status
        status["library_manager"] = get_lm_status()
    except Exception:
        status["library_manager"] = {"running": False, "phase": "not started"}
    try:
        from core.workers.discovery_continuous import get_status as get_dc_status
        status["discovery_continuous"] = get_dc_status()
    except Exception:
        status["discovery_continuous"] = {"running": False, "phase": "not started"}
    try:
        from core.workers.collapse_worker import get_status as get_cw_status
        status["collapse_worker"] = get_cw_status()
    except Exception:
        status["collapse_worker"] = {"running": False, "phase": "not started"}

    # ─── Library-pipeline backlog ─────────────────────────────────────
    # What's the actual bottleneck? Where are documents stuck?
    try:
        from storage.postgres.db import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT acquisition_status, COUNT(*)
                      FROM bibliographic_references
                     GROUP BY acquisition_status
                """)
                ref_states = dict(cur.fetchall())

                cur.execute("""
                    SELECT
                      (SELECT COUNT(*) FROM statements WHERE validation_status='pending'),
                      (SELECT COUNT(*) FROM statements WHERE validation_status='rejected'),
                      (SELECT COUNT(*) FROM statements WHERE assessment_node_id IS NOT NULL),
                      (SELECT COUNT(*) FROM assessments),
                      (SELECT COUNT(*) FROM entity_enrichment_queue WHERE status='pending'),
                      (SELECT COUNT(*) FROM predicate_review_queue WHERE status='pending'),
                      (SELECT COUNT(*) FROM library_signals)
                """)
                r = cur.fetchone()

                cur.execute("""
                    SELECT
                      (SELECT COUNT(*) FROM signals WHERE timestamp > NOW() - INTERVAL '1 hour'),
                      (SELECT COUNT(*) FROM entity_mentions WHERE created_at > NOW() - INTERVAL '1 hour'),
                      (SELECT COUNT(*) FROM entities WHERE created_at > NOW() - INTERVAL '1 hour'),
                      (SELECT COUNT(*) FROM statements WHERE created_at > NOW() - INTERVAL '1 hour'),
                      (SELECT COUNT(*) FROM assessments WHERE first_seen > NOW() - INTERVAL '1 hour')
                """)
                throughput = cur.fetchone()

        status["pipeline_backlog"] = {
            "references_pending": ref_states.get("pending", 0),
            "references_acquired_not_ingested": ref_states.get("acquired", 0),
            "references_ingested": ref_states.get("ingested", 0),
            "statements_pending": r[0],
            "statements_rejected": r[1],
            "statements_promoted": r[2],
            "assessments_total": r[3],
            "enrichment_queue_pending": r[4],
            "predicate_review_pending": r[5],
            "library_signals_matched": r[6],
        }
        status["throughput_1h"] = {
            "signals": throughput[0],
            "mentions": throughput[1],
            "entities_created": throughput[2],
            "statements": throughput[3],
            "assessments": throughput[4],
        }
    except Exception as e:
        status["pipeline_backlog"] = {"error": str(e)}

    # Worker manager (pressure-aware scheduler)
    try:
        from core.workers.manager import get_manager_status
        status["manager"] = get_manager_status()
    except Exception:
        status["manager"] = {"running": False}

    # GDU Gateway status
    import os
    gateway_enabled = os.environ.get("ENKI_USE_GDU_GATEWAY", "").lower() in ("true", "1", "yes")
    gdu_status = {"enabled": gateway_enabled}

    if gateway_enabled:
        try:
            import psycopg2
            from storage.config import get_db_dsn
            gdu_conn = psycopg2.connect(get_db_dsn())
            try:
                with gdu_conn.cursor() as cur:
                    cur.execute("SELECT operation, COUNT(*) FROM gdu_admission_log WHERE tenant_id = %s GROUP BY operation", (auth.tenant_id,))
                    gdu_status["admissions"] = dict(cur.fetchall())
                    cur.execute("SELECT COUNT(*) FROM gdu_admission_log WHERE tenant_id = %s", (auth.tenant_id,))
                    gdu_status["total_admissions"] = cur.fetchone()[0]
                    cur.execute("SELECT COUNT(*) FROM knowledge_nodes WHERE base60_address IS NOT NULL AND tenant_id = %s", (auth.tenant_id,))
                    gdu_status["addressed_nodes"] = cur.fetchone()[0]
                    try:
                        cur.execute("SELECT COUNT(*) FROM entity_ldu_p WHERE tenant_id = %s AND status = 'pending'", (auth.tenant_id,))
                        gdu_status["ldup_pending"] = cur.fetchone()[0]
                    except Exception:
                        gdu_conn.rollback()
                        gdu_status["ldup_pending"] = 0
                    cur.execute("SELECT mutation_hash, previous_hash FROM gdu_admission_log WHERE tenant_id = %s ORDER BY rid DESC LIMIT 2", (auth.tenant_id,))
                    chain_rows = cur.fetchall()
                    gdu_status["chain_intact"] = chain_rows[0][1] == chain_rows[1][0] if len(chain_rows) >= 2 else True
            finally:
                gdu_conn.close()
        except Exception as e:
            gdu_status["query_error"] = str(e)[:200]

    status["gdu_gateway"] = gdu_status

    # Learning governor
    try:
        from core.learning.governor import get_governor
        gov = get_governor()
        gov_status = {
            "running": gov._running if hasattr(gov, '_running') else False,
            "workers": [],
        }
        if hasattr(gov, '_workers'):
            for w in gov._workers:
                gov_status["workers"].append({
                    "name": w.name if hasattr(w, 'name') else str(w),
                    "priority": w.priority if hasattr(w, 'priority') else None,
                    "last_run": w.last_run.isoformat() if hasattr(w, 'last_run') and w.last_run else None,
                })
        status["governor"] = gov_status
    except Exception:
        status["governor"] = {"running": False}

    return status
