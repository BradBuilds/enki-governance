"""
Reference data sync scheduler.

Runs as a background thread. Checks every hour which datasets are
past their sync interval and runs their sync functions automatically.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone

from storage.postgres.db import get_connection

logger = logging.getLogger("enki.reference.scheduler")

_scheduler_thread: threading.Thread | None = None
_stop_event = threading.Event()
CHECK_INTERVAL = 3600  # Check every hour


def start_reference_scheduler():
    """Start the background sync scheduler."""
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return

    _stop_event.clear()
    _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True, name="reference-sync")
    _scheduler_thread.start()
    logger.info("Reference data sync scheduler started (checks every %ds)", CHECK_INTERVAL)


def stop_reference_scheduler():
    """Stop the background sync scheduler."""
    _stop_event.set()
    if _scheduler_thread:
        _scheduler_thread.join(timeout=5)
    logger.info("Reference data sync scheduler stopped")


def _scheduler_loop():
    """Main scheduler loop — runs in background thread."""
    # Wait 60s before first check (let API finish starting)
    time.sleep(60)

    while not _stop_event.is_set():
        try:
            _check_and_sync()
        except Exception as e:
            logger.error(f"Scheduler error: {e}")

        # Sleep in small increments so we can stop quickly
        for _ in range(CHECK_INTERVAL):
            if _stop_event.is_set():
                return
            time.sleep(1)


def _check_and_sync():
    """Check which datasets need syncing and run them."""
    now = datetime.now(timezone.utc)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT dataset, display_name, next_due
                FROM reference_sync_status
                WHERE next_due IS NOT NULL AND next_due <= %s
                   OR (next_due IS NULL AND last_synced IS NULL)
                ORDER BY next_due ASC NULLS FIRST
            """, (now,))
            due = cur.fetchall()

    if not due:
        return

    logger.info(f"Reference sync: {len(due)} dataset(s) due for sync")

    for dataset, display_name, next_due in due:
        logger.info(f"Syncing {display_name} (was due: {next_due})")
        try:
            # Run the sync in an asyncio event loop
            loop = asyncio.new_event_loop()
            try:
                if dataset == "cameras":
                    from services.api.src.routes.cameras import sync_cameras
                    result = loop.run_until_complete(sync_cameras())
                elif dataset == "ports":
                    from core.reference.sync import sync_ports
                    result = loop.run_until_complete(sync_ports())
                elif dataset == "airports":
                    from core.reference.sync import sync_airports
                    result = loop.run_until_complete(sync_airports())
                elif dataset == "aircraft_registry":
                    from core.reference.sync import sync_aircraft_registry
                    result = loop.run_until_complete(sync_aircraft_registry())
                else:
                    logger.warning(f"Unknown dataset: {dataset}")
                    continue
            finally:
                loop.close()

            logger.info(f"Synced {display_name}: {result}")
        except Exception as e:
            logger.error(f"Failed to sync {display_name}: {e}")
            # Update status with error
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE reference_sync_status SET
                            last_error = %s, status = 'error',
                            next_due = NOW() + (sync_interval_hours * interval '1 hour')
                        WHERE dataset = %s
                    """, (str(e), dataset))
                conn.commit()
