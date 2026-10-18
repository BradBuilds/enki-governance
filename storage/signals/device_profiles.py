"""
Device Profile Fast-Path — learn once, apply forever.

When a device sends its first payload, the ingestion pipeline classifies
it normally. After consistent payloads, a device profile is auto-created
mapping the device's JSON structure to signal fields. Every subsequent
payload skips classification and goes straight to LDU-W.

Usage:
    from storage.signals.device_profiles import lookup_device_profile, apply_profile

    profile = lookup_device_profile(device_id, tenant_id)
    if profile:
        mapped = apply_profile(profile, raw_bytes)
        # Skip classify/parse, go straight to LDU-W
"""

import json
import hashlib
from typing import Optional
from datetime import datetime, timezone

from storage.postgres.db import get_connection


def lookup_device_profile(
    device_id: str,
    tenant_id: str,
    dsn: Optional[str] = None,
) -> Optional[dict]:
    """Look up a device profile. Returns dict with field_mapping, type_mapping, etc. or None."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT device_id, tenant_id, profile_id, display_name, device_type,
                       payload_format, parser, field_mapping, type_mapping, defaults,
                       status, signals_ingested
                FROM device_profiles
                WHERE device_id = %s AND tenant_id = %s AND status = 'active'
            """, (device_id, tenant_id))
            row = cur.fetchone()
            if not row:
                return None
            return {
                "device_id": row[0],
                "tenant_id": row[1],
                "profile_id": row[2],
                "display_name": row[3],
                "device_type": row[4],
                "payload_format": row[5],
                "parser": row[6],
                "field_mapping": row[7] if isinstance(row[7], dict) else json.loads(row[7]),
                "type_mapping": row[8] if isinstance(row[8], dict) else json.loads(row[8]),
                "defaults": row[9] if isinstance(row[9], dict) else json.loads(row[9]),
                "status": row[10],
                "signals_ingested": row[11],
            }


def apply_profile(profile: dict, raw_bytes: bytes) -> dict:
    """Apply field_mapping to raw JSON payload. Returns mapped dict.

    field_mapping maps source field names to canonical names:
        {"lat": "latitude", "lon": "longitude", "spd": "speed", ...}

    Fields not in the mapping are preserved as-is.
    """
    text = raw_bytes.decode("utf-8", errors="replace").strip()
    data = json.loads(text)

    if not isinstance(data, dict):
        return data

    mapping = profile.get("field_mapping", {})
    defaults = profile.get("defaults", {})

    mapped = {}
    for key, value in data.items():
        canonical = mapping.get(key, key)
        mapped[canonical] = value

    # Apply defaults for missing fields
    for key, value in defaults.items():
        if key not in mapped:
            mapped[key] = value

    return mapped


def increment_signal_count(
    device_id: str,
    tenant_id: str,
    dsn: Optional[str] = None,
) -> None:
    """Increment signals_ingested and update last_seen_at for a device profile."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE device_profiles
                SET signals_ingested = signals_ingested + 1,
                    last_seen_at = NOW()
                WHERE device_id = %s AND tenant_id = %s
            """, (device_id, tenant_id))
            conn.commit()


def create_device_profile(
    device_id: str,
    tenant_id: str,
    device_type: str,
    payload_format: str,
    parser: str,
    field_mapping: dict,
    type_mapping: dict,
    defaults: Optional[dict] = None,
    display_name: Optional[str] = None,
    learned: bool = False,
    dsn: Optional[str] = None,
) -> dict:
    """Create a new device profile."""
    profile_id = f"prof_{hashlib.sha256(f'{device_id}:{tenant_id}'.encode()).hexdigest()[:12]}"

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO device_profiles
                    (device_id, tenant_id, profile_id, display_name, device_type,
                     payload_format, parser, field_mapping, type_mapping, defaults,
                     status, learned_from_first_payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s)
                ON CONFLICT (device_id) DO NOTHING
                RETURNING device_id
            """, (
                device_id, tenant_id, profile_id,
                display_name or device_id,
                device_type, payload_format, parser,
                json.dumps(field_mapping),
                json.dumps(type_mapping),
                json.dumps(defaults or {}),
                learned,
            ))
            result = cur.fetchone()
            conn.commit()

            if not result:
                return {}  # already exists

            return {
                "device_id": device_id,
                "profile_id": profile_id,
                "status": "active",
            }


def _payload_shape_key(data: dict) -> str:
    """Compute a stable key from the shape (keys + value types) of a JSON payload."""
    parts = []
    for key in sorted(data.keys()):
        val = data[key]
        vtype = type(val).__name__
        parts.append(f"{key}:{vtype}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


# In-memory tracker for consistent payloads (resets on restart, which is fine)
_shape_tracker: dict[str, dict] = {}  # device_id -> {shape_key, count}

AUTO_PROFILE_THRESHOLD = 5


def maybe_auto_create_profile(
    device_id: str,
    tenant_id: str,
    parsed: dict,
    signal_type: str,
    parser_name: str,
    dsn: Optional[str] = None,
) -> Optional[dict]:
    """Track payload shape consistency. Auto-create profile after N consistent payloads.

    Called after successful normal-path LDU-W storage. If the same device sends
    payloads with identical key structure N times, we auto-create a profile
    so future payloads skip classification.
    """
    if not isinstance(parsed, dict) or not signal_type:
        return None

    # Check if profile already exists
    existing = lookup_device_profile(device_id, tenant_id, dsn)
    if existing:
        return None

    shape_key = _payload_shape_key(parsed)
    tracker_key = f"{device_id}:{tenant_id}"

    if tracker_key not in _shape_tracker:
        _shape_tracker[tracker_key] = {"shape_key": shape_key, "count": 1, "types_seen": {signal_type}}
        return None

    tracker = _shape_tracker[tracker_key]

    # Track distinct signal types from this device
    tracker.setdefault("types_seen", set()).add(signal_type)
    if len(tracker["types_seen"]) > 1:
        # Device sends multiple signal types (e.g., desktop agent) — don't auto-profile
        # because a single profile can't represent mixed types
        return None

    if tracker["shape_key"] != shape_key:
        # Shape changed — reset counter
        _shape_tracker[tracker_key] = {"shape_key": shape_key, "count": 1, "types_seen": {signal_type}}
        return None

    tracker["count"] += 1

    if tracker["count"] < AUTO_PROFILE_THRESHOLD:
        return None

    # Threshold reached — build field_mapping (identity mapping for JSON)
    field_mapping = {}
    for key in parsed.keys():
        canonical = _guess_canonical_name(key)
        if canonical != key:
            field_mapping[key] = canonical

    type_mapping = {
        "signal_type": signal_type,
    }

    # Infer device type from signal type domain
    parts = signal_type.split(".")
    device_type = parts[1] if len(parts) >= 2 else "unknown"

    result = create_device_profile(
        device_id=device_id,
        tenant_id=tenant_id,
        device_type=device_type,
        payload_format="json",
        parser=parser_name,
        field_mapping=field_mapping,
        type_mapping=type_mapping,
        learned=True,
        dsn=dsn,
    )

    # Clean up tracker
    _shape_tracker.pop(tracker_key, None)

    return result


def _guess_canonical_name(key: str) -> str:
    """Map common abbreviations to canonical field names."""
    canonical = {
        "lat": "latitude",
        "lon": "longitude",
        "lng": "longitude",
        "alt": "altitude",
        "hae": "altitude",
        "spd": "speed",
        "hdg": "heading",
        "batt": "battery_percent",
        "temp": "temperature",
        "hum": "humidity",
        "ts": "timestamp",
        "src_ip": "source_ip",
        "dst_ip": "destination_ip",
    }
    return canonical.get(key.lower(), key)
