"""
Hub Heartbeat Worker — phones home to the Enki Hub (example.com).

Runs on every node where ENKI_HUB_URL is set. Reports health metrics
to the hub every 5 minutes. If the hub is unreachable, the node continues
operating normally — heartbeats retry with exponential backoff.

On first run, if ENKI_HUB_REGISTRATION_TOKEN is set and the node hasn't
registered yet, it will auto-register with the hub.

Configuration (in /etc/enki/enki.conf or .env):
  ENKI_HUB_URL=https://example.com
  ENKI_HUB_REGISTRATION_TOKEN=<one-time token from approval>
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger("enki.hub_heartbeat")

HUB_URL = os.getenv("ENKI_HUB_URL", "")
NODE_ID = os.getenv("ENKI_NODE_ID", "unknown")
REGISTRATION_TOKEN = os.getenv("ENKI_HUB_REGISTRATION_TOKEN", "")
HEARTBEAT_INTERVAL = int(os.getenv("ENKI_HUB_HEARTBEAT_INTERVAL", "300"))  # 5 minutes
MAX_BACKOFF = 1800  # 30 minutes max between retries

_thread: threading.Thread | None = None
_stop_event = threading.Event()
_registered = False


def _collect_health() -> dict:
    """Collect current node health metrics."""
    from storage.postgres.db import get_connection

    metrics = {
        "node_id": NODE_ID,
        "gdu_version": "v6",
        "platform_version": "0.3.0",
        "firmware_version": None,
        "profile": os.getenv("ENKI_PROFILE", "(unset)"),
        "user_count": 0,
        "connector_count": 0,
        "signal_count": 0,
        "uptime_hours": 0,
        "health": "healthy",
        "health_details": {},
    }

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Signal count
                cur.execute("SELECT COUNT(*) FROM signals")
                metrics["signal_count"] = cur.fetchone()[0]

                # User count
                try:
                    cur.execute("SELECT COUNT(*) FROM identities")
                    metrics["user_count"] = cur.fetchone()[0]
                except Exception:
                    pass

                # Connector count
                try:
                    cur.execute("SELECT COUNT(*) FROM connectors WHERE enabled = true")
                    metrics["connector_count"] = cur.fetchone()[0]
                except Exception:
                    pass

                # Node count
                try:
                    cur.execute("SELECT COUNT(*) FROM knowledge_nodes")
                    metrics["health_details"]["node_count"] = cur.fetchone()[0]
                except Exception:
                    pass

                # Entity count
                try:
                    cur.execute("SELECT COUNT(*) FROM entities")
                    metrics["health_details"]["entity_count"] = cur.fetchone()[0]
                except Exception:
                    pass

    except Exception as e:
        metrics["health"] = "degraded"
        metrics["health_details"]["db_error"] = str(e)

    # Check Ollama
    try:
        import httpx
        r = httpx.get(f"{os.getenv('OLLAMA_BASE_URL', 'http://localhost:11434')}/api/tags", timeout=3)
        if r.status_code >= 400:
            metrics["health_details"]["ollama"] = "error"
    except Exception:
        metrics["health_details"]["ollama"] = "unreachable"

    return metrics


def _register_with_hub() -> bool:
    """One-time registration with the hub using the registration token."""
    global _registered

    if not REGISTRATION_TOKEN:
        logger.debug("No registration token set, skipping registration")
        _registered = True  # assume already registered
        return True

    try:
        import httpx

        # Try to read the node's public key
        public_key = ""
        try:
            from storage.node.identity import get_node_public_key
            public_key = get_node_public_key()
        except Exception:
            public_key = "not-available"

        payload = {
            "node_id": NODE_ID,
            "public_key": public_key,
            "display_name": os.getenv("ENKI_NODE_DISPLAY_NAME", NODE_ID),
            "registration_token": REGISTRATION_TOKEN,
        }

        r = httpx.post(
            f"{HUB_URL}/fleet/register",
            json=payload,
            timeout=10,
        )

        if r.status_code == 200:
            data = r.json()
            logger.info(
                "Registered with hub as %s (owner: %s)",
                data.get("node_id"), data.get("owner_email"),
            )
            _registered = True
            return True
        elif r.status_code == 409:
            # Already registered
            logger.info("Node already registered with hub")
            _registered = True
            return True
        else:
            logger.warning("Hub registration failed: %s %s", r.status_code, r.text)
            return False

    except Exception as e:
        logger.warning("Hub registration error: %s", e)
        return False


def _send_heartbeat() -> bool:
    """Send a single heartbeat to the hub. Returns True on success."""
    try:
        import httpx

        metrics = _collect_health()

        r = httpx.post(
            f"{HUB_URL}/fleet/heartbeat",
            json=metrics,
            timeout=10,
        )

        if r.status_code == 200:
            data = r.json()
            pending = data.get("pending_updates", [])
            if pending:
                logger.info("Hub reports %d pending update(s)", len(pending))
                for upd in pending:
                    logger.info("  - %s: %s (severity: %s)",
                                upd.get("update_id"), upd.get("title"), upd.get("severity"))
            return True
        else:
            logger.warning("Hub heartbeat rejected: %s", r.status_code)
            return False

    except Exception as e:
        logger.debug("Hub heartbeat failed: %s", e)
        return False


def _heartbeat_loop():
    """Main heartbeat loop with exponential backoff on failure."""
    backoff = HEARTBEAT_INTERVAL
    consecutive_failures = 0

    # Initial registration
    if not _registered:
        if not _register_with_hub():
            logger.warning("Initial hub registration failed, will retry")

    while not _stop_event.is_set():
        if _send_heartbeat():
            consecutive_failures = 0
            backoff = HEARTBEAT_INTERVAL
        else:
            consecutive_failures += 1
            backoff = min(HEARTBEAT_INTERVAL * (2 ** consecutive_failures), MAX_BACKOFF)
            logger.debug("Hub unreachable, next retry in %ds (attempt %d)", backoff, consecutive_failures)

        _stop_event.wait(backoff)


def start_worker(tenant_id: str = "default"):
    """Start the hub heartbeat worker thread."""
    global _thread

    if not HUB_URL:
        logger.debug("ENKI_HUB_URL not set, hub heartbeat disabled")
        return

    if _thread and _thread.is_alive():
        return

    _stop_event.clear()
    _thread = threading.Thread(target=_heartbeat_loop, daemon=True, name="hub-heartbeat")
    _thread.start()
    logger.info("Hub heartbeat started → %s (every %ds)", HUB_URL, HEARTBEAT_INTERVAL)


def stop_worker():
    """Stop the hub heartbeat worker."""
    global _thread
    _stop_event.set()
    if _thread:
        _thread.join(timeout=5)
        _thread = None
    logger.info("Hub heartbeat stopped")


def get_status() -> dict:
    """Return worker status for the supervisor."""
    return {
        "running": _thread is not None and _thread.is_alive(),
        "hub_url": HUB_URL,
        "node_id": NODE_ID,
        "registered": _registered,
        "interval": HEARTBEAT_INTERVAL,
    }
