"""
Per-source rate limiter for discovery clients.

Principle: when a source is rate limited, ask slowly rather than rush.
Every external HTTP source we query gets a
named limiter with a minimum interval between calls. Clients call
`get_limiter(name).wait()` before each request.

Process-local + thread-safe (multiple workers in the same container
respect each other; not currently cross-process — that would need a
Redis token bucket but we don't need it yet).

The defaults are conservative on purpose. We'd rather discover slowly
and build a real corpus than burst, get banned, and have to start over.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

log = logging.getLogger("enki.discovery.rate_limiter")


# Conservative per-source minimum-interval defaults (seconds between
# calls). Tuned to stay well under each source's public quota even at
# sustained load.
_DEFAULTS = {
    "google_books":      3.0,    # free quota ~1k/day; 3s = 1200/hr ceiling
    "archive_org":       1.5,    # they're forgiving but politeness matters
    "openlibrary":       2.0,    # generous; 2s keeps us out of any rate response
    "wikipedia":         0.5,    # very generous; 0.5s is fine
    "cia_reading_room":  5.0,    # cranky old server; be very slow
    "fbi_vault":         5.0,    # same
    "gpo":               3.0,    # Government Publishing Office catalog
    "pubmed":            1.0,    # NCBI publishes 3/sec quota → we use 1/sec
    "europepmc":         1.0,
    "gutenberg":         2.0,
    "default":           2.0,
}


class _SourceLimiter:
    def __init__(self, name: str, min_interval_s: float):
        self.name = name
        self.min_interval_s = float(min_interval_s)
        self._last_call_ts = 0.0
        self._lock = threading.Lock()
        self._total_waits = 0
        self._total_calls = 0

    def wait(self) -> float:
        """Block until at least min_interval_s has elapsed since the last
        call to this limiter. Returns the slept duration."""
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call_ts
            slept = 0.0
            if elapsed < self.min_interval_s:
                slept = self.min_interval_s - elapsed
                time.sleep(slept)
                self._total_waits += 1
            self._last_call_ts = time.monotonic()
            self._total_calls += 1
        return slept

    def update_interval(self, new_interval_s: float) -> None:
        with self._lock:
            self.min_interval_s = float(new_interval_s)
        log.info("rate_limiter[%s] interval -> %.2fs", self.name, new_interval_s)

    def stats(self) -> dict:
        return {
            "name": self.name,
            "interval_s": self.min_interval_s,
            "total_calls": self._total_calls,
            "total_waits": self._total_waits,
        }


_LIMITERS: dict[str, _SourceLimiter] = {}
_REGISTRY_LOCK = threading.Lock()


def get_limiter(name: str) -> _SourceLimiter:
    """Get-or-create the limiter for a source name. Thread-safe."""
    with _REGISTRY_LOCK:
        if name not in _LIMITERS:
            interval = _DEFAULTS.get(name, _DEFAULTS["default"])
            _LIMITERS[name] = _SourceLimiter(name, interval)
        return _LIMITERS[name]


def back_off(name: str, multiplier: float = 2.0,
             max_interval_s: float = 60.0) -> None:
    """Increase the interval for a source after a rate-limit response
    (HTTP 429 or similar). Caller decides when to invoke this."""
    lim = get_limiter(name)
    new_interval = min(lim.min_interval_s * multiplier, max_interval_s)
    lim.update_interval(new_interval)


def reset_all() -> None:
    """Test hook: clear all limiters."""
    with _REGISTRY_LOCK:
        _LIMITERS.clear()


def all_stats() -> list[dict]:
    with _REGISTRY_LOCK:
        return [l.stats() for l in _LIMITERS.values()]
