"""
Personal Workers Supervisor — manages all 4 personal signal extraction workers.

Provides:
  - start_all_personal_workers(tenant_id) — starts location, contacts, comms, calendar
  - stop_all_personal_workers() — stops all 4
  - get_personal_workers_status() — unified status across all workers

Called from the API via /workers/personal/start-all, stop-all, status.
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("enki.workers.personal_supervisor")

_TENANT_ID: Optional[str] = None


def start_all_personal_workers(tenant_id: str = "default") -> dict:
    """Start all 4 personal signal extraction workers."""
    global _TENANT_ID
    _TENANT_ID = tenant_id

    results = {}

    # Location worker
    try:
        from core.workers.personal_location import start_worker as start_location
        results["location"] = start_location(tenant_id)
    except Exception as e:
        results["location"] = {"status": "error", "error": str(e)}
        log.warning("Failed to start location worker: %s", e)

    # Contacts worker
    try:
        from core.workers.personal_contacts import start_worker as start_contacts
        results["contacts"] = start_contacts(tenant_id)
    except Exception as e:
        results["contacts"] = {"status": "error", "error": str(e)}
        log.warning("Failed to start contacts worker: %s", e)

    # Communications worker
    try:
        from core.workers.personal_comms import start_worker as start_comms
        results["comms"] = start_comms(tenant_id)
    except Exception as e:
        results["comms"] = {"status": "error", "error": str(e)}
        log.warning("Failed to start comms worker: %s", e)

    # Calendar worker
    try:
        from core.workers.personal_calendar import start_worker as start_calendar
        results["calendar"] = start_calendar(tenant_id)
    except Exception as e:
        results["calendar"] = {"status": "error", "error": str(e)}
        log.warning("Failed to start calendar worker: %s", e)

    started = [k for k, v in results.items() if v.get("status") in ("started", "already_running")]
    log.info("Personal workers started: %s", ", ".join(started))

    return {
        "status": "started",
        "workers": results,
        "started_count": len(started),
        "total": 4,
    }


def stop_all_personal_workers() -> dict:
    """Stop all 4 personal signal extraction workers."""

    results = {}

    try:
        from core.workers.personal_location import stop_worker as stop_location
        results["location"] = stop_location()
    except Exception as e:
        results["location"] = {"status": "error", "error": str(e)}

    try:
        from core.workers.personal_contacts import stop_worker as stop_contacts
        results["contacts"] = stop_contacts()
    except Exception as e:
        results["contacts"] = {"status": "error", "error": str(e)}

    try:
        from core.workers.personal_comms import stop_worker as stop_comms
        results["comms"] = stop_comms()
    except Exception as e:
        results["comms"] = {"status": "error", "error": str(e)}

    try:
        from core.workers.personal_calendar import stop_worker as stop_calendar
        results["calendar"] = stop_calendar()
    except Exception as e:
        results["calendar"] = {"status": "error", "error": str(e)}

    log.info("All personal workers stopped")

    return {
        "status": "stopped",
        "workers": results,
    }


def get_personal_workers_status() -> dict:
    """Get unified status across all personal workers."""

    status = {
        "location": {"running": False, "phase": "not started"},
        "contacts": {"running": False, "phase": "not started"},
        "comms": {"running": False, "phase": "not started"},
        "calendar": {"running": False, "phase": "not started"},
    }

    try:
        from core.workers.personal_location import get_status as loc_status
        status["location"] = loc_status()
    except Exception:
        pass

    try:
        from core.workers.personal_contacts import get_status as con_status
        status["contacts"] = con_status()
    except Exception:
        pass

    try:
        from core.workers.personal_comms import get_status as com_status
        status["comms"] = com_status()
    except Exception:
        pass

    try:
        from core.workers.personal_calendar import get_status as cal_status
        status["calendar"] = cal_status()
    except Exception:
        pass

    # Aggregate stats
    total_processed = sum(
        s.get("signals_processed", 0) for s in status.values()
    )
    total_errors = sum(
        s.get("errors", 0) for s in status.values()
    )
    any_running = any(s.get("running", False) for s in status.values())

    status["_aggregate"] = {
        "any_running": any_running,
        "total_signals_processed": total_processed,
        "total_errors": total_errors,
        "workers_running": sum(1 for s in status.values() if isinstance(s, dict) and s.get("running")),
        "workers_total": 4,
    }

    return status
