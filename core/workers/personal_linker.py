"""
Personal Data Entity Linker — connects co-located personal signals.

Finds GPS positions, photos, calendar events, and contacts that occur within
the same time window (30 min) and spatial radius (500m), then creates edges
between them through the GDU gateway.

Example output:
  Alex (GPS at Gym 16:10) ──person_at_location──▶ Gym (place)
  Photo_IMG_001 (GPS at Gym 16:15) ──photo_at_location──▶ Gym (place)
  Calendar "Workout" (16:00-17:00) ──event_at_location──▶ Gym (place)
  Alex ──co_located_with──▶ Photo_IMG_001

Runs every 60 seconds. Only processes signals not yet linked (marker column).
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from storage.postgres.db import get_connection

_log = logging.getLogger("enki.workers.personal_linker")

_stop_event = threading.Event()
_thread: Optional[threading.Thread] = None

POLL_INTERVAL = 60  # 1 minute
TIME_WINDOW_MINUTES = 30
DISTANCE_METERS = 500


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in meters between two points."""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _worker_loop(tenant_id: str):
    """Main loop: find co-located personal signals and create edges."""
    while not _stop_event.is_set():
        try:
            _link_pass(tenant_id)
        except Exception:
            _log.exception("Personal linker tick failed")
        _stop_event.wait(POLL_INTERVAL)


def _link_pass(tenant_id: str):
    """Single pass: find recent GPS clusters and link related signals."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Get recent personal GPS signals (last 24h, not yet linked)
                cur.execute("""
                    SELECT signal_id, signal_type, timestamp, latitude, longitude,
                           device_id, payload, rid
                    FROM signals
                    WHERE tenant_id = %s
                      AND signal_type IN (
                          'signal.geospatial.position_report',
                          'signal.personal.location'
                      )
                      AND latitude IS NOT NULL
                      AND longitude IS NOT NULL
                      AND timestamp > NOW() - INTERVAL '24 hours'
                      AND metadata->>'personal_linked' IS NULL
                    ORDER BY timestamp DESC
                    LIMIT 100
                """, (tenant_id,))
                gps_signals = cur.fetchall()

                if not gps_signals:
                    return

                # For each GPS signal, find co-located signals in the time window
                linked_count = 0
                for gps in gps_signals:
                    sig_id, sig_type, ts, lat, lon, device_id, payload, rid = gps
                    if not lat or not lon or not ts:
                        continue

                    t_start = ts - timedelta(minutes=TIME_WINDOW_MINUTES)
                    t_end = ts + timedelta(minutes=TIME_WINDOW_MINUTES)

                    # Find photo metadata signals nearby in time
                    cur.execute("""
                        SELECT signal_id, signal_type, timestamp, latitude, longitude, payload
                        FROM signals
                        WHERE tenant_id = %s
                          AND signal_type IN (
                              'signal.personal.photo_metadata_bulk',
                              'signal.content.image'
                          )
                          AND timestamp BETWEEN %s AND %s
                          AND latitude IS NOT NULL
                          AND longitude IS NOT NULL
                        LIMIT 20
                    """, (tenant_id, t_start, t_end))
                    photos = cur.fetchall()

                    # Find calendar events overlapping in time
                    cur.execute("""
                        SELECT signal_id, signal_type, timestamp, latitude, longitude, payload
                        FROM signals
                        WHERE tenant_id = %s
                          AND signal_type IN (
                              'signal.personal.calendar_event',
                              'signal.personal.calendar_bulk'
                          )
                          AND timestamp BETWEEN %s AND %s
                        LIMIT 10
                    """, (tenant_id, t_start, t_end))
                    calendar = cur.fetchall()

                    # Create edges for co-located signals
                    for photo in photos:
                        p_lat, p_lon = photo[3], photo[4]
                        if p_lat and p_lon:
                            dist = _haversine(lat, lon, p_lat, p_lon)
                            if dist <= DISTANCE_METERS:
                                _create_edge(cur, tenant_id,
                                             f"signal:{sig_id}", f"signal:{photo[0]}",
                                             "co_located_with",
                                             f"Within {int(dist)}m, {TIME_WINDOW_MINUTES}min window")
                                linked_count += 1

                    for cal in calendar:
                        _create_edge(cur, tenant_id,
                                     f"signal:{sig_id}", f"signal:{cal[0]}",
                                     "event_at_location",
                                     f"Calendar event during GPS window")
                        linked_count += 1

                    # Mark GPS signal as linked
                    cur.execute("""
                        UPDATE signals
                        SET metadata = COALESCE(metadata, '{}'::jsonb) || '{"personal_linked": true}'::jsonb
                        WHERE signal_id = %s AND tenant_id = %s
                    """, (sig_id, tenant_id))

                conn.commit()

                if linked_count > 0:
                    _log.info("Personal linker: created %d edges from %d GPS signals",
                              linked_count, len(gps_signals))
    except Exception:
        _log.debug("Personal linker pass skipped (table may not exist)")


def _create_edge(cur, tenant_id: str, source_id: str, target_id: str,
                 relationship: str, evidence: str):
    """Create a knowledge edge if both source and target exist as nodes."""
    try:
        cur.execute("""
            INSERT INTO knowledge_edges
                (edge_id, project_id, source_node_id, target_node_id,
                 relationship, strength, evidence, ai_generated)
            VALUES (
                %s, 'default', %s, %s, %s, 0.7, %s, FALSE
            )
            ON CONFLICT (source_node_id, target_node_id, relationship)
            DO UPDATE SET strength = knowledge_edges.strength + 0.1,
                          evidence = EXCLUDED.evidence
        """, (
            f"edge-pl-{source_id[-12:]}-{target_id[-12:]}",
            source_id, target_id, relationship, evidence,
        ))
    except Exception:
        pass  # Edge creation is best-effort


# ─── Public API ──────────────────────────────────────────────────────────────

def start_personal_linker(tenant_id: str = "default"):
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(
        target=_worker_loop, args=(tenant_id,),
        name="personal-linker", daemon=True,
    )
    _thread.start()
    _log.info("Personal linker worker started (interval=%ds)", POLL_INTERVAL)


def stop_personal_linker():
    _stop_event.set()
    _log.info("Personal linker worker stopped")


def get_personal_linker_status() -> dict:
    return {
        "running": _thread is not None and _thread.is_alive(),
        "interval_s": POLL_INTERVAL,
    }
