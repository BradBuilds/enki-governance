"""
Data-integrity validation endpoints.

Wraps core.validation.data_integrity for HTTP callers. Run on demand or
via a low-priority background sweep. Operators look at the report to
decide whether to trigger fixes (entity resolution, edge cleanup,
re-extraction, etc.).

Endpoints:
  GET /validation/data-integrity        — run all checks, return findings
  GET /validation/data-integrity/{name} — run a single check by name

Auth-gated. The report contains entity IDs and document IDs, which we
treat as tenant-scoped data.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

router = APIRouter(prefix="/validation", tags=["validation"])


@router.get("/data-integrity")
async def run_data_integrity(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Run the full data-integrity check suite. Read-only; nothing mutated."""
    from core.validation.data_integrity import run_all
    return run_all()


@router.get("/data-integrity/{check_name}")
async def run_single_check(
    check_name: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Run a single check by function name (e.g. 'check_edges_self_loops').
    Useful when you're debugging one specific issue."""
    from core.validation import data_integrity as di
    fn = getattr(di, check_name, None)
    if not callable(fn) or not check_name.startswith("check_"):
        raise HTTPException(404, f"unknown check: {check_name}")
    from dataclasses import asdict
    return asdict(di._run(fn))


@router.get("/history/latest")
async def latest_validation_run(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Most-recent recorded run from the scheduled validator.
    Read-only — doesn't trigger a fresh run."""
    from core.workers.data_integrity_validator import latest_run
    r = latest_run()
    if not r:
        raise HTTPException(404, "no validation runs recorded yet — start the worker")
    return r


@router.get("/history/regressions")
async def latest_regressions(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Compare the latest run to the previous one. Returns checks whose
    count went UP — what to look at first."""
    from core.workers.data_integrity_validator import regression_check
    return regression_check()


@router.post("/run-now")
async def trigger_validation_run(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Force an immediate run + persistence. Same as the scheduled
    worker's tick but on demand."""
    from core.workers.data_integrity_validator import run_once
    return run_once(triggered_by="api")


@router.post("/worker/start")
async def start_validation_worker(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Start the scheduled validator (runs every 4 hours by default)."""
    from core.workers.data_integrity_validator import start_worker
    return start_worker()


@router.post("/worker/stop")
async def stop_validation_worker(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    from core.workers.data_integrity_validator import stop_worker
    return stop_worker()


@router.get("/worker/status")
async def validation_worker_status(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    from core.workers.data_integrity_validator import get_status
    return get_status()


# ─── Topic Link Worker (auto-aggregate topic libraries from corpora) ───


@router.post("/topic-link/run-now")
async def run_topic_link_now(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Force the topic_link_worker to do an immediate pass. Walks every
    library with a linking_rule, finds matching docs from any corpus
    on this node, adds them to library_members."""
    from core.workers.topic_link_worker import run_once
    return run_once()


@router.post("/topic-link/worker/start")
async def start_topic_link_worker(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Start the periodic topic_link worker (default 10-min interval)."""
    from core.workers.topic_link_worker import start_worker
    return start_worker()


@router.post("/topic-link/worker/stop")
async def stop_topic_link_worker(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    from core.workers.topic_link_worker import stop_worker
    return stop_worker()


@router.get("/topic-link/worker/status")
async def topic_link_worker_status(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    from core.workers.topic_link_worker import get_status
    return get_status()


# ─── Hub Announce Worker (node-A → example.com catalog) ───────────────


@router.post("/hub-announce/run-now")
async def hub_announce_now(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    from core.workers.hub_announce_worker import announce_once
    return announce_once()


@router.post("/hub-announce/worker/start")
async def start_hub_announce_worker(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    from core.workers.hub_announce_worker import start_worker
    return start_worker()


@router.post("/hub-announce/worker/stop")
async def stop_hub_announce_worker(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    from core.workers.hub_announce_worker import stop_worker
    return stop_worker()


@router.get("/hub-announce/worker/status")
async def hub_announce_worker_status(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    from core.workers.hub_announce_worker import get_status
    return get_status()


# ─── Spine validation (governance audit) ────────────────────────────────
@router.get("/spine")
async def spine_audit(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Run the full spine validation harness and return the structured
    report. 18 checks across foundation (keys, profile, active_set),
    governance (triggers, chain integrity, pack positions), runtime
    (worker liveness, recent-contamination rate), and federation
    (library signatures, hub announce recency).

    Read-only; nothing mutated. The UI surfaces this on the admin spine
    page; oncall uses it to confirm the governance posture is still 100%
    after deploys.
    """
    from core.spine.validate import run_all_checks
    return run_all_checks()
