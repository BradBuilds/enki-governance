"""
SIGINT Worker — processes WiFi probe, BLE advertisement, and beacon signals
from ESP32 Sentinel devices.

Intelligence operations:
  1. Device fingerprinting — MAC + SSID probe list + vendor = device identity
  2. Dwell time tracking — how long a device stays in range
  3. After-hours detection — device appears outside expected time windows
  4. Return detection — device seen before, left, came back (pattern of life)
  5. SSID history profiling — probe list reveals where device has been
  6. Cross-site correlation — same MAC seen at multiple sentinel sites
  7. Co-travel detection — devices that always appear/disappear together
  8. Alert generation — configurable rules trigger alerts

Runs as a daemon thread following the standard worker pattern.
Processes signal.intelligence.wifi_probe, signal.intelligence.ble_advertisement,
and signal.intelligence.wifi_beacon from the signals table.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.sigint")

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "signals_processed": 0,
    "devices_tracked": 0,
    "alerts_generated": 0,
    "nodes_created": 0,
    "errors": 0,
    "batch_num": 0,
}

CHECK_INTERVAL = 10
IDLE_BACKOFF = 30
MAX_BACKOFF = 60

SIGINT_TYPES = (
    "signal.intelligence.wifi_probe",
    "signal.intelligence.ble_advertisement",
    "signal.intelligence.wifi_beacon",
)

# ─── Alert Rules ─────────────────────────────────────────────────────────────
# These are the default rules. In production, load from DB/config.

DEFAULT_ALERT_RULES = {
    "after_hours": {
        "enabled": True,
        "description": "Device detected outside business hours",
        "start_hour": 22,   # 10 PM
        "end_hour": 6,      # 6 AM
        "min_dwell_sec": 120,  # must dwell > 2 min to trigger
        "severity": "medium",
    },
    "long_dwell": {
        "enabled": True,
        "description": "Device dwelling in area for extended period",
        "threshold_sec": 3600,  # 1 hour
        "severity": "low",
    },
    "frequent_return": {
        "enabled": True,
        "description": "Device returning to site repeatedly",
        "min_visits": 3,
        "window_days": 7,
        "severity": "medium",
    },
    "new_device_after_hours": {
        "enabled": True,
        "description": "Never-before-seen device appears after hours",
        "start_hour": 22,
        "end_hour": 6,
        "severity": "high",
    },
}


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_worker_loop, args=(tenant_id,),
        name="sigint-worker", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    log.info("SIGINT worker started")
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

    # Ensure sigint tracking tables exist
    _ensure_tables()

    while not _STOP_EVENT.is_set():
        try:
            processed = _process_batch(tenant_id)
            if processed > 0:
                backoff = CHECK_INTERVAL
                _STATUS["phase"] = f"processed {processed} signals"
            else:
                backoff = min(backoff + 10, IDLE_BACKOFF)
                _STATUS["phase"] = "idle — caught up"
        except Exception as e:
            log.warning("SIGINT worker error: %s", e)
            _STATUS["errors"] += 1
            _STATUS["phase"] = f"error: {str(e)[:60]}"
            backoff = min(backoff * 2, MAX_BACKOFF)

        for _ in range(int(backoff)):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)

    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"


# ─── Table Setup ─────────────────────────────────────────────────────────────

def _ensure_tables():
    """Create sigint-specific tracking tables if they don't exist."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Device tracking table — one row per unique MAC per site
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS sigint_devices (
                        rid             SERIAL PRIMARY KEY,
                        tenant_id       TEXT NOT NULL DEFAULT 'default',
                        mac_address     TEXT NOT NULL,
                        is_random_mac   BOOLEAN DEFAULT FALSE,
                        vendor          TEXT,
                        device_name     TEXT,
                        manufacturer_id INTEGER,
                        site            TEXT,
                        sentinel_id     TEXT,
                        first_seen      TIMESTAMPTZ NOT NULL,
                        last_seen       TIMESTAMPTZ NOT NULL,
                        visit_count     INTEGER DEFAULT 1,
                        total_dwell_sec INTEGER DEFAULT 0,
                        ssid_history    JSONB DEFAULT '[]'::jsonb,
                        rssi_avg        REAL,
                        last_rssi       INTEGER,
                        metadata        JSONB DEFAULT '{}'::jsonb,
                        UNIQUE (tenant_id, mac_address, site)
                    )
                """)

                # Visits table — one row per appearance/departure cycle
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS sigint_visits (
                        rid             SERIAL PRIMARY KEY,
                        tenant_id       TEXT NOT NULL DEFAULT 'default',
                        mac_address     TEXT NOT NULL,
                        site            TEXT,
                        sentinel_id     TEXT,
                        arrived_at      TIMESTAMPTZ NOT NULL,
                        departed_at     TIMESTAMPTZ,
                        dwell_sec       INTEGER,
                        rssi_min        INTEGER,
                        rssi_max        INTEGER,
                        rssi_avg        REAL,
                        ssids_probed    JSONB DEFAULT '[]'::jsonb,
                        signal_count    INTEGER DEFAULT 1,
                        source_signal_ids JSONB DEFAULT '[]'::jsonb
                    )
                """)

                # Alerts table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS sigint_alerts (
                        rid             SERIAL PRIMARY KEY,
                        tenant_id       TEXT NOT NULL DEFAULT 'default',
                        alert_id        TEXT NOT NULL UNIQUE,
                        rule_name       TEXT NOT NULL,
                        severity        TEXT NOT NULL DEFAULT 'medium',
                        mac_address     TEXT,
                        site            TEXT,
                        sentinel_id     TEXT,
                        title           TEXT NOT NULL,
                        description     TEXT,
                        triggered_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        acknowledged    BOOLEAN DEFAULT FALSE,
                        metadata        JSONB DEFAULT '{}'::jsonb
                    )
                """)

                # Index for fast signal lookups
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_signals_sigint_type
                    ON signals (signal_type) WHERE signal_type LIKE 'signal.intelligence.%%'
                """)

                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_sigint_devices_mac
                    ON sigint_devices (tenant_id, mac_address)
                """)

            conn.commit()
        log.info("SIGINT tables verified")
    except Exception as e:
        log.warning("Failed to ensure SIGINT tables: %s", e)


# ─── Batch Processing ────────────────────────────────────────────────────────

def _process_batch(tenant_id: str) -> int:
    """Find unprocessed SIGINT signals and analyze them."""
    signals = _find_unprocessed(tenant_id, limit=25)
    if not signals:
        return 0

    _STATUS["batch_num"] += 1
    processed = 0

    for sig in signals:
        try:
            _process_signal(sig, tenant_id)
            _STATUS["signals_processed"] += 1
            processed += 1
        except Exception as e:
            log.warning("Failed to process SIGINT signal %s: %s", sig.get("signal_id"), e)
            _STATUS["errors"] += 1

    _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
    return processed


def _find_unprocessed(tenant_id: str, limit: int) -> list:
    """Find SIGINT signals without enrichments."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT s.rid, s.signal_id, s.signal_type, s.payload,
                           s.timestamp, s.server_time, s.tenant_id
                    FROM signals s
                    LEFT JOIN signal_enrichments se
                        ON se.signal_rid = s.rid AND se.method = 'sigint_worker'
                    WHERE s.tenant_id = %s
                      AND s.signal_type = ANY(%s)
                      AND se.enrichment_id IS NULL
                    ORDER BY s.timestamp DESC
                    LIMIT %s
                """, (tenant_id, list(SIGINT_TYPES), limit))

                results = []
                for r in cur.fetchall():
                    payload = r[3]
                    if isinstance(payload, str):
                        try:
                            payload = json.loads(payload)
                        except Exception:
                            payload = {}
                    results.append({
                        "rid": r[0],
                        "signal_id": r[1],
                        "signal_type": r[2],
                        "payload": payload or {},
                        "timestamp": r[3] if not r[4] else r[4],
                        "server_time": r[5],
                        "tenant_id": r[6],
                    })
                return results
    except Exception as e:
        log.debug("Failed to query SIGINT signals: %s", e)
        return []


# ─── Signal Processing ───────────────────────────────────────────────────────

def _process_signal(sig: dict, tenant_id: str):
    """Process a single SIGINT signal: update device tracking, check alerts."""
    payload = sig["payload"]
    signal_type = sig["signal_type"]
    signal_id = sig["signal_id"]
    sig_rid = sig["rid"]

    mac = payload.get("source_mac", "").upper()
    if not mac or mac == "UNKNOWN":
        _mark_processed(sig_rid, tenant_id, {"skipped": "no_mac"})
        return

    is_random = payload.get("is_random_mac", False)
    vendor = payload.get("vendor", "")
    device_name = payload.get("device_name", "")
    rssi = payload.get("rssi", payload.get("signal_strength_dbm", 0))
    site = payload.get("site", "unknown")
    sentinel_id = payload.get("sentinel_id", "unknown")
    ssid = payload.get("ssid", "")
    ssid_history = payload.get("ssid_history", [])
    is_new = payload.get("is_new_device", False)
    is_return = payload.get("is_return", False)
    mfr_id = payload.get("manufacturer_id", 0)
    dwell_ms = payload.get("dwell_ms", 0)

    now = datetime.now(timezone.utc)

    with get_connection() as conn:
        with conn.cursor() as cur:
            # ── Update device tracking ───────────────────────────────────
            cur.execute("""
                INSERT INTO sigint_devices
                    (tenant_id, mac_address, is_random_mac, vendor, device_name,
                     manufacturer_id, site, sentinel_id,
                     first_seen, last_seen, visit_count, total_dwell_sec,
                     ssid_history, last_rssi)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s, %s)
                ON CONFLICT (tenant_id, mac_address, site) DO UPDATE SET
                    last_seen = EXCLUDED.last_seen,
                    visit_count = sigint_devices.visit_count + CASE WHEN %s THEN 1 ELSE 0 END,
                    total_dwell_sec = sigint_devices.total_dwell_sec + %s,
                    last_rssi = EXCLUDED.last_rssi,
                    vendor = COALESCE(NULLIF(EXCLUDED.vendor, ''), sigint_devices.vendor),
                    device_name = COALESCE(NULLIF(EXCLUDED.device_name, ''), sigint_devices.device_name),
                    manufacturer_id = COALESCE(NULLIF(EXCLUDED.manufacturer_id, 0), sigint_devices.manufacturer_id),
                    ssid_history = (
                        SELECT jsonb_agg(DISTINCT elem)
                        FROM jsonb_array_elements(
                            sigint_devices.ssid_history || EXCLUDED.ssid_history
                        ) AS elem
                    )
            """, (
                tenant_id, mac, is_random, vendor or None, device_name or None,
                mfr_id or None, site, sentinel_id,
                now, now, int(dwell_ms / 1000),
                json.dumps(ssid_history if ssid_history else ([ssid] if ssid else [])),
                rssi,
                is_return or is_new,  # increment visit on new/return
                int(dwell_ms / 1000),
            ))

            # ── Get device history for alert evaluation ──────────────────
            cur.execute("""
                SELECT visit_count, total_dwell_sec, first_seen, ssid_history
                FROM sigint_devices
                WHERE tenant_id = %s AND mac_address = %s AND site = %s
            """, (tenant_id, mac, site))
            dev_row = cur.fetchone()
            visit_count = dev_row[0] if dev_row else 1
            total_dwell = dev_row[1] if dev_row else 0
            first_ever_seen = dev_row[2] if dev_row else now
            all_ssids = dev_row[3] if dev_row else []

            # ── Check alert rules ────────────────────────────────────────
            alerts = []

            # After-hours detection
            rule = DEFAULT_ALERT_RULES["after_hours"]
            if rule["enabled"]:
                hour = now.hour
                is_after_hours = (hour >= rule["start_hour"] or hour < rule["end_hour"])
                if is_after_hours and dwell_ms / 1000 >= rule["min_dwell_sec"]:
                    alerts.append({
                        "rule": "after_hours",
                        "severity": rule["severity"],
                        "title": f"After-hours device at {site}: {mac}",
                        "description": (
                            f"Device {mac} ({vendor or 'unknown vendor'}) detected at "
                            f"{site} at {now.strftime('%H:%M')} (after hours). "
                            f"RSSI: {rssi} dBm, dwell: {dwell_ms//1000}s. "
                            f"{'Random MAC' if is_random else 'Real MAC'}."
                            + (f" Probing for: {ssid}" if ssid else "")
                        ),
                    })

            # New device after hours
            rule = DEFAULT_ALERT_RULES["new_device_after_hours"]
            if rule["enabled"] and is_new:
                hour = now.hour
                is_after_hours = (hour >= rule["start_hour"] or hour < rule["end_hour"])
                if is_after_hours:
                    alerts.append({
                        "rule": "new_device_after_hours",
                        "severity": rule["severity"],
                        "title": f"NEW device after hours at {site}: {mac}",
                        "description": (
                            f"Never-before-seen device {mac} ({vendor or 'unknown'}) "
                            f"appeared at {site} at {now.strftime('%H:%M')}. "
                            f"{'Random MAC' if is_random else 'Real MAC'}."
                            + (f" Device name: {device_name}" if device_name else "")
                            + (f" Probing for: {', '.join(ssid_history)}" if ssid_history else "")
                        ),
                    })

            # Long dwell
            rule = DEFAULT_ALERT_RULES["long_dwell"]
            if rule["enabled"] and total_dwell >= rule["threshold_sec"]:
                alerts.append({
                    "rule": "long_dwell",
                    "severity": rule["severity"],
                    "title": f"Long dwell at {site}: {mac} ({total_dwell//60}min)",
                    "description": (
                        f"Device {mac} has been at {site} for {total_dwell//60} minutes. "
                        f"Vendor: {vendor or 'unknown'}."
                        + (f" Name: {device_name}" if device_name else "")
                    ),
                })

            # Frequent return
            rule = DEFAULT_ALERT_RULES["frequent_return"]
            if rule["enabled"] and visit_count >= rule["min_visits"]:
                days_span = (now - first_ever_seen).days if first_ever_seen else 0
                if days_span <= rule["window_days"]:
                    alerts.append({
                        "rule": "frequent_return",
                        "severity": rule["severity"],
                        "title": f"Frequent visitor at {site}: {mac} ({visit_count} visits)",
                        "description": (
                            f"Device {mac} has returned to {site} {visit_count} times "
                            f"in {days_span} days. Vendor: {vendor or 'unknown'}."
                            + (f" Known SSIDs: {', '.join(all_ssids[:5]) if isinstance(all_ssids, list) else str(all_ssids)}" if all_ssids else "")
                        ),
                    })

            # ── Write alerts ─────────────────────────────────────────────
            for alert in alerts:
                alert_id = f"sigint-{alert['rule']}-{hashlib.sha256(f'{mac}:{site}:{alert['rule']}:{now.date()}'.encode()).hexdigest()[:12]}"
                try:
                    cur.execute("""
                        INSERT INTO sigint_alerts
                            (tenant_id, alert_id, rule_name, severity,
                             mac_address, site, sentinel_id,
                             title, description, metadata)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (alert_id) DO NOTHING
                    """, (
                        tenant_id, alert_id, alert["rule"], alert["severity"],
                        mac, site, sentinel_id,
                        alert["title"], alert["description"],
                        json.dumps({"signal_id": signal_id, "payload_snapshot": {
                            "rssi": rssi, "vendor": vendor, "ssid": ssid,
                            "device_name": device_name, "is_random_mac": is_random,
                        }}),
                    ))
                    _STATUS["alerts_generated"] += 1
                    log.info("SIGINT alert: %s — %s", alert["rule"], alert["title"])
                except Exception as e:
                    log.debug("Alert insert skipped (likely dup): %s", e)

            # ── Create knowledge node for significant devices ────────────
            # Only create nodes for: real MACs, or random MACs with rich data
            should_create_node = (
                not is_random
                or device_name
                or (isinstance(all_ssids, list) and len(all_ssids) >= 3)
                or visit_count >= 3
            )

            if should_create_node and is_new:
                node_id = f"sigint-dev-{hashlib.sha256(f'{mac}:{site}'.encode()).hexdigest()[:12]}"
                # Build SSID profile
                ssid_list = all_ssids if isinstance(all_ssids, list) else []
                ssid_profile = ", ".join(ssid_list[:10]) if ssid_list else "none captured"

                node_title = f"Device: {mac}"
                if device_name:
                    node_title = f"Device: {device_name} ({mac})"
                elif vendor:
                    node_title = f"Device: {vendor} {mac[-8:]}"

                node_desc = (
                    f"{'Random' if is_random else 'Real'} MAC device observed at {site}.\n"
                    f"Vendor: {vendor or 'unknown'}\n"
                    f"SSID probe history: {ssid_profile}\n"
                    f"First seen: {first_ever_seen}\n"
                    f"Visits: {visit_count}, Total dwell: {total_dwell//60}min"
                )

                try:
                    # Admit through GDU gateway for base-60 address
                    from core.gdu.enforce import admit_node as gdu_admit_node
                    gdu_admit_node(
                        tenant_id=tenant_id, category="entity", subtype="device",
                        title=node_title[:200], description=node_desc[:500],
                        signal_rid=0, collapse_key=node_id,
                    )
                    cur.execute("""
                        INSERT INTO knowledge_nodes
                            (node_id, project_id, tenant_id, title, description,
                             node_type, subtype, epistemic_type,
                             confidence, source_count, enrichment_status,
                             gdu_canonical, metadata)
                        VALUES (%s, 'default', %s, %s, %s,
                                'entity', 'sigint_device', 'observation',
                                %s, 1, 'complete',
                                TRUE, %s)
                        ON CONFLICT (node_id) DO UPDATE SET
                            description = EXCLUDED.description,
                            metadata = EXCLUDED.metadata,
                            source_count = knowledge_nodes.source_count + 1
                    """, (
                        node_id, tenant_id, node_title[:500], node_desc[:2000],
                        0.6 if is_random else 0.8,
                        json.dumps({
                            "sigint_device": True,
                            "mac_address": mac,
                            "is_random_mac": is_random,
                            "vendor": vendor,
                            "device_name": device_name,
                            "manufacturer_id": mfr_id,
                            "site": site,
                            "sentinel_id": sentinel_id,
                            "ssid_history": ssid_list[:20],
                            "source_signal_id": signal_id,
                        }),
                    ))
                    _STATUS["nodes_created"] += 1
                except Exception as e:
                    log.debug("Node upsert failed: %s", e)

            # ── Mark signal as processed ─────────────────────────────────
            enrichment_summary = {
                "mac": mac,
                "is_random": is_random,
                "vendor": vendor,
                "is_new": is_new,
                "is_return": is_return,
                "alerts": [a["rule"] for a in alerts],
                "visit_count": visit_count,
                "total_dwell_sec": total_dwell,
            }
            _mark_processed(sig_rid, tenant_id, enrichment_summary, cur=cur)

        conn.commit()

    _STATUS["devices_tracked"] = _count_devices(tenant_id)


def _mark_processed(sig_rid: int, tenant_id: str, summary: dict, cur=None):
    """Write enrichment record to mark signal as processed."""
    enrichment_data = json.dumps(summary, sort_keys=True, default=str)
    enrichment_hash = hashlib.sha256(
        ("sigint" + enrichment_data).encode()
    ).hexdigest()

    def _do_insert(cursor):
        cursor.execute("""
            INSERT INTO signal_enrichments
                (signal_rid, tenant_id, tier, method, model,
                 entities, relationships, event_type, importance,
                 keywords, summary,
                 enrichment_hash, previous_hash, extracted_at)
            VALUES (%s, %s, 2, 'sigint_worker', 'rule_based',
                    %s, %s, %s, %s,
                    %s, %s,
                    %s, NULL, NOW())
            ON CONFLICT DO NOTHING
        """, (
            sig_rid, tenant_id,
            json.dumps([]),
            json.dumps([]),
            "sigint_detection",
            2,
            ["sigint", "wifi", "ble", "sentinel"],
            json.dumps(summary, default=str)[:500],
            enrichment_hash,
        ))

    if cur:
        _do_insert(cur)
    else:
        with get_connection() as conn:
            with conn.cursor() as c:
                _do_insert(c)
            conn.commit()


def _count_devices(tenant_id: str) -> int:
    """Count unique tracked devices."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(DISTINCT mac_address) FROM sigint_devices WHERE tenant_id = %s",
                    (tenant_id,)
                )
                return cur.fetchone()[0]
    except Exception:
        return 0
