"""
Data-Integrity Validator Worker.

Runs `core.validation.data_integrity.run_all()` on a schedule and persists
every Finding into validation_runs + validation_findings. The history
lets the operator notice regressions (e.g. self-loops creeping back in)
without having to manually run /api/validation/data-integrity.

The worker is intentionally quiet — it doesn't spawn alerts on every
issue, just records them. A separate UI surface (or a future
notifier worker) can subscribe to validation_findings and alert when
a check's count goes UP between runs.

Status surface mirrors place_validator / collapse_worker / etc.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.data_integrity_validator")

# Default cadence: every 4 hours. Cheap to run (~1-2s on 42 docs;
# scales roughly linearly with mention count). Slower than typical
# extraction workers because data quality changes on the timescale of
# ingest rate, not seconds.
CHECK_INTERVAL_SECONDS = 4 * 60 * 60

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS = {
    "running":         False,
    "phase":           "idle",
    "last_run_at":     None,
    "last_run_id":     None,
    "checks_run":      0,
    "issues_seen":     0,
    "errors":          0,
}


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_worker_loop,
        name="data-integrity-validator",
        daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    log.info("Data-integrity validator worker started (every %ds)", CHECK_INTERVAL_SECONDS)
    return {"status": "started"}


def stop_worker():
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=10)
    _STATUS["running"] = False
    return {"status": "stopped"}


def _worker_loop():
    _STATUS["running"] = True
    # First run almost immediately so a freshly-started worker captures
    # a baseline. After that we honour the interval.
    initial_wait = 30
    for _ in range(initial_wait):
        if _STOP_EVENT.is_set():
            _STATUS["running"] = False
            return
        time.sleep(1)

    while not _STOP_EVENT.is_set():
        try:
            _STATUS["phase"] = "running checks"
            run_once(triggered_by="scheduled")
            _STATUS["phase"] = "idle"
        except Exception as e:
            log.warning("data-integrity validator error: %s", e)
            _STATUS["errors"] += 1
            _STATUS["phase"] = f"error: {str(e)[:60]}"
        # Sleep in 1-second slices so a stop_worker() returns within a sec
        slept = 0
        while slept < CHECK_INTERVAL_SECONDS and not _STOP_EVENT.is_set():
            time.sleep(1)
            slept += 1

    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"


def run_once(triggered_by: str = "manual") -> dict:
    """Run the validation suite once, persist results, update status.

    Returns the same payload `run_all()` does (for callers that want
    the immediate snapshot), plus the new run_id.
    """
    from core.validation.data_integrity import run_all

    # Start the run row
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO validation_runs (triggered_by) VALUES (%s) RETURNING run_id",
                (triggered_by,),
            )
            run_id = cur.fetchone()[0]
        conn.commit()

    report = run_all()
    findings = report.get("findings", [])
    summary = report.get("summary", {})

    # Persist findings
    with get_connection() as conn:
        with conn.cursor() as cur:
            for f in findings:
                # Cap sample size to 10 rows of ~1KB each — caps storage at
                # roughly 10KB per finding, ~120KB per run across 11 checks.
                sample = (f.get("sample") or [])[:10]
                cur.execute(
                    """
                    INSERT INTO validation_findings
                        (run_id, check_name, severity, count, sample,
                         description, fix_hint, query_ms)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                    """,
                    (
                        run_id,
                        f.get("name"),
                        f.get("severity"),
                        f.get("count"),
                        json.dumps(sample, default=str),
                        f.get("description"),
                        f.get("fix_hint"),
                        f.get("query_ms"),
                    ),
                )
            cur.execute(
                """
                UPDATE validation_runs
                   SET run_finished_at = NOW(),
                       total_checks    = %s,
                       with_issues     = %s,
                       by_severity     = %s::jsonb
                 WHERE run_id = %s
                """,
                (
                    summary.get("total_checks"),
                    summary.get("checks_with_issues"),
                    json.dumps(summary.get("by_severity") or {}),
                    run_id,
                ),
            )
        conn.commit()

    # Update status
    _STATUS["last_run_at"] = time.time()
    _STATUS["last_run_id"] = run_id
    _STATUS["checks_run"]  = summary.get("total_checks", 0)
    _STATUS["issues_seen"] = summary.get("checks_with_issues", 0)

    log.info(
        "validation run %d (%s): %d checks, %d with issues",
        run_id, triggered_by,
        summary.get("total_checks", 0),
        summary.get("checks_with_issues", 0),
    )
    return {**report, "run_id": run_id}


def latest_run() -> Optional[dict]:
    """Return the most recent recorded run + its findings."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT run_id, run_started_at, run_finished_at, triggered_by,
                       total_checks, with_issues, by_severity
                  FROM validation_runs
                 WHERE run_finished_at IS NOT NULL
                 ORDER BY run_started_at DESC LIMIT 1
            """)
            r = cur.fetchone()
            if not r:
                return None
            run_id, started, finished, trig, tot, wi, sev = r
            cur.execute("""
                SELECT check_name, severity, count, sample, description, fix_hint, query_ms
                  FROM validation_findings WHERE run_id = %s ORDER BY check_name
            """, (run_id,))
            findings = [
                {
                    "name": cn, "severity": sv, "count": cnt,
                    "sample": sp, "description": desc, "fix_hint": fh,
                    "query_ms": ms,
                }
                for cn, sv, cnt, sp, desc, fh, ms in cur.fetchall()
            ]
    return {
        "run_id":           run_id,
        "run_started_at":   started.isoformat() if started else None,
        "run_finished_at":  finished.isoformat() if finished else None,
        "triggered_by":     trig,
        "summary":          {"total_checks": tot, "checks_with_issues": wi, "by_severity": sev},
        "findings":         findings,
    }


def regression_check() -> dict:
    """Compare the latest run's per-check counts against the run before.
    Returns the list of checks whose count went UP — the things to look
    at first."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT run_id FROM validation_runs
                 WHERE run_finished_at IS NOT NULL
                 ORDER BY run_started_at DESC LIMIT 2
            """)
            rows = cur.fetchall()
            if len(rows) < 2:
                return {"runs_compared": len(rows), "regressions": []}
            latest_id, prev_id = rows[0][0], rows[1][0]
            cur.execute("""
                SELECT cur.check_name, prev.count AS prev_count, cur.count AS cur_count,
                       (cur.count - prev.count) AS delta
                  FROM validation_findings cur
                  JOIN validation_findings prev
                    ON cur.check_name = prev.check_name
                 WHERE cur.run_id = %s AND prev.run_id = %s
                   AND cur.count > prev.count
                 ORDER BY (cur.count - prev.count) DESC
            """, (latest_id, prev_id))
            regs = [
                {"check": cn, "prev_count": pc, "cur_count": cc, "delta": d}
                for cn, pc, cc, d in cur.fetchall()
            ]
    return {
        "latest_run_id":   latest_id,
        "previous_run_id": prev_id,
        "regressions":     regs,
    }
