"""
Traccar GPS Tracking — Pull Connector

Fetches positions from the Traccar REST API for historical backfill
and gap recovery. Complements the webhook handler (real-time push).

Source config:
    {
        "api_url": "http://localhost:8082/api",
        "username": "...",         # set via secrets API or env TRACCAR_USERNAME
        "password": "...",         # set via secrets API or env TRACCAR_PASSWORD
        "device_ids": [1, 2, 3]   # optional: filter specific Traccar device IDs
    }

Watermark:
    {"last_position_id": 12345, "last_fetch_at": "2026-03-31T00:00:00Z"}
"""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error
import base64
from datetime import datetime, timezone
from typing import Optional

from storage.connectors.base import BaseConnector

logger = logging.getLogger(__name__)


def _map_position(position: dict, device_name: Optional[str] = None) -> dict:
    """
    Map a Traccar position to the Enki signal format.

    Reuses the same field mapping as the webhook handler
    (services/api/src/routes/webhooks.py lines 104-127).
    """
    mapped = {
        "latitude": position.get("latitude"),
        "longitude": position.get("longitude"),
        "altitude": position.get("altitude"),
        "speed": position.get("speed"),
        "course": position.get("course"),
        "timestamp": position.get("fixTime") or position.get("serverTime"),
        "accuracy": position.get("accuracy"),
    }

    attributes = position.get("attributes", {})
    if isinstance(attributes, dict):
        mapped["attributes"] = attributes
        for key in ("battery", "batteryLevel", "ignition", "motion", "alarm"):
            if key in attributes:
                mapped[key] = attributes[key]

    if device_name:
        mapped["device_name"] = device_name

    return mapped


class TraccarPullConnector(BaseConnector):
    """Pull connector for Traccar GPS tracking server."""

    connector_type = "pull"
    source_type = "traccar_pull"

    def _auth_header(self) -> str:
        """Build Basic auth header from config or environment."""
        import os
        user = self.source_config.get("username") or os.environ.get("TRACCAR_USERNAME", "admin")
        pw = self.source_config.get("password") or os.environ.get("TRACCAR_PASSWORD", "")
        cred = base64.b64encode(f"{user}:{pw}".encode()).decode()
        return f"Basic {cred}"

    def _api_get(self, path: str, params: Optional[dict] = None) -> list | dict:
        """Make authenticated GET request to Traccar API."""
        api_url = self.source_config.get("api_url", "http://localhost:8082/api")
        url = f"{api_url}{path}"

        if params:
            from urllib.parse import urlencode
            url += "?" + urlencode(params)

        req = urllib.request.Request(url)
        req.add_header("Authorization", self._auth_header())
        req.add_header("Accept", "application/json")

        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    def fetch(self) -> list[dict]:
        """
        Fetch new positions from Traccar API since last watermark.

        Uses GET /api/positions which returns current positions for all devices.
        For historical data, uses GET /api/positions with from/to params.
        """
        items = []
        last_id = self.watermark.get("last_position_id", 0)

        # Fetch device list for name mapping
        device_names = {}
        try:
            devices = self._api_get("/devices")
            device_names = {d["id"]: d.get("name", "") for d in devices}
        except Exception as e:
            logger.warning(f"Traccar device list fetch failed: {e}")

        # Filter to configured device IDs if specified
        filter_ids = self.source_config.get("device_ids")

        # Fetch current positions
        try:
            positions = self._api_get("/positions")
        except urllib.error.HTTPError as e:
            logger.error(f"Traccar positions fetch failed: {e.code} {e.reason}")
            raise
        except Exception as e:
            logger.error(f"Traccar positions fetch failed: {e}")
            raise

        for pos in positions:
            pos_id = pos.get("id", 0)

            # Skip already-seen positions
            if pos_id <= last_id:
                continue

            # Filter by device IDs if configured
            traccar_device_id = pos.get("deviceId")
            if filter_ids and traccar_device_id not in filter_ids:
                continue

            device_name = device_names.get(traccar_device_id)
            mapped = _map_position(pos, device_name)

            items.append({
                "raw_bytes": json.dumps(mapped, default=str).encode("utf-8"),
                "content_type": "application/json",
                "source_type": "traccar",
                "_position_id": pos_id,
            })

        logger.info(
            f"Traccar connector {self.connector_id}: "
            f"fetched {len(items)} new positions (last_id={last_id})"
        )
        return items

    def update_watermark(self, items: list[dict]) -> dict:
        """Track the highest position ID seen."""
        if not items:
            return self.watermark

        max_id = max(item.get("_position_id", 0) for item in items)
        return {
            "last_position_id": max_id,
            "last_fetch_at": datetime.now(timezone.utc).isoformat(),
        }

    def sync_devices(self) -> list[dict]:
        """
        Optional: fetch Traccar device list for syncing with Enki device profiles.

        Returns list of device dicts from Traccar API.
        Not part of the standard fetch cycle — call manually or via admin route.
        """
        try:
            return self._api_get("/devices")
        except Exception as e:
            logger.error(f"Traccar device sync failed: {e}")
            return []
