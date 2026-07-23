"""
Connector Scheduler — auto-polls connectors on their configured interval.

Runs as a single background thread alongside the worker runner.
Every 30 seconds, checks all active connectors. If a connector's
last_fetch_at + poll_interval < now, triggers a fetch.

Usage:
    from core.connectors.scheduler import start_connector_scheduler, stop_connector_scheduler

    start_connector_scheduler("default")  # call from API lifespan
    stop_connector_scheduler()             # call on shutdown
"""

from __future__ import annotations

import time
import threading
import logging
from typing import Optional
from datetime import datetime, timezone

log = logging.getLogger("enki.connectors.scheduler")

_scheduler_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()

CHECK_INTERVAL = 30  # seconds between scheduler checks


def _scheduler_loop(tenant_id: str, dsn: Optional[str] = None):
    """Main scheduler loop — runs in background thread."""
    log.info("[SCHEDULER] Connector scheduler started")

    while not _stop_event.is_set():
        try:
            _check_and_trigger(tenant_id, dsn)
        except Exception as e:
            log.error(f"[SCHEDULER] Error in check cycle: {e}")

        _stop_event.wait(timeout=CHECK_INTERVAL)

    log.info("[SCHEDULER] Connector scheduler stopped")


def _check_and_trigger(tenant_id: str, dsn: Optional[str] = None):
    """Check all active connectors and trigger those that are due."""
    from storage.connectors.manager import list_connectors, trigger_connector

    try:
        # Bumped from 100 to 1000 (Jun 9 2026) — at 100, tenants with more
        # than 100 active connectors had the older ones silently never
        # polled. list_connectors clamps to 1000 internally.
        connectors = list_connectors(tenant_id, limit=1000, dsn=dsn)
    except Exception as e:
        log.warning(f"[SCHEDULER] Failed to list connectors: {e}")
        return

    now = datetime.now(timezone.utc)
    triggered = 0

    for conn in connectors:
        if conn.get("status") != "active":
            continue

        poll_interval = conn.get("poll_interval")
        if not poll_interval:
            continue

        last_fetch = conn.get("last_fetch_at")
        if last_fetch:
            if isinstance(last_fetch, str):
                try:
                    last_fetch = datetime.fromisoformat(last_fetch.replace("Z", "+00:00"))
                except ValueError:
                    last_fetch = None

        # If never fetched, or past due
        if last_fetch is None:
            is_due = True
        else:
            # Parse interval string like "5 minutes", "1 hour", "1 day"
            interval_seconds = _parse_interval(poll_interval)
            if interval_seconds is None:
                continue

            elapsed = (now - last_fetch).total_seconds()
            is_due = elapsed >= interval_seconds

        if is_due:
            try:
                log.info(f"[SCHEDULER] Triggering connector {conn['connector_id']} ({conn['source_type']})")
                trigger_connector(conn["connector_id"], tenant_id, dsn)
                triggered += 1
            except Exception as e:
                log.warning(f"[SCHEDULER] Failed to trigger {conn['connector_id']}: {e}")

    if triggered > 0:
        log.info(f"[SCHEDULER] Triggered {triggered} connector(s)")


def _parse_interval(interval) -> Optional[float]:
    """Parse PostgreSQL interval string to seconds.
    Handles: '5 minutes', '1 hour', '6 hours', '1 day', '30 seconds', etc.
    Also handles timedelta and direct numeric seconds.
    """
    if isinstance(interval, (int, float)):
        return float(interval)

    if hasattr(interval, 'total_seconds'):
        return interval.total_seconds()

    if not isinstance(interval, str):
        return None

    interval = interval.strip().lower()

    # Simple parsing for common patterns
    parts = interval.split()
    if len(parts) == 2:
        try:
            value = float(parts[0])
        except ValueError:
            return None

        unit = parts[1].rstrip('s')  # "minutes" → "minute"
        multipliers = {
            "second": 1,
            "minute": 60,
            "hour": 3600,
            "day": 86400,
            "week": 604800,
        }
        return value * multipliers.get(unit, 60)

    # Try as raw seconds
    try:
        return float(interval)
    except ValueError:
        return None


def start_connector_scheduler(tenant_id: str = "default", dsn: Optional[str] = None):
    """Start the connector scheduler in a background thread."""
    global _scheduler_thread

    if _scheduler_thread and _scheduler_thread.is_alive():
        return  # already running

    _stop_event.clear()
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop,
        args=(tenant_id, dsn),
        daemon=True,
        name="enki-connector-scheduler",
    )
    _scheduler_thread.start()


def stop_connector_scheduler():
    """Stop the connector scheduler."""
    global _scheduler_thread

    _stop_event.set()
    if _scheduler_thread:
        _scheduler_thread.join(timeout=5)
        _scheduler_thread = None
