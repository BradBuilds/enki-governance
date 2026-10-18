"""
Detection engine — evaluates signals against active rules.

Currently supports:
  - geofence rules: checks if signal lat/lon is inside a geofence polygon

Called at the end of signal ingestion to fire alerts in real-time.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection


def run_detection(signal: dict, tenant_id: str, dsn: Optional[str] = None):
    """
    Evaluate a newly ingested signal against all enabled detection rules.
    Creates alerts for any matching rules.

    Args:
        signal: dict with at minimum signal_id, signal_type, and parsed payload
        tenant_id: tenant scope
    """
    lat = signal.get("latitude")
    lon = signal.get("longitude")

    # Only run geofence checks if signal has coordinates
    if lat is not None and lon is not None:
        _check_geofences(signal, tenant_id, float(lat), float(lon), dsn)


def _check_geofences(signal: dict, tenant_id: str, lat: float, lon: float,
                      dsn: Optional[str] = None):
    """Check if a signal's coordinates fall inside any active geofences."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Use PostGIS ST_Contains to check all enabled geofences at once
            cur.execute("""
                SELECT g.geofence_id, g.name, g.alert_on, g.rule_id, g.color
                FROM geofences g
                WHERE g.tenant_id = %s
                  AND g.enabled = TRUE
                  AND ST_Contains(g.geometry, ST_SetSRID(ST_Point(%s, %s), 4326))
            """, (tenant_id, lon, lat))  # PostGIS uses lon, lat order

            matches = cur.fetchall()

            for row in matches:
                geofence_id, name, alert_on, rule_id, color = row

                # For "enter" alerts, we always fire (we don't track previous position yet)
                # For "exit" alerts, we'd need to know the previous state
                # For now, fire on any containment match
                if alert_on in ("enter", "both"):
                    _create_alert(
                        cur, conn, tenant_id,
                        signal=signal,
                        geofence_name=name,
                        geofence_id=geofence_id,
                        rule_id=rule_id,
                    )


def _create_alert(cur, conn, tenant_id: str, signal: dict,
                   geofence_name: str, geofence_id: str,
                   rule_id: Optional[str] = None):
    """Create an alert from a geofence match."""
    alert_id = f"alert_{uuid.uuid4().hex[:12]}"
    signal_id = signal.get("signal_id", "unknown")
    device_id = signal.get("device_id", "unknown")
    lat = signal.get("latitude")
    lon = signal.get("longitude")

    title = f"Device {device_id} entered geofence: {geofence_name}"
    now = datetime.now(timezone.utc)

    # Check for duplicate suppression (same device + geofence in last 5 minutes)
    cur.execute("""
        SELECT alert_id FROM alerts
        WHERE tenant_id = %s
          AND category = 'geofence'
          AND title = %s
          AND created_at > NOW() - INTERVAL '5 minutes'
        LIMIT 1
    """, (tenant_id, title))

    if cur.fetchone():
        return  # Suppress duplicate

    cur.execute("""
        INSERT INTO alerts
            (alert_id, tenant_id, rule_id, severity, status, category,
             title, signal_ids, metadata, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        alert_id, tenant_id, rule_id or '', 'medium', 'new', 'geofence',
        title, [signal_id],
        json.dumps({
            "geofence_id": geofence_id,
            "geofence_name": geofence_name,
            "device_id": device_id,
            "latitude": lat,
            "longitude": lon,
        }),
        now, now,
    ))
    conn.commit()
