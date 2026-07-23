"""library_manager.py — orchestrator for library-as-project runs.

Reads library_runs to find the active focus. Drives it through the 5 work
phases (acquire → ingest → collapse → enrich → investigate), advancing
when phase done-criteria met OR no progress for N minutes OR operator
clicks 'next'. When a run completes, picks the next-highest-priority
queued run.

Workers themselves stay generic; this manager just changes the orderING
of work by writing the focus state. The collapse_worker (and friends)
check library_manager.get_current_focus() before pulling their batch.

Public surface:
    start_run(library_id, ...)   create + queue a run
    pause_run(run_id)            mark paused — manager skips it
    resume_run(run_id)           queue → running on next tick
    cancel_run(run_id)           mark failed
    advance_run(run_id)          force phase advance now
    get_current_focus()          for workers to scope their queries
    get_status()                 for /admin/workers dashboard
    start_worker() / stop_worker()
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.library_manager")

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS: dict = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "ticks": 0,
    "phase_advances": 0,
    "runs_completed": 0,
    "current_focus_library_id": None,
    "current_focus_phase": None,
}

# How often the manager ticks
_TICK_INTERVAL_S = 30
# After N minutes of no measurable progress, advance the phase
_STALL_MINUTES = 5

# Phase order
_PHASES = ["discover", "acquire", "ingest", "collapse", "enrich", "investigate", "done"]


# ─── Public worker control ────────────────────────────────────────────────

def start_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_loop, args=(tenant_id,), name="library-manager", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    return {"status": "started"}


def stop_worker():
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=10)
    _STATUS["running"] = False
    return {"status": "stopped"}


def get_status() -> dict:
    return dict(_STATUS)


# ─── Run lifecycle ────────────────────────────────────────────────────────

def start_run(library_id: int, *, priority: int = 100,
              tenant_id: str = "default", started_by: str = "operator",
              note: str = "") -> dict:
    """Queue a new project run for a library. If a run is already open for
    this library, return that one. Manager will pick it up on next tick.

    Sprint 0: pin the GDU active_set at run start. Federation peers must
    advertise the same active_set_version + pack_hash to contribute
    compute. Mismatched peers can still subscribe (read-only)."""
    active_set = _snapshot_active_set()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT run_id, run_status FROM library_runs
                 WHERE library_id = %s
                   AND run_status IN ('queued', 'running', 'paused')
                 LIMIT 1
                """,
                (library_id,),
            )
            existing = cur.fetchone()
            if existing:
                return {"run_id": existing[0], "status": existing[1],
                        "already_open": True}

            # Baseline metric snapshot — lets us compute deltas later.
            baseline = _snapshot_metrics(conn, library_id)
            cur.execute(
                """
                INSERT INTO library_runs
                    (library_id, tenant_id, run_status, priority,
                     metrics, started_by, note,
                     required_active_set_version, required_pack_hash,
                     required_pack_version, active_set_at_start)
                VALUES (%s, %s, 'queued', %s, %s::jsonb, %s, %s,
                        %s, %s, %s, %s::jsonb)
                RETURNING run_id
                """,
                (library_id, tenant_id, priority,
                 json.dumps({"baseline": baseline}), started_by, note,
                 active_set.get("version"),
                 active_set.get("pack_hash"),
                 active_set.get("pack_version"),
                 json.dumps(active_set)),
            )
            run_id = cur.fetchone()[0]
            conn.commit()
    log.info(
        "queued run %d for library %d (priority=%d, active_set=%s)",
        run_id, library_id, priority, active_set.get("version"),
    )
    return {"run_id": run_id, "status": "queued", "baseline": baseline,
            "active_set": active_set}


def _snapshot_active_set() -> dict:
    """Capture the GDU active_set state at run start. This is what
    peers will be checked against when joining the swarm."""
    snap: dict = {"version": None, "pack_hash": None, "pack_version": None}
    try:
        from services.api.src.routes.governance import state as gov_state
        s = gov_state()
        snap["version"] = (
            s.get("active_set_version")
            or s.get("verification", {}).get("active_set_version")
        )
        snap["pack_version"] = s.get("verification", {}).get("pack_version")
    except Exception as e:
        log.debug("active_set snapshot from governance.state failed: %s", e)
    try:
        from core.gdu.pack_registry import get_pack_hash
        snap["pack_hash"] = get_pack_hash()
    except Exception:
        snap["pack_hash"] = None
    return snap


def pause_run(run_id: int) -> dict:
    return _set_status(run_id, "paused")


def resume_run(run_id: int) -> dict:
    return _set_status(run_id, "queued")


def cancel_run(run_id: int) -> dict:
    return _set_status(run_id, "failed", end_now=True)


def advance_run(run_id: int) -> dict:
    """Force the current run to advance to the next phase."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT library_id, current_phase, run_status FROM library_runs WHERE run_id=%s",
                (run_id,),
            )
            row = cur.fetchone()
            if not row:
                return {"error": "run_id not found"}
            library_id, current_phase, run_status = row
            if run_status != "running":
                return {"error": f"run is {run_status}, must be running"}
            next_phase = _next_phase(current_phase)
            _record_phase_transition(conn, run_id, library_id,
                                     current_phase, next_phase,
                                     reason="manual_advance")
            _set_run_phase(conn, run_id, next_phase)
            conn.commit()
    _STATUS["phase_advances"] += 1
    return {"run_id": run_id, "advanced_to": next_phase}


def _set_status(run_id: int, new_status: str, *, end_now: bool = False) -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            extra = ", completed_at = NOW()" if end_now else ""
            cur.execute(
                f"""
                UPDATE library_runs
                   SET run_status = %s, updated_at = NOW(){extra}
                 WHERE run_id = %s
                RETURNING library_id
                """,
                (new_status, run_id),
            )
            row = cur.fetchone()
            conn.commit()
    return {"run_id": run_id, "status": new_status,
            "library_id": row[0] if row else None}


# ─── Focus accessor (workers call this) ───────────────────────────────────

def get_current_focus() -> Optional[dict]:
    """Workers call this to scope their queries. Returns None when no run
    is active — workers fall back to ambient processing."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT run_id, library_id, current_phase, phase_started_at
                      FROM library_runs
                     WHERE run_status = 'running'
                     LIMIT 1
                    """
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "run_id": row[0],
                    "library_id": row[1],
                    "phase": row[2],
                    "phase_started_at": row[3].isoformat() if row[3] else None,
                }
    except Exception as e:
        log.debug("get_current_focus failed: %s", e)
        return None


# ─── Manager loop ─────────────────────────────────────────────────────────

def _loop(tenant_id: str):
    while not _STOP_EVENT.is_set():
        try:
            _tick(tenant_id)
            _STATUS["ticks"] += 1
            _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
        except Exception as e:
            log.warning("manager tick failed: %s", e)
        for _ in range(_TICK_INTERVAL_S):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)
    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"


def _tick(tenant_id: str):
    """One manager pass:
      1. If a run is 'running', evaluate its phase + advance if needed
      2. If no run is 'running', pick the highest-priority queued run
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT run_id, library_id, current_phase, phase_started_at,
                       last_progress_at, metrics
                  FROM library_runs
                 WHERE run_status = 'running'
                 LIMIT 1
                """
            )
            running = cur.fetchone()

        if running:
            run_id, library_id, phase, phase_started, last_progress, metrics = running
            _STATUS["current_focus_library_id"] = library_id
            _STATUS["current_focus_phase"] = phase
            _STATUS["phase"] = f"focus={library_id} phase={phase}"
            _evaluate_running(conn, run_id, library_id, phase,
                              phase_started, last_progress, metrics)
        else:
            _STATUS["current_focus_library_id"] = None
            _STATUS["current_focus_phase"] = None
            _STATUS["phase"] = "no active run — picking next"
            _pick_next(conn)
        conn.commit()


def _evaluate_running(conn, run_id: int, library_id: int,
                      phase: Optional[str],
                      phase_started: Optional[datetime],
                      last_progress: Optional[datetime],
                      metrics: dict):
    """Check if the current phase is done or stalled."""
    if phase is None:
        # First evaluation — start the run + enter first phase
        _record_phase_transition(conn, run_id, library_id, None, _PHASES[0],
                                 reason="run_started")
        _set_run_phase(conn, run_id, _PHASES[0], started=True)
        return

    # Refresh current metrics
    current = _snapshot_metrics(conn, library_id)
    baseline = (metrics or {}).get("baseline", {})

    # Determine if phase is "done" or stalled
    done, done_reason = _phase_done(phase, library_id, current, baseline)
    stalled = False
    stall_reason = None
    if phase_started:
        # No progress = last_progress hasn't advanced
        if last_progress is None:
            last_progress = phase_started
        idle_min = (datetime.now(timezone.utc) - last_progress).total_seconds() / 60
        if idle_min >= _STALL_MINUTES:
            stalled = True
            stall_reason = f"no_progress_for_{int(idle_min)}min"

    # Update progress-watermark if metrics changed since last snapshot
    last_snap_key = f"phase_snapshot_{phase}"
    last_snap = (metrics or {}).get(last_snap_key, {})
    if current != last_snap:
        _update_progress_watermark(conn, run_id, current, phase)

    if done:
        next_phase = _next_phase(phase)
        log.info("run %d: phase %s done (%s) → %s",
                 run_id, phase, done_reason, next_phase)
        _record_phase_transition(conn, run_id, library_id, phase, next_phase,
                                 reason=f"done:{done_reason}")
        _set_run_phase(conn, run_id, next_phase)
        if next_phase == "done":
            _mark_complete(conn, run_id)
        _STATUS["phase_advances"] += 1
    elif stalled:
        next_phase = _next_phase(phase)
        log.info("run %d: phase %s stalled (%s) → %s",
                 run_id, phase, stall_reason, next_phase)
        _record_phase_transition(conn, run_id, library_id, phase, next_phase,
                                 reason=f"stall:{stall_reason}")
        _set_run_phase(conn, run_id, next_phase)
        if next_phase == "done":
            _mark_complete(conn, run_id)
        _STATUS["phase_advances"] += 1


def _pick_next(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT run_id FROM library_runs
             WHERE run_status = 'queued'
             ORDER BY priority DESC, queued_at ASC
             LIMIT 1
            """
        )
        row = cur.fetchone()
        if not row:
            return
        run_id = row[0]
        cur.execute(
            """
            UPDATE library_runs
               SET run_status = 'running',
                   started_at = COALESCE(started_at, NOW()),
                   phase_started_at = NOW(),
                   last_progress_at = NOW(),
                   updated_at = NOW()
             WHERE run_id = %s
            """,
            (run_id,),
        )
        log.info("activated run %d", run_id)


# ─── Phase semantics ──────────────────────────────────────────────────────

def _next_phase(current: Optional[str]) -> str:
    if current is None:
        return _PHASES[0]
    try:
        i = _PHASES.index(current)
        return _PHASES[min(i + 1, len(_PHASES) - 1)]
    except ValueError:
        return _PHASES[0]


def _phase_done(phase: str, library_id: int, current: dict,
                baseline: dict) -> tuple[bool, str]:
    """Done-criteria per phase. Returns (done, reason)."""
    if phase == "discover":
        # Discovery is one-shot per library; for now, considered done if any
        # refs catalogued. (Later: re-discover periodically.)
        if current.get("refs_total", 0) > 0:
            return True, "refs_catalogued"
        return False, ""
    if phase == "acquire":
        # Done when all refs acquired OR no new acquisitions in window
        rt = current.get("refs_total", 0)
        ra = current.get("refs_acquired", 0)
        ri = current.get("refs_ingested", 0)
        if rt > 0 and (ra + ri) >= rt:
            return True, "all_refs_acquired"
        return False, ""
    if phase == "ingest":
        ra = current.get("refs_acquired", 0)
        if ra == 0:
            return True, "no_acquired_to_ingest"
        return False, ""
    if phase == "collapse":
        # Done when no statements remain pending for this lib
        if current.get("statements_pending", 0) == 0:
            return True, "no_pending_statements"
        return False, ""
    if phase == "enrich":
        et = current.get("entities_total", 0)
        en = current.get("entities_enriched", 0)
        if et > 0 and en >= max(et * 0.85, 1):
            return True, "85pct_enriched"
        return False, ""
    if phase == "investigate":
        # Investigation runs on top-N entities; we consider done after 30 done
        # or all known notable entities investigated.
        if current.get("entities_investigated", 0) >= 30:
            return True, "30_investigated"
        return False, ""
    if phase == "done":
        return True, "already_done"
    return False, ""


def _snapshot_metrics(conn, library_id: int) -> dict:
    """Capture key counters for a library. Cheap aggregate queries — these
    run every 30s in the manager loop, so keep them O(1) per index."""
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH ent AS (SELECT member_id FROM library_members
                          WHERE library_id = %s AND member_kind = 'entity'),
            doc_ids AS (SELECT document_id FROM bibliographic_references
                          WHERE library_id = %s AND document_id IS NOT NULL)
            SELECT
              (SELECT COUNT(*) FROM bibliographic_references WHERE library_id = %s),
              (SELECT COUNT(*) FROM bibliographic_references WHERE library_id = %s AND acquisition_status='acquired'),
              (SELECT COUNT(*) FROM bibliographic_references WHERE library_id = %s AND acquisition_status='ingested'),
              (SELECT COUNT(*) FROM document_paragraphs WHERE document_id IN (SELECT document_id FROM doc_ids)),
              (SELECT COUNT(*) FROM statements WHERE document_id IN (SELECT document_id FROM doc_ids)),
              (SELECT COUNT(*) FROM statements s WHERE (s.speaker_entity_id IN (SELECT member_id FROM ent) OR s.object_entity_id IN (SELECT member_id FROM ent)) AND s.validation_status='pending'),
              (SELECT COUNT(*) FROM ent),
              (SELECT COUNT(*) FROM ent e JOIN entity_kb_cache kb ON LOWER(kb.canonical_name) = LOWER((SELECT canonical_name FROM entities WHERE entity_id = e.member_id))),
              (SELECT COUNT(DISTINCT ei.entity_id) FROM entity_investigations ei WHERE ei.entity_id IN (SELECT member_id FROM ent) AND ei.status='complete'),
              (SELECT COUNT(*) FROM assessments a WHERE a.subj_entity_id IN (SELECT member_id FROM ent) OR a.obj_entity_id IN (SELECT member_id FROM ent))
            """,
            (library_id, library_id, library_id, library_id, library_id),
        )
        r = cur.fetchone()
        return {
            "refs_total": r[0] or 0,
            "refs_acquired": r[1] or 0,
            "refs_ingested": r[2] or 0,
            "paragraphs_total": r[3] or 0,
            "statements_total": r[4] or 0,
            "statements_pending": r[5] or 0,
            "entities_total": r[6] or 0,
            "entities_enriched": r[7] or 0,
            "entities_investigated": r[8] or 0,
            "assessments_total": r[9] or 0,
        }


def _set_run_phase(conn, run_id: int, phase: str, *, started: bool = False):
    extra = ", started_at = COALESCE(started_at, NOW())" if started else ""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE library_runs
               SET current_phase = %s,
                   phase_started_at = NOW(),
                   last_progress_at = NOW(),
                   updated_at = NOW()
                   {extra}
             WHERE run_id = %s
            """,
            (phase, run_id),
        )


def _update_progress_watermark(conn, run_id: int, current: dict, phase: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE library_runs
               SET last_progress_at = NOW(),
                   metrics = metrics || jsonb_build_object('current', %s::jsonb,
                                                            %s, %s::jsonb)
             WHERE run_id = %s
            """,
            (json.dumps(current), f"phase_snapshot_{phase}",
             json.dumps(current), run_id),
        )


def _record_phase_transition(conn, run_id: int, library_id: int,
                              from_phase: Optional[str], to_phase: str,
                              *, reason: str):
    snap = _snapshot_metrics(conn, library_id)
    entry = {
        "from": from_phase, "to": to_phase, "reason": reason,
        "at": datetime.now(timezone.utc).isoformat(),
        "snapshot": snap,
    }
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE library_runs
               SET phase_history = phase_history || %s::jsonb,
                   updated_at = NOW()
             WHERE run_id = %s
            """,
            (json.dumps([entry]), run_id),
        )


def _mark_complete(conn, run_id: int):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE library_runs
               SET run_status = 'complete',
                   completed_at = NOW(),
                   updated_at = NOW()
             WHERE run_id = %s
            """,
            (run_id,),
        )
    _STATUS["runs_completed"] += 1
    log.info("run %d complete", run_id)
