"""
Governor — the compute scheduler for the self-learning substrate.

Workers register with a priority tier and a minimum interval between runs.
The governor loops, picks the workers whose interval has elapsed and whose
priority isn't throttled by foreground pressure, runs them one at a time,
and records what they did to the learning_log table.

Foreground pressure comes from the API layer: any user-facing request
(chat, research, map click, card open) upserts `learning_pulse` so the
governor can see when the user is actively working. Pressure levels:

    level 0 — idle (>60s since last user action): run everything
    level 1 — recent activity (10-60s): pause BACKFILL + REPLAY
    level 2 — active (<10s): pause everything except WATCHLIST

Workers are expected to be idempotent and to bound their own work per
invocation (typically "process N items, return stats, come back later").
The governor never preempts mid-worker; preemption happens between jobs.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.learning.governor")


class Priority:
    """Priority tiers, lower number = higher priority."""
    FOREGROUND = 0   # reserved — not used by background workers
    WATCHLIST = 1    # touches a tracked watchlist entity / active case
    RECENT = 2       # items younger than 24h
    FRESH = 3        # items younger than 7d
    REVIEW = 4       # aged review queue items ready for auto-resolution
    BACKFILL = 5     # historical sweep
    REPLAY = 6       # re-extraction after pack version bump


@dataclass
class Worker:
    name: str
    priority: int
    interval_seconds: float
    fn: Callable      # fn(conn) -> dict[str, int|str] of stats


class Governor:
    """Process-wide singleton. Call start() once at service boot; stop()
    at shutdown. Use register() before start() to add workers."""

    def __init__(self, tick_seconds: float = 5.0):
        self._workers: list[Worker] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._tick = tick_seconds
        self._last_run: dict[str, float] = {}

    def register(
        self,
        name: str,
        priority: int,
        interval_seconds: float,
        fn: Callable,
    ) -> None:
        """Add a worker. fn receives a live DB connection and should return
        a small dict of stats (items_processed, etc.) that goes to learning_log."""
        if any(w.name == name for w in self._workers):
            raise ValueError(f"worker {name!r} already registered")
        self._workers.append(Worker(name, priority, interval_seconds, fn))
        log.info("governor: registered worker %s (priority=%d, interval=%.0fs)",
                 name, priority, interval_seconds)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="enki-governor", daemon=True
        )
        self._thread.start()
        log.info("governor: started with %d worker(s)", len(self._workers))

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        log.info("governor: stopped")

    # ── pressure detection ─────────────────────────────────────────────

    def pressure_level(self) -> int:
        """Read the latest foreground pulse from the DB. Returns 0/1/2."""
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT EXTRACT(EPOCH FROM (NOW() - last_activity_at)) "
                        "FROM learning_pulse WHERE pulse_id = 1"
                    )
                    row = cur.fetchone()
        except Exception:
            return 0
        if not row or row[0] is None:
            return 0
        delta = float(row[0])
        if delta < 10:
            return 2
        if delta < 60:
            return 1
        return 0

    @staticmethod
    def notice_foreground(conn=None) -> None:
        """Call from API middleware on every user-facing request. Cheap
        UPSERT of a single row; can be called hundreds of times per minute
        without noticeable DB load."""
        sql = """
            INSERT INTO learning_pulse (pulse_id, last_activity_at)
            VALUES (1, NOW())
            ON CONFLICT (pulse_id) DO UPDATE SET last_activity_at = NOW()
        """
        try:
            if conn is not None:
                with conn.cursor() as cur:
                    cur.execute(sql)
                conn.commit()
                return
            with get_connection() as c:
                with c.cursor() as cur:
                    cur.execute(sql)
                c.commit()
        except Exception as e:
            log.debug("notice_foreground failed: %s", e)

    def _max_priority_allowed(self) -> int:
        # Validation (REVIEW) and clustering (RECENT) are both user-visible
        # work — pausing them under UI activity makes the page look frozen
        # exactly when the user is watching it grow. Only the heavy sweeps
        # (historical BACKFILL, full REPLAY) should back off under pressure.
        #
        #   pressure 0 (idle)     → everything
        #   pressure 1 (recent)   → pauses REPLAY only
        #   pressure 2 (active)   → pauses BACKFILL + REPLAY
        # REVIEW and tighter always run regardless of pressure.
        p = self.pressure_level()
        if p >= 2:
            return Priority.REVIEW
        if p == 1:
            return Priority.BACKFILL
        return Priority.REPLAY

    # ── main loop ──────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick_once()
            except Exception as e:
                log.exception("governor tick failed: %s", e)
            self._stop.wait(self._tick)

    def _tick_once(self) -> None:
        now = time.time()
        max_allowed = self._max_priority_allowed()
        # Priority order: lower int runs first; ties broken by least-recent run
        queue = sorted(
            self._workers,
            key=lambda w: (w.priority, self._last_run.get(w.name, 0.0)),
        )
        for w in queue:
            if self._stop.is_set():
                return
            if w.priority > max_allowed:
                continue
            if now - self._last_run.get(w.name, 0.0) < w.interval_seconds:
                continue
            self._run_worker(w)

    def _run_worker(self, w: Worker) -> None:
        started = time.time()
        stats: dict = {}
        error: Optional[str] = None
        try:
            with get_connection() as conn:
                stats = w.fn(conn) or {}
        except Exception as e:
            error = str(e)[:500]
            log.exception("worker %s raised: %s", w.name, e)
        elapsed_ms = int((time.time() - started) * 1000)
        self._last_run[w.name] = time.time()
        self._record_log(w, stats, error, elapsed_ms)

    def _record_log(
        self,
        w: Worker,
        stats: dict,
        error: Optional[str],
        elapsed_ms: int,
    ) -> None:
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO learning_log
                            (worker, priority, stats, error, elapsed_ms, pressure_level)
                        VALUES
                            (%s, %s, %s::jsonb, %s, %s, %s)
                        """,
                        (
                            w.name,
                            w.priority,
                            json.dumps(stats, default=str),
                            error,
                            elapsed_ms,
                            self.pressure_level(),
                        ),
                    )
                conn.commit()
        except Exception as e:
            log.debug("governor log insert failed: %s", e)


# Process singleton ─ lazy-constructed
_singleton: Optional[Governor] = None
_singleton_lock = threading.Lock()


def get_governor() -> Governor:
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = Governor()
        return _singleton
