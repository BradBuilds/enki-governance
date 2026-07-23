"""
Device pairing — QR-based mobile device onboarding.

Flow:
  1. User clicks "Add Device" in UI → POST /devices/pair/generate
  2. Server creates a pending token, returns QR payload
  3. User scans QR with phone app
  4. Phone calls POST /devices/pair/activate with token
  5. Server activates device, returns full config
  6. Phone starts sending GPS pings / photos / health data
"""

import os
import secrets
import json
from datetime import datetime, timezone, timedelta

from storage.postgres.db import get_connection


def generate_pairing_token(
    tenant_id: str,
    display_name: str,
    device_type: str = "phone",
    server_url: str = "",
    gps_interval_s: int = 30,
    upload_photos: bool = True,
    upload_health: bool = False,
) -> dict:
    """Generate a pairing token and QR payload. Token expires in 10 minutes."""
    token = secrets.token_urlsafe(32)
    device_id = f"mobile_{secrets.token_hex(6)}"
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)

    config = {
        "gps_interval_s": gps_interval_s,
        "upload_photos": upload_photos,
        "upload_health": upload_health,
    }

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO paired_devices
                    (device_token, tenant_id, device_id, display_name, device_type,
                     status, server_url, config, expires_at)
                VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s, %s)
                RETURNING device_token
            """, (token, tenant_id, device_id, display_name, device_type,
                  server_url, json.dumps(config), expires_at))
        conn.commit()

    # QR payload — everything the phone needs
    qr_payload = {
        "server": server_url,
        "token": token,
        "device_id": device_id,
        "tenant_id": tenant_id,
    }

    return {
        "token": token,
        "device_id": device_id,
        "display_name": display_name,
        "qr_payload": qr_payload,
        "qr_json": json.dumps(qr_payload),
        "expires_at": expires_at.isoformat(),
    }


def activate_device(token: str, platform: str = "unknown") -> dict | None:
    """Phone calls this after scanning QR. Activates the device, returns config."""
    now = datetime.now(timezone.utc)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT device_token, tenant_id, device_id, display_name, device_type,
                       status, server_url, config, expires_at
                FROM paired_devices
                WHERE device_token = %s
            """, (token,))
            row = cur.fetchone()
            if not row:
                return None

            cols = ["device_token", "tenant_id", "device_id", "display_name",
                    "device_type", "status", "server_url", "config", "expires_at"]
            device = dict(zip(cols, row))

            if device["status"] != "pending":
                return {"error": "already_activated" if device["status"] == "active" else "revoked"}

            if device["expires_at"] and device["expires_at"] < now:
                return {"error": "expired"}

            # Activate
            cur.execute("""
                UPDATE paired_devices
                SET status = 'active', platform = %s, activated_at = %s, last_seen_at = %s
                WHERE device_token = %s
            """, (platform, now, now, token))

            # Create device profile for fast-path ingestion
            cur.execute("""
                INSERT INTO device_profiles
                    (device_id, tenant_id, profile_id, display_name, device_type,
                     payload_format, parser, field_mapping, type_mapping, status)
                VALUES (%s, %s, %s, %s, %s, 'json', 'json', '{}', %s, 'active')
                ON CONFLICT (device_id) DO NOTHING
            """, (device["device_id"], device["tenant_id"],
                  f"profile_{device['device_id']}", device["display_name"],
                  device["device_type"],
                  json.dumps({"signal_type": "signal.geospatial.position_report",
                              "source_domain": "enki_mobile"})))

        conn.commit()

    config = device["config"] if isinstance(device["config"], dict) else json.loads(device["config"])

    return {
        "status": "activated",
        "device_id": device["device_id"],
        "tenant_id": device["tenant_id"],
        "server_url": device["server_url"],
        "display_name": device["display_name"],
        "config": config,
        "endpoints": {
            "gps_ping": "/signals/geo/ping",
            "ingest": "/signals/ingest",
            "health": "/health",
        },
    }


def list_devices(tenant_id: str) -> list:
    """List all paired devices for a tenant."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT device_token, device_id, display_name, device_type, platform,
                       status, config, signals_count, last_seen_at, created_at, activated_at
                FROM paired_devices
                WHERE tenant_id = %s AND status != 'pending'
                ORDER BY last_seen_at DESC NULLS LAST
            """, (tenant_id,))
            cols = ["device_token", "device_id", "display_name", "device_type", "platform",
                    "status", "config", "signals_count", "last_seen_at", "created_at", "activated_at"]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_device(device_id: str, tenant_id: str) -> dict | None:
    """Get a single device."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT device_token, device_id, display_name, device_type, platform,
                       status, config, signals_count, last_seen_at, created_at, activated_at
                FROM paired_devices
                WHERE device_id = %s AND tenant_id = %s
            """, (device_id, tenant_id))
            row = cur.fetchone()
            if not row:
                return None
            cols = ["device_token", "device_id", "display_name", "device_type", "platform",
                    "status", "config", "signals_count", "last_seen_at", "created_at", "activated_at"]
            return dict(zip(cols, row))


def update_device_config(device_id: str, tenant_id: str, config_updates: dict) -> bool:
    """Update device config (GPS interval, photo/health toggles)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE paired_devices
                SET config = config || %s::jsonb, updated_at = NOW()
                WHERE device_id = %s AND tenant_id = %s AND status = 'active'
            """, (json.dumps(config_updates), device_id, tenant_id))
            updated = cur.rowcount > 0
        conn.commit()
    return updated


def revoke_device(device_id: str, tenant_id: str) -> bool:
    """Revoke a paired device."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE paired_devices
                SET status = 'revoked'
                WHERE device_id = %s AND tenant_id = %s
            """, (device_id, tenant_id))
            revoked = cur.rowcount > 0
        conn.commit()
    return revoked


def purge_device(device_id: str, tenant_id: str) -> dict:
    """Fully delete a device and ALL its signals from the database."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Must disable append-only triggers to delete signals
            for table, trigger in [
                ("signals", "no_update_signals"),
                ("ldu_t_signals", "no_update_ldu_t_signals"),
                ("observable_index", "no_update_observable_index"),
            ]:
                cur.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")

            # Delete signals from this device
            cur.execute("DELETE FROM observable_index WHERE signal_rid IN (SELECT rid FROM signals WHERE device_id = %s AND tenant_id = %s)", (device_id, tenant_id))
            obs_deleted = cur.rowcount
            cur.execute("DELETE FROM ldu_t_signals WHERE tenant_id = %s AND ldu_t_id IN (SELECT ldu_t_id FROM signals WHERE device_id = %s AND tenant_id = %s)", (tenant_id, device_id, tenant_id))
            cur.execute("DELETE FROM signals WHERE device_id = %s AND tenant_id = %s", (device_id, tenant_id))
            signals_deleted = cur.rowcount

            # Delete device profile
            cur.execute("DELETE FROM device_profiles WHERE device_id = %s AND tenant_id = %s", (device_id, tenant_id))

            # Delete commands
            cur.execute("DELETE FROM device_commands WHERE device_id = %s AND tenant_id = %s", (device_id, tenant_id))

            # Delete paired device entry
            cur.execute("DELETE FROM paired_devices WHERE device_id = %s AND tenant_id = %s", (device_id, tenant_id))
            device_deleted = cur.rowcount > 0

            # Restore triggers
            for table, trigger in [
                ("signals", "no_update_signals"),
                ("ldu_t_signals", "no_update_ldu_t_signals"),
                ("observable_index", "no_update_observable_index"),
            ]:
                cur.execute(f"CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION prevent_mutation()")

        conn.commit()

    return {
        "deleted": device_deleted,
        "signals_removed": signals_deleted,
        "observables_removed": obs_deleted,
    }


def record_device_ping(device_id: str, tenant_id: str):
    """Update last_seen and increment signal count. Called on each GPS ping."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE paired_devices
                SET last_seen_at = NOW(), signals_count = signals_count + 1
                WHERE device_id = %s AND tenant_id = %s AND status = 'active'
            """, (device_id, tenant_id))
        conn.commit()


# ─── Device Commands ──────────────────────────────────────────────────────────

COMMAND_TYPES = [
    "take_photo", "record_video", "record_audio",
    "wifi_scan", "bt_scan", "sensor_snapshot", "get_location",
    "sound_alarm", "flash_light", "display_message",
    "get_contacts", "get_sms", "get_call_log",
    "get_file", "sync_directory", "file_search",
]


def queue_command(device_id: str, tenant_id: str, command_type: str, params: dict = None) -> dict:
    """Queue a remote command for a device. Phone picks it up on next config poll."""
    if command_type not in COMMAND_TYPES:
        return {"error": f"Unknown command: {command_type}. Valid: {COMMAND_TYPES}"}

    command_id = f"cmd_{secrets.token_hex(8)}"

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO device_commands
                    (command_id, tenant_id, device_id, command_type, params)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING command_id
            """, (command_id, tenant_id, device_id, command_type,
                  json.dumps(params or {})))
        conn.commit()

    return {
        "command_id": command_id,
        "device_id": device_id,
        "command_type": command_type,
        "status": "pending",
    }


def get_pending_commands(device_id: str, tenant_id: str) -> list:
    """Get all pending commands for a device (phone calls this)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT command_id, command_type, params, created_at
                FROM device_commands
                WHERE device_id = %s AND tenant_id = %s
                  AND status = 'pending'
                  AND (expires_at IS NULL OR expires_at > NOW())
                ORDER BY created_at ASC
            """, (device_id, tenant_id))
            cols = ["id", "type", "params", "created_at"]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


def complete_command(command_id: str, tenant_id: str, signal_id: str = None) -> bool:
    """Mark a command as completed (phone calls this after execution)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE device_commands
                SET status = 'completed', completed_at = NOW(), result_signal_id = %s
                WHERE command_id = %s AND tenant_id = %s
            """, (signal_id, command_id, tenant_id))
            updated = cur.rowcount > 0
        conn.commit()
    return updated


def list_commands(device_id: str, tenant_id: str, limit: int = 50) -> list:
    """List recent commands for a device (for UI display)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT command_id, command_type, params, status,
                       result_signal_id, created_at, completed_at
                FROM device_commands
                WHERE device_id = %s AND tenant_id = %s
                ORDER BY created_at DESC
                LIMIT %s
            """, (device_id, tenant_id, limit))
            cols = ["command_id", "command_type", "params", "status",
                    "result_signal_id", "created_at", "completed_at"]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
