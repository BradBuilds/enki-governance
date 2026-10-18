"""
Deep-Research Worker — drives the LLM-directed research loop.

Runs as a daemon thread (mirrors core/workers/investigation.py). Picks up
research_runs with status='queued' using FOR UPDATE SKIP LOCKED — the only
pattern safe under the api's `--workers 2` (the DB row-lock serializes the
claim across both uvicorn workers). The slow work — planning, web search,
grabbing URLs, collapse, synthesis — happens in core/research/orchestrator.py;
this worker persists incremental status so GET /research/{id} can poll.

  start_worker() / stop_worker() / get_status()
  queue_research_run(goal, ...) -> (run_id, library_id)   # insert + auto-create library
  load_research_results(run_id, tenant_id) -> dict
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection
from core.workers.stuck_running_reaper import reap_stuck_rows

log = logging.getLogger("enki.workers.research")

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "runs_completed": 0,
    "errors": 0,
}

CHECK_INTERVAL = 15         # seconds between polls
BATCH_SIZE = 1              # research runs are heavy — one at a time
REAP_EVERY_N_TICKS = 40     # ~10 min

# statuses that mean "in flight" (used by the stuck-row reaper + polling)
_ACTIVE_STATUSES = ("planning", "gathering", "collapsing", "assessing", "synthesizing")


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_worker_loop, args=(tenant_id,),
        name="research-worker", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    _STATUS["phase"] = "starting"
    try:
        reap_stuck_rows("research_runs", tenant_id=tenant_id)
    except Exception as e:
        log.debug("research reap on start failed: %s", e)
    log.info("Research worker started")
    return {"status": "started"}


def stop_worker():
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=10)
    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"
    return {"status": "stopped"}


def _worker_loop(tenant_id: str):
    _STATUS["running"] = True
    backoff = CHECK_INTERVAL
    tick = 0
    while not _STOP_EVENT.is_set():
        try:
            tick += 1
            if tick % REAP_EVERY_N_TICKS == 0:
                try:
                    reap_stuck_rows("research_runs", tenant_id=tenant_id)
                except Exception:
                    pass
            processed = _process_batch(tenant_id)
            if processed > 0:
                backoff = CHECK_INTERVAL
                _STATUS["phase"] = "processing"
            else:
                backoff = min(backoff + 10, 120)
                _STATUS["phase"] = "idle — no queued research"
        except Exception as e:
            log.warning("Research worker error: %s", e)
            _STATUS["errors"] += 1
            _STATUS["phase"] = f"error: {str(e)[:60]}"
            backoff = min(backoff * 2, 300)

        for _ in range(int(backoff)):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)

    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"


def _process_batch(tenant_id: str) -> int:
    runs = _find_queued(tenant_id, BATCH_SIZE)
    if not runs:
        return 0
    processed = 0
    for run in runs:
        try:
            _run_research_run(run, tenant_id)
            processed += 1
            _STATUS["runs_completed"] += 1
        except Exception as e:
            log.warning("Research run %d failed: %s", run["run_id"], e)
            _mark_failed(run["run_id"], str(e))
            _STATUS["errors"] += 1
    _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
    return processed


def _find_queued(tenant_id: str, limit: int) -> list:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT run_id, goal, library_id, COALESCE(max_iterations, 4)
                      FROM research_runs
                     WHERE tenant_id = %s AND status = 'queued'
                     ORDER BY created_at ASC
                     LIMIT %s
                     FOR UPDATE SKIP LOCKED
                """, (tenant_id, limit))
                rows = cur.fetchall()
                results = []
                for r in rows:
                    cur.execute("""
                        UPDATE research_runs
                           SET status = 'planning', started_at = NOW()
                         WHERE run_id = %s
                    """, (r[0],))
                    results.append({
                        "run_id": r[0], "goal": r[1],
                        "library_id": r[2], "max_iterations": r[3] or 4,
                    })
                conn.commit()
                return results
    except Exception as e:
        log.debug("find queued research failed: %s", e)
        return []


# ── Progress persistence (the polling surface) ──────────────────────────────

_PHASE_STATUS = {"planning", "gathering", "collapsing", "assessing", "synthesizing"}


def _make_progress(run_id: int, tenant_id: str):
    """Return a callback the orchestrator calls after each phase. Persists a
    research_run_steps row and updates the run header so GET can poll it."""
    def cb(phase: Optional[str] = None, title: Optional[str] = None,
           detail: Optional[dict] = None, iteration: Optional[int] = None,
           sources_grabbed: Optional[int] = None,
           entities_count: Optional[int] = None,
           facts_count: Optional[int] = None,
           director_model: Optional[str] = None):
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    if title:
                        cur.execute("""
                            INSERT INTO research_run_steps
                                (run_id, tenant_id, iteration, phase, title, detail)
                            VALUES (%s, %s, %s, %s, %s, %s)
                        """, (run_id, tenant_id, iteration or 0,
                              phase or "planning", title[:500],
                              json.dumps(detail or {})))
                    # Build a dynamic header update from whatever was supplied.
                    sets, vals = [], []
                    if phase in _PHASE_STATUS:
                        sets.append("status = %s"); vals.append(phase)
                    if title:
                        sets.append("phase_detail = %s"); vals.append(title[:500])
                    if iteration is not None:
                        sets.append("iteration = %s"); vals.append(iteration)
                    if sources_grabbed is not None:
                        sets.append("sources_grabbed = %s"); vals.append(sources_grabbed)
                    if entities_count is not None:
                        sets.append("entities_count = %s"); vals.append(entities_count)
                    if facts_count is not None:
                        sets.append("facts_count = %s"); vals.append(facts_count)
                    if director_model:
                        sets.append("director_model = %s"); vals.append(director_model)
                    if sets:
                        vals.append(run_id)
                        cur.execute(
                            f"UPDATE research_runs SET {', '.join(sets)} WHERE run_id = %s",
                            vals)
                conn.commit()
        except Exception as e:
            log.debug("progress persist failed for run %d: %s", run_id, e)
    return cb


def _run_research_run(run: dict, tenant_id: str):
    """Drive the orchestrator loop for one queued run."""
    from core.research.orchestrator import run_research
    from core.research.credentials import set_tenant_context

    set_tenant_context(tenant_id)
    run_id = run["run_id"]
    progress = _make_progress(run_id, tenant_id)

    result = run_research(
        run_id=run_id, goal=run["goal"], library_id=run["library_id"],
        tenant_id=tenant_id, progress=progress,
        max_iterations=run.get("max_iterations", 4),
    )

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE research_runs
                   SET status = 'done',
                       report_markdown = %s,
                       report_citations = %s,
                       iteration = %s,
                       sources_grabbed = %s,
                       entities_count = %s,
                       facts_count = %s,
                       tokens_used = %s,
                       director_model = COALESCE(%s, director_model),
                       phase_detail = 'Report ready',
                       completed_at = NOW()
                 WHERE run_id = %s
            """, (
                result.get("report_markdown"),
                json.dumps(result.get("report_citations", [])),
                result.get("iterations", 0),
                result.get("sources_grabbed", 0),
                result.get("entities_count", 0),
                result.get("facts_count", 0),
                result.get("tokens_used", 0),
                result.get("director_model"),
                run_id,
            ))
        conn.commit()
    log.info("Research run %d complete: %d sources, %d entities, %d facts",
             run_id, result.get("sources_grabbed", 0),
             result.get("entities_count", 0), result.get("facts_count", 0))


def _mark_failed(run_id: int, error: str):
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE research_runs
                       SET status = 'failed', error = %s,
                           phase_detail = %s, completed_at = NOW()
                     WHERE run_id = %s
                """, (error[:1000], error[:200], run_id))
            conn.commit()
    except Exception as e:
        log.debug("mark research failed error: %s", e)


# ── Queue + auto-create library ─────────────────────────────────────────────

def queue_research_run(
    goal: str,
    tenant_id: str = "default",
    identity_id: Optional[str] = None,
    library_id: Optional[int] = None,
    max_iterations: int = 4,
) -> tuple[int, int]:
    """Create a queued research run + (unless given) an auto-created topic
    library as the corpus. Returns (run_id, library_id)."""
    goal = (goal or "").strip()
    if not goal:
        raise ValueError("goal is required")

    with get_connection() as conn:
        with conn.cursor() as cur:
            if library_id is None:
                # Assign a proper base-60 address when the allocator is available;
                # fall back to '' (the federation-mirror path uses '' too).
                addr = ""
                try:
                    from core.gdu.address import assign_address
                    addr = assign_address(conn, "library", "topic_library") or ""
                except Exception:
                    addr = ""
                canonical = f"topic_library/{goal}"[:500]
                cur.execute("""
                    INSERT INTO libraries (
                        base60_address, library_subtype, canonical_name,
                        focal_term, description, tenant_id, discovery_status
                    ) VALUES (%s, 'topic_library', %s, %s, %s, %s, 'active')
                    RETURNING library_id
                """, (addr, canonical, goal[:300],
                      f"Deep-research corpus for: {goal}"[:500], tenant_id))
                library_id = int(cur.fetchone()[0])

            cur.execute("""
                INSERT INTO research_runs
                    (tenant_id, identity_id, goal, status, library_id, max_iterations)
                VALUES (%s, %s, %s, 'queued', %s, %s)
                RETURNING run_id
            """, (tenant_id, identity_id, goal, library_id, max_iterations))
            run_id = int(cur.fetchone()[0])
        conn.commit()

    log.info("Queued research run %d (library %d): %s", run_id, library_id, goal[:80])
    return run_id, library_id


# ── Results loader (polling + report) ───────────────────────────────────────

def load_research_results(run_id: int, tenant_id: str) -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT run_id, goal, status, library_id, project_id,
                       director_model, iteration, max_iterations,
                       sources_grabbed, entities_count, facts_count,
                       tokens_used, report_markdown, report_citations,
                       phase_detail, error, started_at, completed_at, created_at
                  FROM research_runs
                 WHERE run_id = %s AND tenant_id = %s
            """, (run_id, tenant_id))
            row = cur.fetchone()
            if not row:
                return {"error": "not_found"}
            run = {
                "run_id": row[0], "goal": row[1], "status": row[2],
                "library_id": row[3], "project_id": row[4],
                "director_model": row[5], "iteration": row[6],
                "max_iterations": row[7], "sources_grabbed": row[8],
                "entities_count": row[9], "facts_count": row[10],
                "tokens_used": row[11], "report_markdown": row[12],
                "report_citations": row[13] or [],
                "phase_detail": row[14], "error": row[15],
                "started_at": row[16].isoformat() if row[16] else None,
                "completed_at": row[17].isoformat() if row[17] else None,
                "created_at": row[18].isoformat() if row[18] else None,
            }
            cur.execute("""
                SELECT iteration, phase, title, detail, created_at
                  FROM research_run_steps
                 WHERE run_id = %s
                 ORDER BY step_id ASC
                 LIMIT 200
            """, (run_id,))
            run["steps"] = [{
                "iteration": s[0], "phase": s[1], "title": s[2],
                "detail": s[3] or {},
                "created_at": s[4].isoformat() if s[4] else None,
            } for s in cur.fetchall()]
    return run


def list_research_runs(tenant_id: str, status: Optional[str] = None,
                       limit: int = 50) -> list:
    with get_connection() as conn:
        with conn.cursor() as cur:
            if status:
                cur.execute("""
                    SELECT run_id, goal, status, library_id, director_model,
                           iteration, max_iterations, sources_grabbed,
                           entities_count, facts_count, created_at, completed_at
                      FROM research_runs
                     WHERE tenant_id = %s AND status = %s
                     ORDER BY created_at DESC LIMIT %s
                """, (tenant_id, status, limit))
            else:
                cur.execute("""
                    SELECT run_id, goal, status, library_id, director_model,
                           iteration, max_iterations, sources_grabbed,
                           entities_count, facts_count, created_at, completed_at
                      FROM research_runs
                     WHERE tenant_id = %s
                     ORDER BY created_at DESC LIMIT %s
                """, (tenant_id, limit))
            out = []
            for r in cur.fetchall():
                out.append({
                    "run_id": r[0], "goal": r[1], "status": r[2],
                    "library_id": r[3], "director_model": r[4],
                    "iteration": r[5], "max_iterations": r[6],
                    "sources_grabbed": r[7], "entities_count": r[8],
                    "facts_count": r[9],
                    "created_at": r[10].isoformat() if r[10] else None,
                    "completed_at": r[11].isoformat() if r[11] else None,
                })
    return out
