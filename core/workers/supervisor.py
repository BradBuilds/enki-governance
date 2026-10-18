"""
Worker Supervisor — automatic restart for daemon workers.

Monitors all daemon worker threads every 10 seconds. If a thread has
died (is_alive() == False) and it was supposed to be running, the
supervisor restarts it automatically. Logs every restart.

This solves the gap where daemon workers (feed_enrichment, node_builder,
reconciliation, etc.) would die on unhandled exceptions and stay dead
until the next API restart.

Usage:
    from core.workers.supervisor import start_supervisor, get_supervisor_status
    start_supervisor("default")
"""

from __future__ import annotations

import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

_log = logging.getLogger("enki.workers.supervisor")

_SUPERVISOR_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()

CHECK_INTERVAL = 10  # seconds between health checks
MAX_RESTART_BACKOFF = 300  # max seconds between restart attempts for same worker

# Workers whose tick() will hang or hard-fail when Ollama is unreachable.
# When the LLM is down, the supervisor pauses restart attempts on these so we
# don't burn CPU restarting threads that will immediately die again, and
# force-restarts them once Ollama recovers.
_LLM_DEPENDENT_WORKERS = frozenset({
    "feed_enrichment",
    "node_builder",
    "llm_extraction",
    "llm_entity_validator",
    "llm_entity_confirmer",
    "image_analyst",
    "security_analyst",
    "auto_validator",
    # News-pipeline workers that call Gemma per-tick
    "collapse_worker",
    "signal_statement_extractor",
    "predicate_extender",
})

_OLLAMA_HEALTH = {
    "healthy": True,           # optimistic on startup; first probe will correct
    "last_check": None,
    "consecutive_failures": 0,
    "last_error": None,
}

# ── Worker registry ─────────────────────────────────────────────────────

@dataclass
class WorkerEntry:
    """A registered daemon worker that the supervisor monitors."""
    name: str
    start_fn: Callable[[str], dict]   # start_worker(tenant_id) -> status dict
    status_fn: Callable[[], dict]     # get_status() -> status dict
    stop_fn: Callable[[], dict]       # stop_worker() -> status dict
    tenant_id: str = "default"
    restart_count: int = 0
    last_restart: Optional[datetime] = None
    consecutive_failures: int = 0

_WORKERS: dict[str, WorkerEntry] = {}

_STATUS = {
    "running": False,
    "checks": 0,
    "restarts": 0,
    "last_check": None,
    "workers_monitored": 0,
    "worker_states": {},
}


def register_worker(
    name: str,
    start_fn: Callable,
    status_fn: Callable,
    stop_fn: Callable,
    tenant_id: str = "default",
):
    """Register a daemon worker for supervision."""
    _WORKERS[name] = WorkerEntry(
        name=name,
        start_fn=start_fn,
        status_fn=status_fn,
        stop_fn=stop_fn,
        tenant_id=tenant_id,
    )
    _log.info("Supervisor: registered worker '%s'", name)


def start_supervisor(tenant_id: str = "default"):
    """Start the supervisor thread."""
    global _SUPERVISOR_THREAD
    if _SUPERVISOR_THREAD and _SUPERVISOR_THREAD.is_alive():
        return {"status": "already_running"}

    _STOP_EVENT.clear()
    _SUPERVISOR_THREAD = threading.Thread(
        target=_supervisor_loop,
        args=(tenant_id,),
        name="worker-supervisor",
        daemon=True,
    )
    _SUPERVISOR_THREAD.start()
    _STATUS["running"] = True
    _log.info("Worker supervisor started — monitoring %d workers", len(_WORKERS))
    return {"status": "started", "workers": list(_WORKERS.keys())}


def stop_supervisor():
    """Stop the supervisor."""
    _STOP_EVENT.set()
    if _SUPERVISOR_THREAD:
        _SUPERVISOR_THREAD.join(timeout=5)
    _STATUS["running"] = False
    return {"status": "stopped"}


def get_supervisor_status() -> dict:
    """Current supervisor state + per-worker health."""
    status = dict(_STATUS)
    status["workers_monitored"] = len(_WORKERS)

    worker_states = {}
    for name, entry in _WORKERS.items():
        try:
            ws = entry.status_fn()
            worker_states[name] = {
                "running": ws.get("running", False),
                "phase": ws.get("phase", "unknown"),
                "restart_count": entry.restart_count,
                "consecutive_failures": entry.consecutive_failures,
                "last_restart": entry.last_restart.isoformat() if entry.last_restart else None,
            }
        except Exception as e:
            worker_states[name] = {
                "running": False,
                "phase": f"status_error: {e}",
                "restart_count": entry.restart_count,
                "consecutive_failures": entry.consecutive_failures,
            }

    status["worker_states"] = worker_states
    status["ollama"] = dict(_OLLAMA_HEALTH)
    return status


def _supervisor_loop(tenant_id: str):
    """Main loop — check Ollama, then check all workers, restart any that died."""
    while not _STOP_EVENT.is_set():
        try:
            _probe_ollama_health()
            _check_all_workers()
            _STATUS["checks"] += 1
            _STATUS["last_check"] = datetime.now(timezone.utc).isoformat()
        except Exception as e:
            _log.warning("Supervisor check error: %s", e)

        # Interruptible sleep
        for _ in range(CHECK_INTERVAL):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)

    _STATUS["running"] = False


def _probe_ollama_health() -> None:
    """Quick liveness check against the configured Ollama endpoint.

    Updates _OLLAMA_HEALTH in place. Recovery transition (unhealthy → healthy)
    triggers a force-restart sweep of LLM-dependent workers in
    _check_all_workers, so they pick up the now-available LLM.
    """
    base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    url = f"{base}/api/tags"
    was_healthy = _OLLAMA_HEALTH["healthy"]
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            ok = resp.status == 200
        if ok:
            if not was_healthy:
                _log.info(
                    "Supervisor: Ollama RECOVERED after %d failure(s)",
                    _OLLAMA_HEALTH["consecutive_failures"],
                )
            _OLLAMA_HEALTH["healthy"] = True
            _OLLAMA_HEALTH["consecutive_failures"] = 0
            _OLLAMA_HEALTH["last_error"] = None
        else:
            raise RuntimeError(f"HTTP {resp.status}")
    except (urllib.error.URLError, RuntimeError, OSError) as e:
        _OLLAMA_HEALTH["healthy"] = False
        _OLLAMA_HEALTH["consecutive_failures"] += 1
        _OLLAMA_HEALTH["last_error"] = str(e)
        if was_healthy:
            _log.warning("Supervisor: Ollama UNREACHABLE (%s)", e)
    finally:
        _OLLAMA_HEALTH["last_check"] = datetime.now(timezone.utc).isoformat()


def _check_all_workers():
    """Check each registered worker and restart if dead.

    Two-phase: when Ollama just recovered, force-restart any LLM-dependent
    worker that isn't running (it likely died during the outage and the
    backoff would otherwise delay recovery). When Ollama is unhealthy,
    skip restarts on LLM-dependent workers — they'd just die again.
    """
    ollama_healthy = _OLLAMA_HEALTH["healthy"]
    # Detect rising edge: if last_check is set and we are now healthy with
    # zero failures, treat as a recovery sweep this tick.
    just_recovered = (
        ollama_healthy
        and _OLLAMA_HEALTH["consecutive_failures"] == 0
        and _STATUS.get("ollama_was_unhealthy", False)
    )
    _STATUS["ollama_was_unhealthy"] = not ollama_healthy

    # The manager (core.workers.manager) may decide some workers should not
    # be running based on system pressure. Imported lazily to avoid a hard
    # dependency at import time.
    try:
        from core.workers.manager import is_manager_disabled
    except Exception:
        is_manager_disabled = lambda _w: False  # type: ignore

    for name, entry in _WORKERS.items():
        try:
            if is_manager_disabled(name):
                # Manager has paused this worker. Stop it if it's running and
                # do not attempt to restart.
                try:
                    s = entry.status_fn()
                    if s.get("running", False):
                        entry.stop_fn()
                except Exception:
                    pass
                continue

            status = entry.status_fn()
            is_running = status.get("running", False)
            phase = status.get("phase", "")
            is_dead = (not is_running) or phase == "stopped"

            llm_dep = name in _LLM_DEPENDENT_WORKERS

            if llm_dep and not ollama_healthy:
                # Don't thrash restarting workers whose only job is to call a
                # dead LLM. They'll come back on the recovery edge below.
                continue

            if is_dead:
                _restart_worker(entry)
            elif just_recovered and llm_dep:
                # Healthy worker on recovery edge — leave it alone.
                pass
        except Exception as e:
            _log.debug("Supervisor: failed to check %s: %s", name, e)
            # Skip the blind restart for LLM-dependent workers when Ollama is
            # down — they'd just die again. Otherwise try to recover.
            if not (name in _LLM_DEPENDENT_WORKERS and not ollama_healthy):
                _restart_worker(entry)


def _restart_worker(entry: WorkerEntry):
    """Restart a dead worker with exponential backoff."""
    now = datetime.now(timezone.utc)

    # Backoff: don't restart too rapidly if it keeps dying
    if entry.last_restart:
        elapsed = (now - entry.last_restart).total_seconds()
        backoff = min(10 * (2 ** entry.consecutive_failures), MAX_RESTART_BACKOFF)
        if elapsed < backoff:
            return  # Too soon, wait

    try:
        _log.info(
            "Supervisor: restarting '%s' (restart #%d, consecutive failures: %d)",
            entry.name, entry.restart_count + 1, entry.consecutive_failures,
        )

        # Stop first (clean slate)
        try:
            entry.stop_fn()
        except Exception:
            pass

        # Start
        result = entry.start_fn(entry.tenant_id)
        entry.restart_count += 1
        entry.last_restart = now
        _STATUS["restarts"] += 1

        # Check if start actually worked
        time.sleep(1)
        post_status = entry.status_fn()
        if post_status.get("running", False):
            entry.consecutive_failures = 0
            _log.info("Supervisor: '%s' restarted successfully", entry.name)
        else:
            entry.consecutive_failures += 1
            _log.warning(
                "Supervisor: '%s' restart failed (attempt %d)",
                entry.name, entry.consecutive_failures,
            )

    except Exception as e:
        entry.consecutive_failures += 1
        entry.last_restart = now
        _log.warning("Supervisor: '%s' restart error: %s", entry.name, e)
