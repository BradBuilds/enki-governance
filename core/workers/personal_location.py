"""
Personal Location Worker — extracts GPS position reports from photo metadata batches.

Processes signal.personal.photo_metadata_bulk signals from the mobile app.
For each photo with has_gps=true, creates:
  1. A position_report enrichment in signal_enrichments (marks as processed)
  2. A new signal.geospatial.position_report signal for immediate map display

Purely structural — no LLM needed.
Runs as a daemon thread following the standard worker pattern.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.personal_location")

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "signals_processed": 0,
    "photos_with_gps": 0,
    "position_reports_created": 0,
    "errors": 0,
    "batch_num": 0,
}

CHECK_INTERVAL = 10
IDLE_BACKOFF = 45
MAX_BACKOFF = 90


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_worker_loop, args=(tenant_id,),
        name="personal-location-worker", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    log.info("Personal location worker started")
    return {"status": "started"}


def stop_worker():
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=10)
    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"
    return {"status": "stopped"}


def _worker_loop(tenant_id: str):
    _STATUS["running"] = True
    backoff = CHECK_INTERVAL

    while not _STOP_EVENT.is_set():
        try:
            processed = _process_batch(tenant_id)
            if processed > 0:
                backoff = CHECK_INTERVAL
                _STATUS["phase"] = "processing"
            else:
                backoff = min(backoff + 15, IDLE_BACKOFF)
                _STATUS["phase"] = "idle — caught up"
        except Exception as e:
            log.warning("Personal location worker error: %s", e)
            _STATUS["errors"] += 1
            _STATUS["phase"] = f"error: {str(e)[:60]}"
            backoff = min(backoff * 2, MAX_BACKOFF)

        for _ in range(int(backoff)):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)

    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"


def _process_batch(tenant_id: str) -> int:
    """Find unprocessed photo_metadata_bulk signals and extract GPS positions."""

    signals = _find_unprocessed_signals(tenant_id, limit=5)
    if not signals:
        return 0

    _STATUS["batch_num"] += 1
    processed = 0

    for sig in signals:
        try:
            gps_count = _process_photo_metadata(sig, tenant_id)
            _STATUS["signals_processed"] += 1
            _STATUS["photos_with_gps"] += gps_count
            processed += 1
        except Exception as e:
            log.warning("Failed to process signal %d: %s", sig["rid"], e)
            _STATUS["errors"] += 1

    _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
    log.info("Location batch %d: processed %d signals, %d GPS points total",
             _STATUS["batch_num"], processed, _STATUS["photos_with_gps"])
    return processed


def _find_unprocessed_signals(tenant_id: str, limit: int) -> list:
    """Find photo_metadata_bulk signals without enrichments."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT s.rid, s.signal_id, s.payload, s.timestamp
                    FROM signals s
                    LEFT JOIN signal_enrichments se
                        ON se.signal_rid = s.rid AND se.tier >= 2
                    WHERE s.tenant_id = %s
                      AND s.signal_type = 'signal.personal.photo_metadata_bulk'
                      AND se.enrichment_id IS NULL
                    ORDER BY s.timestamp DESC
                    LIMIT %s
                """, (tenant_id, limit))

                results = []
                for r in cur.fetchall():
                    results.append({
                        "rid": r[0],
                        "signal_id": r[1],
                        "payload": r[2] or {},
                        "timestamp": r[3],
                    })
                return results
    except Exception as e:
        log.debug("Failed to find photo metadata signals: %s", e)
        return []


def _process_photo_metadata(sig: dict, tenant_id: str) -> int:
    """Extract GPS positions from photo metadata batch and create position reports."""

    payload = sig["payload"]
    photos = payload.get("photos", payload.get("items", []))

    if not isinstance(photos, list):
        photos = [payload] if payload.get("has_gps") else []

    gps_points = []
    for photo in photos:
        lat = photo.get("lat") or photo.get("latitude")
        lon = photo.get("lon") or photo.get("longitude")
        if photo.get("has_gps") and lat and lon:
            gps_points.append({
                "lat": float(lat),
                "lon": float(lon),
                "filename": photo.get("filename", ""),
                "timestamp": photo.get("date_taken", photo.get("timestamp", "")),
                "altitude": photo.get("altitude"),
            })

    import os
    node_id = os.environ.get("ENKI_NODE_ID", "local")

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Create individual position_report signals for map display
            for i, pt in enumerate(gps_points):
                # Use filename or batch index to ensure uniqueness — same location, different photos
                photo_key = pt.get("filename") or f"idx{i}"
                photo_hash = hashlib.sha256(f"{sig['signal_id']}:{photo_key}".encode()).hexdigest()[:12]
                position_signal_id = f"pos_{photo_hash}_{pt['lat']:.6f}_{pt['lon']:.6f}"

                # Convert epoch ms timestamp to ISO if numeric
                ts_val = pt.get("timestamp")
                if isinstance(ts_val, (int, float)) and ts_val > 1_000_000_000:
                    if ts_val > 1_000_000_000_000:  # millis
                        ts_val = datetime.fromtimestamp(ts_val / 1000, tz=timezone.utc).isoformat()
                    else:
                        ts_val = datetime.fromtimestamp(ts_val, tz=timezone.utc).isoformat()

                # Generate a synthetic ldu_t_id for derived signals
                ldu_t_id = f"ldu_t_derived_{photo_hash}"

                cur.execute("""
                    INSERT INTO signals
                        (signal_id, tenant_id, node_id, device_id,
                         signal_type, source_domain, severity, confidence,
                         timestamp, server_time,
                         latitude, longitude, altitude,
                         payload, observables, metadata,
                         ldu_t_id, cid, origin_node_id)
                    VALUES (%s, %s, %s, %s,
                            'signal.geospatial.position_report', 'photo_exif', 'informational', 'confirmed',
                            COALESCE(%s::timestamptz, NOW()), NOW(),
                            %s, %s, %s,
                            %s, '[]'::jsonb, %s,
                            %s, %s, %s)
                    ON CONFLICT (signal_id) DO NOTHING
                """, (
                    position_signal_id, tenant_id, node_id,
                    payload.get("device_id", "unknown"),
                    ts_val,
                    pt["lat"], pt["lon"], pt.get("altitude"),
                    json.dumps({
                        "lat": pt["lat"], "lon": pt["lon"],
                        "source_signal": sig["signal_id"],
                        "filename": pt.get("filename", ""),
                        "altitude": pt.get("altitude"),
                    }),
                    json.dumps({"source": "photo_exif_extraction", "parser": "personal_location_worker"}),
                    ldu_t_id,
                    hashlib.sha256(position_signal_id.encode()).hexdigest(),
                    node_id,
                ))
                _STATUS["position_reports_created"] += 1

            # Write enrichment to mark this signal as processed
            enrichment_data = json.dumps({
                "signal_rid": sig["rid"],
                "gps_points": len(gps_points),
            }, sort_keys=True)
            enrichment_hash = hashlib.sha256(
                ("genesis" + enrichment_data).encode()
            ).hexdigest()

            cur.execute("""
                INSERT INTO signal_enrichments
                    (signal_rid, tenant_id, tier, method, model,
                     entities, relationships, event_type, importance,
                     keywords, summary,
                     enrichment_hash, previous_hash, extracted_at)
                VALUES (%s, %s, 2, 'structural', 'rule_based',
                        %s, %s, %s, %s,
                        %s, %s,
                        %s, NULL, NOW())
            """, (
                sig["rid"], tenant_id,
                json.dumps([]),  # no entities for position reports
                json.dumps([]),  # no relationships
                "position_report",
                1,
                ["gps", "location", "photo"],
                f"Extracted {len(gps_points)} GPS positions from {len(photos)} photos",
                enrichment_hash,
            ))

        conn.commit()

    log.info("Signal %d: extracted %d GPS points from %d photos",
             sig["rid"], len(gps_points), len(photos))
    return len(gps_points)
