"""Device feed — returns devices with latest signal per type + summaries.

Single query returns everything the feed page needs for initial render.
No N+1 fetches.

Uses the card_templates system for recipe generation:
- Templates are created automatically on first signal of a new type
- Templates persist in DB — user column visibility preferences survive
- New payload fields are auto-merged as hidden columns
- render_card() produces path-based recipes with trackable fields
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from storage.postgres.db import get_connection
from storage.signals.card_templates import ensure_template, render_card

log = logging.getLogger(__name__)


def get_device_feed(tenant_id: str, dsn: Optional[str] = None) -> list[dict]:
    """Get all paired devices with their latest signal per signal_type.

    Returns:
        List of device dicts, each with `latest_by_type` map and `type_counts`.
    """
    with get_connection(dsn) as conn:
     return _query_device_feed(conn, tenant_id)


def _query_device_feed(conn, tenant_id: str) -> list[dict]:
    # 1. Get all active paired devices
    with conn.cursor() as cur:
        cur.execute(
            "SELECT device_id, display_name, device_type, platform, status, "
            "signals_count, last_seen_at, config "
            "FROM paired_devices WHERE tenant_id = %s AND status = 'active' "
            "ORDER BY last_seen_at DESC NULLS LAST",
            (tenant_id,),
        )
        devices = []
        for row in cur.fetchall():
            devices.append({
                "device_id": row[0],
                "display_name": row[1],
                "device_type": row[2],
                "platform": row[3],
                "status": row[4],
                "signals_count": row[5] or 0,
                "last_seen_at": row[6].isoformat() if row[6] else None,
                "config": row[7] if isinstance(row[7], dict) else json.loads(row[7]) if row[7] else {},
            })

    if not devices:
        return []

    # 2. For each device, get latest signal per signal_type using DISTINCT ON
    for device in devices:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT ON (signal_type)
                    signal_id, signal_type, timestamp, payload, metadata,
                    latitude, longitude
                FROM signals
                WHERE tenant_id = %s AND device_id = %s
                ORDER BY signal_type, timestamp DESC
            """, (tenant_id, device["device_id"]))

            latest_by_type = {}
            for row in cur.fetchall():
                signal_id = row[0]
                signal_type = row[1]
                timestamp = row[2].isoformat() if row[2] else None
                payload = row[3] if isinstance(row[3], dict) else json.loads(row[3]) if row[3] else {}
                metadata = row[4] if isinstance(row[4], dict) else json.loads(row[4]) if row[4] else {}
                lat = float(row[5]) if row[5] is not None else None
                lon = float(row[6]) if row[6] is not None else None

                # Use stored recipe from metadata, or generate via template system
                recipe = metadata.get("card_recipe")
                if not recipe:
                    try:
                        template = ensure_template(signal_type, payload, tenant_id)
                        recipe = render_card(template, payload, signal_id or "", lat, lon)
                    except Exception:
                        recipe = None

                latest_by_type[signal_type] = {
                    "signal_id": signal_id,
                    "timestamp": timestamp,
                    "summary": recipe.get("summary", "") if recipe else "",
                    "card_recipe": recipe,
                }

            device["latest_by_type"] = latest_by_type

        # 3. Get signal count per type for this device
        with conn.cursor() as cur:
            cur.execute("""
                SELECT signal_type, count(*)
                FROM signals
                WHERE tenant_id = %s AND device_id = %s
                GROUP BY signal_type
                ORDER BY count(*) DESC
            """, (tenant_id, device["device_id"]))
            device["type_counts"] = {row[0]: row[1] for row in cur.fetchall()}

    return devices


def get_type_history(
    tenant_id: str,
    device_id: str,
    signal_type: str,
    limit: int = 20,
    dsn: Optional[str] = None,
) -> list[dict]:
    """Get signal history for a specific type on a specific device.

    Each signal includes the full payload (for frontend path resolution
    and sparkline extraction) plus a pre-rendered card recipe.
    """
    with get_connection(dsn) as conn:
        # Get or create the template for this signal type
        template = None

        with conn.cursor() as cur:
            cur.execute("""
                SELECT signal_id, signal_type, timestamp, payload, metadata,
                       severity, latitude, longitude
                FROM signals
                WHERE tenant_id = %s AND device_id = %s AND signal_type = %s
                ORDER BY timestamp DESC
                LIMIT %s
            """, (tenant_id, device_id, signal_type, limit))

            signals = []
            for row in cur.fetchall():
                payload = row[3] if isinstance(row[3], dict) else json.loads(row[3]) if row[3] else {}
                metadata = row[4] if isinstance(row[4], dict) else json.loads(row[4]) if row[4] else {}
                lat = float(row[6]) if row[6] is not None else None
                lon = float(row[7]) if row[7] is not None else None

                # Ensure template exists (only creates on first call)
                if template is None and payload:
                    try:
                        template = ensure_template(signal_type, payload, tenant_id)
                    except Exception:
                        pass

                # Build recipe from template
                recipe = metadata.get("card_recipe")
                if not recipe and template:
                    try:
                        recipe = render_card(template, payload, row[0] or "", lat, lon)
                    except Exception:
                        recipe = None

                signals.append({
                    "signal_id": row[0],
                    "signal_type": row[1],
                    "timestamp": row[2].isoformat() if row[2] else None,
                    "payload": payload,
                    "metadata": metadata,
                    "severity": row[5],
                    "summary": recipe.get("summary", "") if recipe else "",
                    "card_recipe": recipe,
                })
            return signals
