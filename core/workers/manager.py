"""
Worker Manager — pressure-aware scheduler that decides WHICH workers run.

The supervisor restarts dead worker threads. The runner starts/stops worker
threads based on DB status. The MANAGER is the layer above both: it decides,
based on system pressure (Ollama health, load average, GPU readiness), which
workers should be eligible to run at all.

Workers are grouped into tiers. The manager picks a mode each tick and
enables tier <= mode_max_tier.

    mode            tier_max  meaning
    ─────────────   ────────  ─────────────────────────────────────────
    normal          3         everything runs
    degraded        2         pause heavy/optional (T3)
    loaded          1         only baseline LLM + always-on
    llm_emergency   0         no LLM workers — only always-on, non-LLM
    cold            -1        nothing runs (during boot or planned drain)

When mode improves (e.g. Ollama recovers), the manager ramps the max tier
up by one per tick rather than enabling everything at once. This gives
workers time to reconnect and the LLM time to warm without a thundering
herd of pending requests piling onto a single-slot Ollama.

Integration:
- DB-backed workers (storage.signals.workers): toggled via update_worker(
  status=...). The runner picks up the change on its 3s monitor cycle.
- Code-registered workers (core.workers.supervisor): the supervisor checks
  is_manager_disabled() before restarting a worker. Pausing a registered
  worker means: stop it now, don't restart until the manager unpauses.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from core.workers.supervisor import _OLLAMA_HEALTH

_log = logging.getLogger("enki.workers.manager")

# ── Tier assignments ────────────────────────────────────────────────────
# Tier 0: always on. No LLM dependency, low CPU, must keep running for the
#         system to stay observable (heartbeat, geocoding, hash chain).
# Tier 1: LLM-light. Single small calls, baseline enrichment.
# Tier 2: LLM-medium. Validators and confirmers — useful but skippable
#         under pressure.
# Tier 3: heavy/optional. Long investigations, reconciliation sweeps,
#         media analysis. First to be paused.

WORKER_TIERS: dict[str, int] = {
    # Tier 0 — always-on, no LLM
    "hub_heartbeat":       0,
    "geocoder":            0,
    "mentions_writer":     0,
    "chain_verifier":      0,
    "feed_snapshots":      0,
    "gdelt_gkg":           0,

    # Tier 1 — baseline LLM
    "feed_enrichment":     1,
    "node_builder":        1,
    "place_validator":     1,
    "event_validator":     1,
    # Phase 3 of the pipeline-restart plan (May 23 2026). Three workers
    # that close the boundary failures identified in the May 22 diagnosis:
    # threshold-cross investigations (not just first-admit), document
    # mention resolution outside the library workflow, and entity dedup
    # as a continuous sweep instead of a one-shot script. All three are
    # DB-only (no LLM), tiered with the other baseline workers.
    "investigation_sweep": 1,
    "mention_backfill":    1,
    "entity_dedup_sweep":  1,

    # Tier 2 — opportunistic LLM
    "llm_extraction":          2,
    "llm_entity_validator":    2,
    "llm_entity_confirmer":    2,
    "auto_validator":          2,
    "document_validator":      2,
    "assessment_validator":    2,
    "candidate_promoter":      2,
    # Research pipelines (May 8 2026 wiring fix). Tier 2 because they're
    # important but skippable under pressure — opportunistic, not always-on.
    "investigation":           2,
    "enrichment_queue_worker": 2,
    # Phase 7c (May 9 2026): scores completed investigations + flags low-value
    # entities for review. Pure read-then-update worker, no LLM, very cheap.
    "investigation_quality_validator": 2,
    # Phase 7d (May 9 2026): per-finding LLM relevance scorer. LLM-medium
    # (one Gemma call per finding) — pauses cleanly when system load high
    # or Ollama unhealthy, like the rest of the LLM-dependent workers.
    "finding_relevance_scorer": 2,

    # Tier 3 — heavy/optional
    "reconciliation":          3,
    "investigation":           3,
    "security_analyst":        3,
    "image_analyst":           3,
    "ldup_promoter":           3,
    "article_clusterer":       3,
    "watch_derivatives":       3,
    "personal_linker":         3,
    "personal_location":       3,
    "personal_contacts":       3,
    "personal_comms":          3,
    "personal_supervisor":     3,
    "sigint_worker":           3,
    # Phase 2.5 (May 12 2026): scanned-PDF OCR pipeline. GPU/CPU-heavy
    # PaddleOCR + Ollama-vision fallback, processes one document at a time.
    "ocr_worker":              3,
}

# Workers managed via storage.signals.workers (DB status column).
# Everything else is registered via supervisor.register_worker.
DB_BACKED_WORKERS = frozenset({
    "parser", "classifier", "ocr", "ai_classifier",
    "auto_approve", "promoter",
})

# ── Mode policy thresholds ──────────────────────────────────────────────

# Load avg 5m thresholds (8-core box). Above this → step down a mode.
LOAD_DEGRADED  = 8.0
LOAD_LOADED    = 12.0

# How many consecutive Ollama failures before going emergency.
OLLAMA_FAIL_EMERGENCY = 2

CHECK_INTERVAL = 30          # seconds between manager ticks
RAMP_DELAY     = 30          # min seconds between tier-up steps during recovery


# ── State ───────────────────────────────────────────────────────────────

_MANAGER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_DISABLED: set[str] = set()      # worker_ids the manager is currently keeping off
_DISABLED_LOCK = threading.Lock()

_STATE = {
    "running": False,
    "mode": "cold",
    "current_max_tier": -1,
    "target_max_tier": -1,
    "ticks": 0,
    "last_tick": None,
    "last_ramp_at": None,
    "load_5m": None,
    "ollama_healthy": None,
    "disabled_workers": [],
    "transitions": [],            # rolling log of last N mode changes
}
_MAX_TRANSITIONS = 20


def is_manager_disabled(worker_id: str) -> bool:
    """Cheap check used by the supervisor to skip restarts on paused workers."""
    with _DISABLED_LOCK:
        return worker_id in _DISABLED


def get_manager_status() -> dict:
    snapshot = dict(_STATE)
    with _DISABLED_LOCK:
        snapshot["disabled_workers"] = sorted(_DISABLED)
    snapshot["worker_tiers"] = dict(WORKER_TIERS)
    return snapshot


def start_manager(tenant_id: str = "default") -> dict:
    global _MANAGER_THREAD
    if _MANAGER_THREAD and _MANAGER_THREAD.is_alive():
        return {"status": "already_running"}

    _STOP_EVENT.clear()
    _MANAGER_THREAD = threading.Thread(
        target=_manager_loop,
        args=(tenant_id,),
        name="worker-manager",
        daemon=True,
    )
    _MANAGER_THREAD.start()
    _STATE["running"] = True
    _log.info("Worker manager started")
    return {"status": "started"}


def stop_manager() -> dict:
    _STOP_EVENT.set()
    if _MANAGER_THREAD:
        _MANAGER_THREAD.join(timeout=5)
    _STATE["running"] = False
    return {"status": "stopped"}


# ── Loop ────────────────────────────────────────────────────────────────

def _manager_loop(tenant_id: str) -> None:
    while not _STOP_EVENT.is_set():
        try:
            _tick(tenant_id)
            _STATE["ticks"] += 1
            _STATE["last_tick"] = datetime.now(timezone.utc).isoformat()
        except Exception as e:
            _log.warning("Manager tick error: %s", e)

        for _ in range(CHECK_INTERVAL):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)

    _STATE["running"] = False


def _tick(tenant_id: str) -> None:
    load_5m = _read_load_5m()
    ollama_healthy = bool(_OLLAMA_HEALTH.get("healthy", False))
    ollama_fail = int(_OLLAMA_HEALTH.get("consecutive_failures", 0))

    target_mode, target_max = _decide_mode(load_5m, ollama_healthy, ollama_fail)
    current_max = _STATE["current_max_tier"]

    # Recovery ramp: only step UP one tier per RAMP_DELAY seconds.
    new_max = current_max
    if target_max > current_max:
        last_ramp = _STATE.get("last_ramp_at")
        elapsed = _seconds_since(last_ramp)
        if last_ramp is None or elapsed >= RAMP_DELAY:
            new_max = current_max + 1
            _STATE["last_ramp_at"] = datetime.now(timezone.utc).isoformat()
    elif target_max < current_max:
        # Step down immediately on pressure — no graceful drain.
        new_max = target_max

    # Apply the decision.
    _apply_max_tier(new_max, tenant_id)

    if new_max != current_max:
        msg = f"mode {_STATE['mode']}→{target_mode} max_tier {current_max}→{new_max} (load5m={load_5m} ollama_ok={ollama_healthy})"
        _log.info("Manager: %s", msg)
        _STATE["transitions"].append({
            "at": datetime.now(timezone.utc).isoformat(),
            "from_mode": _STATE["mode"],
            "to_mode": target_mode,
            "from_tier": current_max,
            "to_tier": new_max,
            "load_5m": load_5m,
            "ollama_healthy": ollama_healthy,
        })
        _STATE["transitions"] = _STATE["transitions"][-_MAX_TRANSITIONS:]

    _STATE["current_max_tier"] = new_max
    _STATE["target_max_tier"] = target_max
    _STATE["mode"] = target_mode
    _STATE["load_5m"] = load_5m
    _STATE["ollama_healthy"] = ollama_healthy


def _decide_mode(load_5m: Optional[float], ollama_healthy: bool, ollama_fail: int):
    """Return (mode_name, max_tier_int)."""
    if not ollama_healthy and ollama_fail >= OLLAMA_FAIL_EMERGENCY:
        return ("llm_emergency", 0)
    if load_5m is None:
        # Defensive: if /proc/loadavg unreadable, assume normal so we don't
        # silently stall the whole pipeline.
        return ("normal", 3)
    if load_5m >= LOAD_LOADED:
        return ("loaded", 1)
    if load_5m >= LOAD_DEGRADED:
        return ("degraded", 2)
    return ("normal", 3)


def _apply_max_tier(max_tier: int, tenant_id: str) -> None:
    """Enable workers at tier <= max_tier, disable the rest."""
    target_disabled: set[str] = set()
    target_enabled: set[str] = set()
    for worker_id, tier in WORKER_TIERS.items():
        (target_enabled if tier <= max_tier else target_disabled).add(worker_id)

    with _DISABLED_LOCK:
        newly_disabled = target_disabled - _DISABLED
        newly_enabled = target_enabled & _DISABLED
        _DISABLED.clear()
        _DISABLED.update(target_disabled)

    # Apply DB-backed worker statuses. The supervisor consults
    # is_manager_disabled() for code-registered workers, so we don't need
    # to call their start/stop directly — just update the disabled set.
    for worker_id in newly_disabled:
        if worker_id in DB_BACKED_WORKERS:
            _set_db_worker_status(worker_id, "off", tenant_id)
    for worker_id in newly_enabled:
        if worker_id in DB_BACKED_WORKERS:
            _set_db_worker_status(worker_id, "on", tenant_id)


def _set_db_worker_status(worker_id: str, status: str, tenant_id: str) -> None:
    try:
        from storage.signals.workers import update_worker
        update_worker(worker_id, status=status, tenant_id=tenant_id)
    except Exception as e:
        _log.warning("Manager: failed to set %s=%s: %s", worker_id, status, e)


# ── Helpers ─────────────────────────────────────────────────────────────

def _read_load_5m() -> Optional[float]:
    try:
        with open("/proc/loadavg", "r") as f:
            parts = f.read().strip().split()
        return float(parts[1])
    except Exception:
        return None


def _seconds_since(iso_ts: Optional[str]) -> float:
    if not iso_ts:
        return float("inf")
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return float("inf")
