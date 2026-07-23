"""
Worker Runner — background loop that continuously runs active workers.

Starts a thread per worker that loops: claim chunks → process → sleep → repeat.
Workers only run when status is "on". Changing status to "off" or "paused"
stops the loop gracefully.

Usage:
    from core.workers.runner import WorkerRunner

    runner = WorkerRunner(tenant_id="default")
    runner.start()   # starts all "on" workers in background threads
    runner.stop()    # stops all workers gracefully

Integrated into the API via lifespan events — workers start when the
API starts and stop when it shuts down.
"""

from __future__ import annotations

import time
import threading
import logging
from typing import Optional

from storage.signals.workers import (
    list_workers, get_worker, run_worker_tick,
    ensure_default_workers, record_tick,
)

log = logging.getLogger("enki.workers.runner")


class WorkerRunner:
    """Manages background threads for all workers."""

    def __init__(self, tenant_id: str = "default", dsn: Optional[str] = None):
        self.tenant_id = tenant_id
        self.dsn = dsn
        self._threads: dict[str, threading.Thread] = {}
        self._stop_events: dict[str, threading.Event] = {}
        self._running = False

    def start(self) -> None:
        """Start the runner. Launches threads for all 'on' workers."""
        if self._running:
            return

        self._running = True
        ensure_default_workers(self.tenant_id, self.dsn)

        # Start monitor thread that checks for worker status changes
        monitor = threading.Thread(
            target=self._monitor_loop,
            name="worker-monitor",
            daemon=True,
        )
        monitor.start()
        log.info("[WORKERS] Runner started. Monitor thread active.")

    def stop(self) -> None:
        """Stop all worker threads gracefully."""
        self._running = False
        for worker_id, event in self._stop_events.items():
            event.set()
        # Wait for threads to finish (max 5s each)
        for worker_id, thread in list(self._threads.items()):
            thread.join(timeout=5)
        self._threads.clear()
        self._stop_events.clear()
        log.info("[WORKERS] Runner stopped.")

    def _monitor_loop(self) -> None:
        """Periodically check which workers should be running."""
        while self._running:
            try:
                workers = list_workers(self.tenant_id, self.dsn)
                for w in workers:
                    wid = w["worker_id"]
                    should_run = w["status"] == "on"
                    is_running = wid in self._threads and self._threads[wid].is_alive()

                    if should_run and not is_running:
                        self._start_worker(w)
                    elif not should_run and is_running:
                        self._stop_worker(wid)
            except Exception as e:
                log.warning(f"[WORKERS] Monitor error: {e}")

            time.sleep(3)  # Check every 3 seconds

    def _start_worker(self, worker: dict) -> None:
        """Start a background thread for a worker."""
        wid = worker["worker_id"]
        stop_event = threading.Event()
        self._stop_events[wid] = stop_event

        thread = threading.Thread(
            target=self._worker_loop,
            args=(wid, worker["interval_ms"] / 1000.0, stop_event),
            name=f"worker-{wid}",
            daemon=True,
        )
        self._threads[wid] = thread
        thread.start()
        log.info(f"[WORKERS] Started: {wid} (interval: {worker['interval_ms']}ms)")

    def _stop_worker(self, worker_id: str) -> None:
        """Stop a worker thread."""
        if worker_id in self._stop_events:
            self._stop_events[worker_id].set()
        if worker_id in self._threads:
            self._threads[worker_id].join(timeout=5)
            del self._threads[worker_id]
        if worker_id in self._stop_events:
            del self._stop_events[worker_id]
        log.info(f"[WORKERS] Stopped: {worker_id}")

    def _worker_loop(self, worker_id: str, interval: float, stop_event: threading.Event) -> None:
        """The actual processing loop for a single worker."""
        while not stop_event.is_set():
            try:
                # Re-check status (might have been paused/stopped)
                w = get_worker(worker_id, self.tenant_id, self.dsn)
                if not w or w["status"] != "on":
                    break

                result = run_worker_tick(worker_id, self.tenant_id, self.dsn)

                processed = result.get("processed", 0)
                if processed > 0:
                    log.debug(f"[WORKERS] {worker_id}: processed {processed}")

            except Exception as e:
                log.warning(f"[WORKERS] {worker_id} error: {e}")
                record_tick(worker_id, errors=1, last_error=str(e)[:200], dsn=self.dsn)

            # Sleep with interruptible wait
            stop_event.wait(timeout=interval)


# ─── Singleton for API integration ────────────────────────────────────────────

_runner: Optional[WorkerRunner] = None


def get_runner(tenant_id: str = "default") -> WorkerRunner:
    """Get or create the singleton worker runner."""
    global _runner
    if _runner is None:
        _runner = WorkerRunner(tenant_id=tenant_id)
    return _runner


def start_workers(tenant_id: str = "default") -> None:
    """Start the worker runner (called from API lifespan)."""
    runner = get_runner(tenant_id)
    runner.start()


def stop_workers() -> None:
    """Stop the worker runner (called from API shutdown)."""
    global _runner
    if _runner:
        _runner.stop()
        _runner = None
