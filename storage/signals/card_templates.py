"""
Card Template System — persistent, per-signal-type display templates.

A card template defines HOW a signal type renders in the UI:
- Which columns are visible on the card
- Which columns are hidden (but still searchable, alertable, queryable)
- Which fields render as media (map, image, graph, gauge, etc.)
- Which numeric fields are trackable (graphable over time)
- Display name and icon

Templates are created automatically on first ingestion of a new signal type.
The system walks the payload recursively, classifying fields by type and
relevance. New fields that appear in later payloads are auto-merged as
hidden columns — no crash, no data loss.

Admins can edit column visibility and media mapping at any time.

ALL columns (visible or hidden) are available for:
- Detection rules
- Pattern matching
- Observable extraction
- AI chat queries
- Search

The card is just what you choose to SEE. The data is always complete.
"""

from __future__ import annotations

import json
import re
from typing import Optional
from storage.postgres.db import get_connection


# ─── Noise Filter ─────────────────────────────────────────────────────────────
# Fields matching these patterns are internal plumbing — never show on cards.

NOISE_PATTERNS = {
    # Exact matches
    "signal_type", "signal_id", "tenant_id", "node_id", "device_id",
    "identity_id", "ldu_t_id", "ldu_p_id", "cid", "rid", "raw_sha256",
    "raw_byte_length", "device_signature", "origin_node_id", "federation_id",
    "route_path", "source_domain", "format_detected", "detection_confidence",
    "content_type", "ingestion_timestamp", "server_time", "created_at",
    "updated_at", "metadata", "raw", "raw_bytes", "_internal",
    "signal_type_hint",
    # Payload-level fields that duplicate pipeline metadata
    "timestamp",  # raw epoch millis — actual time is in signal.timestamp
}

NOISE_SUFFIXES = ("_id", "_hash", "_rid", "_cid")

NOISE_PREFIXES = ("raw_", "ldu_", "_")


def _is_noise(key: str) -> bool:
    """Return True if this field is internal plumbing and should be hidden."""
    if key in NOISE_PATTERNS:
        return True
    if key.endswith(NOISE_SUFFIXES):
        return True
    if key.startswith(NOISE_PREFIXES):
        return True
    # Long hex strings are hashes
    if re.match(r'^[a-f0-9]{32,}$', key):
        return True
    return False


def _is_noise_value(value) -> bool:
    """Return True if a value looks like an internal hash or ID."""
    if isinstance(value, str) and len(value) >= 32 and re.match(r'^[a-f0-9\-]+$', value):
        return True
    return False


# ─── Relevance Hints ──────────────────────────────────────────────────────────
# Per signal-type-prefix keyword weights. Fields whose path contains a
# high-relevance keyword sort to the top. Not hardcoded paths — just hints.

RELEVANCE_HINTS: dict[str, dict[str, list[str]]] = {
    "signal.device.sensor_reading": {
        "high": ["battery", "level", "cpu", "memory", "ram", "disk", "model",
                 "temperature", "charging"],
        "medium": ["sensor", "clipboard", "swap", "load", "process_count",
                   "hostname", "platform", "uptime"],
        "low": ["sdk", "build", "kernel", "board", "bootloader"],
    },
    "signal.device.network_scan": {
        "high": ["ssid", "rssi", "signal", "bssid", "carrier", "network_type",
                 "wifi", "bluetooth"],
        "medium": ["frequency", "channel", "address", "name", "cell_tower",
                   "capabilities"],
        "low": ["timestamp"],
    },
    "signal.device.network_flow": {
        "high": ["dns", "domain", "flow", "app", "dest_ip", "query"],
        "medium": ["port", "proto", "bytes", "packets"],
        "low": ["timestamp"],
    },
    "signal.device.app_usage": {
        "high": ["app", "url", "browser", "duration", "activity"],
        "medium": ["action", "package", "window"],
        "low": ["event_type"],
    },
    "signal.device.system_log": {
        "high": ["level", "message", "source", "entry_count", "is_root"],
        "medium": ["channel", "provider", "event_id"],
        "low": ["record_id"],
    },
    "signal.geospatial.position_report": {
        "high": ["lat", "lon", "latitude", "longitude", "accuracy", "speed",
                 "callsign"],
        "medium": ["altitude", "bearing", "battery", "provider", "course"],
        "low": ["hdop", "vdop", "satellites"],
    },
    "signal.communications.email": {
        "high": ["from", "to", "subject", "body", "body_preview"],
        "medium": ["cc", "bcc", "attachment", "message_id", "date"],
        "low": ["headers", "mime_type"],
    },
    "signal.security.authentication_failure": {
        "high": ["user", "username", "src_ip", "ip", "result", "action"],
        "medium": ["method", "application", "geo", "country"],
        "low": ["user_agent", "session"],
    },
    "signal.financial.transaction": {
        "high": ["amount", "currency", "from", "to", "sender", "recipient",
                 "description", "merchant", "date", "account"],
        "medium": ["type", "status", "reference", "memo", "balance",
                   "check_number", "category", "posting_date", "transaction_count"],
        "low": ["fee", "exchange_rate", "sic", "ref_number", "transaction_id"],
    },
    "signal.financial.statement": {
        "high": ["transaction_count", "total_debits", "total_credits", "net",
                 "account", "institution", "period_start", "period_end"],
        "medium": ["balance", "account_type", "format", "page_count",
                   "summary", "transactions"],
        "low": ["file_sha256", "date_range"],
    },
    "signal.vehicle.alpr": {
        "high": ["plate", "plate_number", "confidence", "camera_id"],
        "medium": ["vehicle_description", "color", "make", "model"],
        "low": ["direction", "lane"],
    },
    "signal.device.process_list": {
        "high": ["process_count", "processes", "top_processes"],
        "medium": ["name", "pid", "cpu", "memory", "memory_percent"],
        "low": ["ppid", "threads", "create_time"],
    },
    "signal.device.browser_history": {
        "high": ["entries", "entry_count", "browser"],
        "medium": ["url", "title", "visit_count", "last_visit"],
        "low": ["profile"],
    },
    # ─── Aviation Weather ────────────────────────────────────────────────
    "signal.environmental.aviation_weather": {
        "high": ["temp", "dewp", "wdir", "wspd", "visib", "name", "icaoId",
                 "rawOb", "fltcat", "cover", "clouds", "altim"],
        "medium": ["wgst", "ceil", "prior", "elev", "lat", "lon",
                   "obsTime", "reportTime", "wxString"],
        "low": ["metar_id", "taf_id", "mostRecent"],
    },
    # ─── Geospatial ──────────────────────────────────────────────────────
    "signal.geospatial.aircraft_position": {
        "high": ["flight", "alt_baro", "gs", "track", "hex", "squawk",
                 "category", "r", "t"],
        "medium": ["lat", "lon", "alt_geom", "baro_rate", "nav_heading",
                   "nav_altitude_mcp", "ias", "tas", "mach"],
        "low": ["seen", "rssi", "messages", "seen_pos"],
    },
    "signal.geospatial.vessel_position": {
        "high": ["mmsi", "name", "sog", "cog", "heading", "ship_type",
                 "destination", "callsign"],
        "medium": ["lat", "lon", "draught", "length", "width", "imo",
                   "eta", "nav_status"],
        "low": ["timestamp", "source"],
    },
    "signal.geospatial.geofence_event": {
        "high": ["geofence_name", "event_type", "entity_id", "entity_name"],
        "medium": ["duration", "distance", "speed", "geofence_id"],
        "low": ["rule_id"],
    },
    # ─── Communications ──────────────────────────────────────────────────
    "signal.communications.sms": {
        "high": ["from", "to", "body", "sender", "recipient"],
        "medium": ["direction", "status", "read", "thread_id"],
        "low": ["sim_slot", "service_center"],
    },
    "signal.communications.chat_message": {
        "high": ["from", "to", "body", "channel", "platform", "sender"],
        "medium": ["thread", "reactions", "attachments", "edited"],
        "low": ["message_type"],
    },
    "signal.communications.notification": {
        "high": ["app", "title", "text", "package"],
        "medium": ["category", "priority", "group", "actions"],
        "low": ["key", "posted_at"],
    },
    "signal.communications.call_log": {
        "high": ["number", "name", "type", "duration"],
        "medium": ["direction", "date", "contact_name", "is_read"],
        "low": ["phone_account", "sim_slot"],
    },
    "signal.communications.voicemail": {
        "high": ["caller", "duration", "transcript"],
        "medium": ["number", "read", "date"],
        "low": ["source_package"],
    },
    "signal.communications.radio_transmission": {
        "high": ["frequency", "callsign", "message", "mode"],
        "medium": ["signal_strength", "bandwidth", "modulation"],
        "low": ["squelch", "power"],
    },
    # ─── Security ────────────────────────────────────────────────────────
    "signal.security.malware_detected": {
        "high": ["malware_name", "file_path", "file_hash", "severity", "action"],
        "medium": ["scanner", "engine_version", "quarantined"],
        "low": ["scan_id"],
    },
    "signal.security.threat_feed": {
        "high": ["indicator", "indicator_type", "threat_type", "confidence",
                 "source"],
        "medium": ["description", "tags", "first_seen", "last_seen"],
        "low": ["feed_name"],
    },
    "signal.security.network_anomaly": {
        "high": ["src_ip", "dst_ip", "port", "protocol", "anomaly_type"],
        "medium": ["bytes", "packets", "duration", "geo"],
        "low": ["flow_id"],
    },
    "signal.security.intrusion_detected": {
        "high": ["rule_name", "src_ip", "dst_ip", "severity", "action"],
        "medium": ["protocol", "port", "signature_id", "category"],
        "low": ["decoder"],
    },
    "signal.security.policy_violation": {
        "high": ["policy_name", "user", "action", "resource"],
        "medium": ["severity", "details", "remediation"],
        "low": ["policy_id"],
    },
    "signal.security.unauthorized_access": {
        "high": ["user", "resource", "action", "src_ip"],
        "medium": ["method", "user_agent", "geo"],
        "low": ["session_id"],
    },
    # ─── Personal ────────────────────────────────────────────────────────
    "signal.personal.contacts": {
        "high": ["contacts", "contact_count", "name", "phone", "email"],
        "medium": ["organization", "title", "notes"],
        "low": ["contact_id", "starred"],
    },
    "signal.personal.calendar_event": {
        "high": ["title", "start", "end", "location", "attendees"],
        "medium": ["description", "organizer", "recurrence", "status"],
        "low": ["calendar_id", "reminders"],
    },
    "signal.personal.sos": {
        "high": ["message", "latitude", "longitude", "battery"],
        "medium": ["accuracy", "speed", "altitude"],
        "low": ["provider"],
    },
    # ─── Financial ───────────────────────────────────────────────────────
    "signal.financial.invoice": {
        "high": ["total", "currency", "vendor", "due_date", "status"],
        "medium": ["line_items", "tax", "subtotal", "invoice_number"],
        "low": ["payment_terms"],
    },
    "signal.financial.payment": {
        "high": ["amount", "currency", "from", "to", "method"],
        "medium": ["status", "reference", "fee"],
        "low": ["processor"],
    },
    "signal.financial.expense": {
        "high": ["amount", "currency", "category", "vendor", "description"],
        "medium": ["date", "receipt", "reimbursable"],
        "low": ["report_id"],
    },
    "signal.financial.balance_update": {
        "high": ["ticker", "price", "change", "change_percent", "market_cap"],
        "medium": ["volume", "high_24h", "low_24h", "name"],
        "low": ["last_updated"],
    },
    # ─── Content ─────────────────────────────────────────────────────────
    "signal.content.news": {
        "high": ["title", "url", "source", "domain"],
        "medium": ["language", "seendate", "socialimage", "sourcecountry"],
        "low": ["url_mobile"],
    },
    "signal.content.social_post": {
        "high": ["author", "title", "body", "text", "content", "url", "username", "handle",
                 "subreddit", "score", "platform", "hashtag", "mention", "media_url", "image"],
        "medium": ["comments", "created", "flair", "awards", "retweet", "like", "share",
                   "reply_count", "engagement", "verified", "follower", "geotag", "location",
                   "channel", "thread", "quote", "embed_url", "video_url", "thumbnail"],
        "low": ["permalink", "id", "lang", "source_app", "client"],
    },
    "signal.content.rss_entry": {
        "high": ["title", "link", "summary", "author"],
        "medium": ["published", "updated", "categories"],
        "low": ["id"],
    },
    "signal.content.image": {
        "high": ["filename", "width", "height", "format", "caption"],
        "medium": ["size_bytes", "camera", "gps_latitude", "gps_longitude"],
        "low": ["exif"],
    },
    "signal.content.document": {
        "high": ["title", "filename", "page_count", "author"],
        "medium": ["size_bytes", "format", "text_preview"],
        "low": ["creator_tool"],
    },
    "signal.content.video": {
        "high": ["filename", "duration", "width", "height", "codec"],
        "medium": ["size_bytes", "fps", "bitrate"],
        "low": ["container"],
    },
    "signal.content.audio": {
        "high": ["filename", "duration", "format", "transcript"],
        "medium": ["size_bytes", "sample_rate", "channels", "bitrate"],
        "low": ["codec"],
    },
    # ─── Business ────────────────────────────────────────────────────────
    "signal.business.crm_record": {
        "high": ["name", "company", "email", "phone", "stage"],
        "medium": ["owner", "value", "status", "last_activity"],
        "low": ["record_type"],
    },
    "signal.business.support_ticket": {
        "high": ["title", "status", "priority", "assignee", "requester"],
        "medium": ["category", "description", "tags", "sla"],
        "low": ["ticket_id"],
    },
    "signal.business.order": {
        "high": ["total", "currency", "status", "customer", "items"],
        "medium": ["shipping_address", "payment_method", "tracking_number"],
        "low": ["order_number"],
    },
    "signal.business.shipment": {
        "high": ["tracking_number", "carrier", "status", "eta"],
        "medium": ["origin", "destination", "weight", "dimensions"],
        "low": ["service_type"],
    },
    # ─── Infrastructure ──────────────────────────────────────────────────
    "signal.infrastructure.health_degraded": {
        "high": ["service", "status", "metric", "value", "threshold"],
        "medium": ["host", "region", "duration", "impact"],
        "low": ["check_id"],
    },
    "signal.infrastructure.threshold_exceeded": {
        "high": ["metric", "value", "threshold", "host", "service"],
        "medium": ["severity", "duration", "trend"],
        "low": ["rule_id"],
    },
    "signal.infrastructure.alpr_reading": {
        "high": ["plate", "plate_number", "confidence", "camera_id"],
        "medium": ["vehicle_description", "color", "make", "model",
                   "direction", "speed"],
        "low": ["lane", "image_url"],
    },
    # ─── Intelligence ────────────────────────────────────────────────────
    "signal.intelligence.osint": {
        "high": ["title", "source", "summary", "indicators"],
        "medium": ["confidence", "tags", "references", "author"],
        "low": ["collection_method"],
    },
    "signal.intelligence.humint": {
        "high": ["source_reliability", "information_credibility", "summary"],
        "medium": ["location", "time_observed", "subjects"],
        "low": ["report_number"],
    },
    "signal.intelligence.sigint": {
        "high": ["frequency", "modulation", "emitter_type", "signal_strength"],
        "medium": ["bearing", "location", "bandwidth", "duration"],
        "low": ["intercept_id"],
    },
    # ─── Medical ─────────────────────────────────────────────────────────
    "signal.medical.vital_alert": {
        "high": ["heart_rate", "blood_pressure", "spo2", "temperature",
                 "respiratory_rate"],
        "medium": ["patient_id", "alert_type", "severity"],
        "low": ["device_model"],
    },
    "signal.medical.lab_result": {
        "high": ["test_name", "result", "unit", "reference_range", "status"],
        "medium": ["patient_id", "specimen", "ordered_by"],
        "low": ["lab_id"],
    },
    # ─── Operations ──────────────────────────────────────────────────────
    "signal.operations.mission_update": {
        "high": ["mission_name", "status", "objective", "commander"],
        "medium": ["phase", "location", "personnel_count"],
        "low": ["mission_id"],
    },
    "signal.operations.resource_request": {
        "high": ["resource_type", "quantity", "priority", "requestor"],
        "medium": ["location", "needed_by", "justification"],
        "low": ["request_id"],
    },
    # ─── Vehicle ─────────────────────────────────────────────────────────
    "signal.vehicle.status": {
        "high": ["ignition", "speed", "fuel_level", "odometer"],
        "medium": ["battery_voltage", "engine_temp", "dtc_codes"],
        "low": ["vin"],
    },
    "signal.vehicle.location": {
        "high": ["latitude", "longitude", "speed", "heading", "device_name"],
        "medium": ["altitude", "accuracy", "battery", "ignition", "motion"],
        "low": ["hdop"],
    },
    # ─── Asset ───────────────────────────────────────────────────────────
    "signal.asset.location": {
        "high": ["latitude", "longitude", "asset_name", "asset_type"],
        "medium": ["battery", "accuracy", "zone"],
        "low": ["beacon_id"],
    },
    "signal.asset.status": {
        "high": ["asset_name", "status", "condition", "location"],
        "medium": ["assigned_to", "last_inspection", "notes"],
        "low": ["asset_tag"],
    },
    # ─── CBRN ────────────────────────────────────────────────────────────
    "signal.cbrn.chemical_detection": {
        "high": ["agent", "concentration", "detector", "alarm_level"],
        "medium": ["latitude", "longitude", "wind_speed", "wind_direction"],
        "low": ["detector_model"],
    },
    "signal.cbrn.radiological_detection": {
        "high": ["dose_rate", "isotope", "detector", "alarm_level"],
        "medium": ["latitude", "longitude", "cumulative_dose"],
        "low": ["detector_model"],
    },
    # ─── Interest ────────────────────────────────────────────────────────
    "signal.interest.image": {
        "high": ["title", "url", "source", "description"],
        "medium": ["width", "height", "author", "tags"],
        "low": ["feed_id"],
    },
    "signal.interest.event": {
        "high": ["title", "start", "end", "location", "description"],
        "medium": ["organizer", "url", "category"],
        "low": ["event_id"],
    },
    # ─── Device (additional) ─────────────────────────────────────────────
    "signal.device.file_index": {
        "high": ["file_count", "total_size", "path"],
        "medium": ["files", "extensions", "largest"],
        "low": ["scan_duration"],
    },
    "signal.device.installed_software": {
        "high": ["software_count", "programs", "name", "version"],
        "medium": ["publisher", "install_date", "size"],
        "low": ["registry_key"],
    },
    "signal.device.service_list": {
        "high": ["service_count", "services", "name", "status"],
        "medium": ["display_name", "start_type", "pid"],
        "low": ["path"],
    },
    "signal.device.startup_items": {
        "high": ["items", "item_count", "name", "command"],
        "medium": ["location", "enabled", "user"],
        "low": ["registry_path"],
    },
    "signal.device.dns_cache": {
        "high": ["entries", "entry_count", "name", "address"],
        "medium": ["type", "ttl", "section"],
        "low": ["data_length"],
    },
    "signal.device.shell_command": {
        "high": ["command", "exit_code", "output", "user"],
        "medium": ["cwd", "duration", "shell"],
        "low": ["pid"],
    },
    "signal.device.git_commit": {
        "high": ["message", "repo", "branch", "author"],
        "medium": ["files_changed", "insertions", "deletions", "hash"],
        "low": ["committer"],
    },
    "signal.device.clipboard": {
        "high": ["content_type", "content_preview", "length"],
        "medium": ["source_app", "has_image"],
        "low": ["hash"],
    },
}


def _relevance_score(path: str, signal_type: str) -> int:
    """Score a field path by relevance. Lower = more relevant."""
    # Check exact signal type first, then prefix match
    hints = RELEVANCE_HINTS.get(signal_type)
    if not hints:
        # Prefix match (e.g., signal.security.* matches signal.security.authentication_failure)
        for prefix, h in RELEVANCE_HINTS.items():
            if signal_type and signal_type.startswith(prefix.rsplit(".", 1)[0] + "."):
                hints = h
                break

    if hints:
        path_lower = path.lower()
        for keyword in hints.get("high", []):
            if keyword in path_lower:
                return 10
        for keyword in hints.get("medium", []):
            if keyword in path_lower:
                return 20
        for keyword in hints.get("low", []):
            if keyword in path_lower:
                return 30

    return 50  # Unknown — middle priority


# ─── Payload Walking ──────────────────────────────────────────────────────────

def _walk_payload(payload: dict, prefix: str = "", max_depth: int = 3) -> list[dict]:
    """
    Recursively walk a payload and classify every field.

    Returns a flat list of column definitions with dot-paths:
        {"key": "vitals.level", "label": "Battery Level", ...}

    Arrays of objects become table media sources (not individual columns).
    Arrays of numbers become graph sources.
    Nested dicts recurse with dot-path prefixes.
    """
    columns = []
    media_hints = []

    for key, value in payload.items():
        path = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"

        if _is_noise(key):
            continue
        if _is_noise_value(value):
            continue

        if isinstance(value, dict) and max_depth > 0:
            # Recurse into nested objects
            sub_cols = _walk_payload(value, path, max_depth - 1)
            columns.extend(sub_cols)
        elif isinstance(value, list):
            if len(value) == 0:
                continue
            if isinstance(value[0], dict):
                # Array of objects → table media (not individual columns)
                # Extract column headers from first object's keys
                table_keys = [k for k in value[0].keys() if not _is_noise(k)]
                columns.append({
                    "key": path,
                    "label": _path_to_label(path),
                    "visible": True,
                    "format": "table",
                    "media_type": "table",
                    "table_columns": table_keys[:8],
                    "position": len(columns),
                    "is_array": True,
                    "trackable": False,
                })
            elif all(isinstance(v, (int, float)) for v in value[:10]):
                # Array of numbers → graph source
                columns.append({
                    "key": path,
                    "label": _path_to_label(path),
                    "visible": True,
                    "format": "graph",
                    "media_type": "graph",
                    "position": len(columns),
                    "is_array": True,
                    "trackable": False,
                })
            else:
                # Array of strings or mixed → text summary
                columns.append({
                    "key": path,
                    "label": _path_to_label(path),
                    "visible": len(columns) < 6,
                    "format": "text",
                    "media_type": None,
                    "position": len(columns),
                    "is_array": True,
                    "trackable": False,
                })
        else:
            col = _classify_field(path, value, len(columns))
            columns.append(col)

    return columns


def _path_to_label(path: str) -> str:
    """Convert a dot-path to a human-readable label.

    vitals.level        → Battery Level (if known)
    hardware.model      → Model
    wifi_networks       → WiFi Networks
    sensors.accel.x     → Accel X
    """
    # Use the last segment, or last two if the last is very short (x, y, z)
    parts = path.split(".")
    if len(parts) >= 2 and len(parts[-1]) <= 2:
        label_parts = parts[-2:]
    else:
        label_parts = [parts[-1]]
    return " ".join(p.replace("_", " ").title() for p in label_parts)


# ─── Field Classification ─────────────────────────────────────────────────────

def _classify_field(path: str, value, position: int) -> dict:
    """Classify a single field into a column definition with format + trackable."""
    label = _path_to_label(path)
    key_lower = path.lower()
    fmt = "text"
    media_type = None
    trackable = False
    visible = position < 6  # First 6 fields visible by default

    if isinstance(value, bool):
        fmt = "badge"
    elif isinstance(value, (int, float)):
        fmt = "numeric"
        trackable = True  # All numerics are graphable over time

        # Percentage-like fields → gauge
        if any(kw in key_lower for kw in (
            "percent", "level", "battery", "cpu", "memory", "ram", "disk",
            "humidity", "confidence", "accuracy", "quality", "health",
        )):
            media_type = "gauge"
            visible = True
        # Temperature, pressure, voltage → gauge
        elif any(kw in key_lower for kw in (
            "temperature", "temp", "pressure", "voltage", "current", "power",
            "co2", "pm25", "speed", "rssi", "signal_strength",
        )):
            media_type = "gauge"
            visible = True
    elif isinstance(value, str):
        if "@" in value and "." in value:
            fmt = "email"
        elif value.startswith("http"):
            fmt = "url"
        elif any(kw in key_lower for kw in ("ip", "src_ip", "dst_ip", "source_ip", "dest_ip", "address")):
            if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', value):
                fmt = "ip"
        elif re.match(r'^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$', value):
            fmt = "mac"
        elif value.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
            media_type = "image"
            visible = True

    # Always-show fields (important across many signal types)
    always_show = {
        "from", "to", "subject", "plate", "plate_number", "callsign",
        "user", "username", "amount", "name", "title", "status",
    }
    last_key = path.split(".")[-1]
    if last_key in always_show:
        visible = True

    return {
        "key": path,
        "label": label,
        "visible": visible,
        "format": fmt,
        "media_type": media_type,
        "position": position,
        "is_array": False,
        "trackable": trackable,
    }


# ─── Coordinate Detection ─────────────────────────────────────────────────────

_LAT_KEYS = {"lat", "latitude", "lat_deg"}
_LON_KEYS = {"lon", "longitude", "lng", "lon_deg"}


def _find_coordinates(payload: dict, prefix: str = "") -> Optional[tuple[str, str]]:
    """Find lat/lon field paths anywhere in the payload (including nested).

    Returns (lat_path, lon_path) or None.
    """
    flat_keys = {}

    def _collect(obj, pfx):
        for k, v in obj.items():
            path = f"{pfx}.{k}" if pfx else k
            if isinstance(v, (int, float)):
                flat_keys[k.lower()] = (path, v)
            elif isinstance(v, dict):
                _collect(v, path)

    _collect(payload, prefix)

    lat_path = None
    lon_path = None
    for name in _LAT_KEYS:
        if name in flat_keys:
            path, val = flat_keys[name]
            if -90 <= val <= 90:
                lat_path = path
                break
    for name in _LON_KEYS:
        if name in flat_keys:
            path, val = flat_keys[name]
            if -180 <= val <= 180:
                lon_path = path
                break

    if lat_path and lon_path:
        return (lat_path, lon_path)
    return None


# ─── Template CRUD ────────────────────────────────────────────────────────────

def get_template(signal_type: str, tenant_id: str = "default", dsn: Optional[str] = None) -> Optional[dict]:
    """Get the card template for a signal type."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT signal_type, display_name, icon, columns, media_config,
                       field_count, sample_payload, created_at, updated_at
                FROM card_templates
                WHERE signal_type = %s AND tenant_id = %s
            """, (signal_type, tenant_id))
            r = cur.fetchone()
            if not r:
                return None
            return {
                "signal_type": r[0], "display_name": r[1], "icon": r[2],
                "columns": r[3], "media_config": r[4],
                "field_count": r[5], "sample_payload": r[6],
                "created_at": r[7].isoformat() if r[7] else None,
                "updated_at": r[8].isoformat() if r[8] else None,
            }


def list_templates(tenant_id: str = "default", dsn: Optional[str] = None) -> list[dict]:
    """List all card templates for a tenant."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT signal_type, display_name, icon, field_count, updated_at
                FROM card_templates
                WHERE tenant_id = %s
                ORDER BY display_name
            """, (tenant_id,))
            return [{
                "signal_type": r[0], "display_name": r[1], "icon": r[2],
                "field_count": r[3], "updated_at": r[4].isoformat() if r[4] else None,
            } for r in cur.fetchall()]


def update_template_columns(
    signal_type: str,
    columns: list[dict],
    media_config: Optional[list[dict]] = None,
    tenant_id: str = "default",
    dsn: Optional[str] = None,
) -> bool:
    """Update which columns are visible/hidden and media configuration."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            sets = ["columns = %s", "updated_at = now()"]
            params = [json.dumps(columns)]
            if media_config is not None:
                sets.append("media_config = %s")
                params.append(json.dumps(media_config))
            params.extend([signal_type, tenant_id])
            cur.execute(
                f"UPDATE card_templates SET {', '.join(sets)} WHERE signal_type = %s AND tenant_id = %s",
                params,
            )
            ok = cur.rowcount > 0
            conn.commit()
            return ok


# ─── Auto-Template Generation ─────────────────────────────────────────────────

def ensure_template(
    signal_type: str,
    payload: dict,
    tenant_id: str = "default",
    dsn: Optional[str] = None,
) -> dict:
    """
    Get or create a card template for this signal type.

    On first ingestion of a new type, analyzes the payload and creates
    a template with sensible defaults. Returns the template.
    """
    existing = get_template(signal_type, tenant_id, dsn)
    if existing:
        # Check if new fields appeared — merge them as hidden columns
        return _merge_new_fields(existing, payload, tenant_id, dsn)

    # First time seeing this signal type — create template
    template = _generate_template(signal_type, payload, tenant_id)

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO card_templates (signal_type, display_name, icon, tenant_id,
                    columns, media_config, field_count, sample_payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (signal_type, tenant_id) DO NOTHING
            """, (
                signal_type, template["display_name"], template["icon"], tenant_id,
                json.dumps(template["columns"]), json.dumps(template["media_config"]),
                template["field_count"], json.dumps(payload),
            ))
            conn.commit()

    return template


def _generate_template(signal_type: str, payload: dict, tenant_id: str = "default") -> dict:
    """Generate a card template. Tries AI first, falls back to rule-based."""
    # Try AI-enhanced generation (best-effort)
    try:
        ai_template = _ai_suggest_layout(signal_type, payload, tenant_id)
        if ai_template:
            return ai_template
    except Exception:
        pass

    # Fall back to rule-based generation
    return _rule_based_template(signal_type, payload)


def _ai_suggest_layout(
    signal_type: str, payload: dict, tenant_id: str,
) -> Optional[dict]:
    """Ask the tenant's AI provider to suggest an optimal card layout.

    The AI sees the signal type (from GDU vocabulary) and the payload structure,
    then suggests which fields to display, which embeds to use, and a good title/icon.

    Returns a template dict or None if AI is unavailable.
    """
    from core.ai.config import get_tenant_provider

    provider = get_tenant_provider(tenant_id)
    if not provider:
        return None

    # Build a compact payload sample (keys + value types + sample values)
    field_summary = []
    flat_fields = _walk_payload(payload)
    for col in flat_fields[:20]:
        val = _resolve_path(payload, col["key"])
        vtype = type(val).__name__ if val is not None else "null"
        sample = str(val)[:60] if not isinstance(val, (dict, list)) else f"({vtype})"
        field_summary.append(f"  {col['key']}: {vtype} = {sample}")

    prompt = f"""You are a UI layout designer for a signal intelligence platform.

Signal type (from governed vocabulary): {signal_type}
Payload fields:
{chr(10).join(field_summary)}

Design a card layout. Respond with ONLY valid JSON (no markdown, no explanation):
{{
  "display_name": "Human-readable title",
  "icon": "one of: mail, image, map-pin, lock, shield-alert, thermometer, dollar-sign, globe, car, file-text, video, radar, activity, zap, smartphone, wifi, cpu, hard-drive, monitor",
  "visible_fields": ["field.path1", "field.path2", ...],
  "hidden_fields": ["field.path3", ...],
  "media": [
    {{"type": "map", "lat_field": "lat", "lon_field": "lon"}} or
    {{"type": "gauge", "field": "temperature", "unit": "°F", "min": 0, "max": 120}} or
    {{"type": "image", "field": "image_url"}} or
    {{"type": "graph", "field": "readings"}} or
    {{"type": "table", "field": "items", "columns": ["col1", "col2"]}}
  ]
}}

Rules:
- Maximum 6 visible fields
- Use gauge for numeric values with known ranges (temperature, battery, speed, humidity)
- Use map if lat/lon or latitude/longitude fields exist
- Use image if any field contains an image URL
- Use table for array-of-objects fields
- Hidden fields are still searchable and alertable — they just don't show on the card
- Use dot-paths for nested fields (e.g., vitals.level, hardware.model)"""

    messages = [{"role": "user", "content": prompt}]
    response = provider.complete(messages, max_tokens=500, temperature=0.0)

    # Parse the AI response
    content = response.content.strip()
    # Strip markdown code fences if present
    if content.startswith("```"):
        content = content.split("\n", 1)[1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

    suggestion = json.loads(content)

    # Walk payload to get all columns with proper classification
    columns = _walk_payload(payload)

    # Apply AI visibility suggestions
    visible_set = set(suggestion.get("visible_fields", []))
    hidden_set = set(suggestion.get("hidden_fields", []))
    for col in columns:
        if col["key"] in visible_set:
            col["visible"] = True
        elif col["key"] in hidden_set:
            col["visible"] = False

    # Build media from AI suggestion
    media_config = _build_media_config(payload, columns)

    # Override/add media from AI suggestions
    for m in suggestion.get("media", []):
        if m.get("type") == "gauge" and m.get("field"):
            col = next((c for c in columns if c["key"] == m["field"]), None)
            if col:
                col["media_type"] = "gauge"
                col["visible"] = True
        elif m.get("type") == "table" and m.get("field"):
            col = next((c for c in columns if c["key"] == m["field"]), None)
            if col:
                col["media_type"] = "table"
                if m.get("columns"):
                    col["table_columns"] = m["columns"]

    icon = suggestion.get("icon", _type_to_icon(signal_type))
    display_name = suggestion.get("display_name", _type_to_display_name(signal_type))

    return {
        "signal_type": signal_type,
        "display_name": display_name,
        "icon": icon,
        "columns": columns,
        "media_config": media_config,
        "field_count": len(columns),
        "ai_generated": True,
    }


def _rule_based_template(signal_type: str, payload: dict) -> dict:
    """Analyze a payload and generate a card template with smart defaults.

    Uses recursive walking, noise filtering, and relevance hints
    to produce a template that works for any payload shape.
    """
    # Walk the full payload recursively
    columns = _walk_payload(payload)

    # Sort by relevance for this signal type
    columns.sort(key=lambda c: (_relevance_score(c["key"], signal_type), c["position"]))

    # Re-assign positions and cap visibility at 6
    visible_count = 0
    for i, col in enumerate(columns):
        col["position"] = i
        if col.get("media_type") in ("table", "graph"):
            col["visible"] = True  # Media fields always visible
        elif visible_count >= 6 and col["visible"]:
            col["visible"] = False
        if col["visible"]:
            visible_count += 1

    # Build media config from auto-detection
    media_config = _build_media_config(payload, columns)

    icon = _type_to_icon(signal_type)
    display_name = _type_to_display_name(signal_type)

    return {
        "signal_type": signal_type,
        "display_name": display_name,
        "icon": icon,
        "columns": columns,
        "media_config": media_config,
        "field_count": len(columns),
    }


def _build_media_config(payload: dict, columns: list[dict]) -> list[dict]:
    """Auto-detect media blocks from columns and payload."""
    media_config = []

    # Check for coordinates → map
    coords = _find_coordinates(payload)
    if coords:
        lat_path, lon_path = coords
        media_config.append({
            "type": "map",
            "lat_field": lat_path,
            "lon_field": lon_path,
            "label": "Location",
        })

    # Check columns for gauge, table, graph, image media
    for col in columns:
        if col.get("media_type") == "gauge":
            value = _resolve_path(payload, col["key"])
            if isinstance(value, (int, float)):
                media_config.append({
                    "type": "gauge",
                    "source_field": col["key"],
                    "label": col["label"],
                    "value": value,
                    "min": 0,
                    "max": _guess_max(col["key"], value),
                    "unit": _guess_unit(col["key"]),
                })
        elif col.get("media_type") == "table":
            media_config.append({
                "type": "table",
                "source_field": col["key"],
                "label": col["label"],
                "columns": col.get("table_columns", []),
            })
        elif col.get("media_type") == "graph":
            media_config.append({
                "type": "graph",
                "source_field": col["key"],
                "label": col["label"],
            })
        elif col.get("media_type") == "image":
            media_config.append({
                "type": "image",
                "source_field": col["key"],
                "label": col["label"],
            })

    # Check for email structure
    if "from" in payload and "subject" in payload:
        media_config.append({
            "type": "email",
            "source_field": "email",
            "label": "Email",
        })

    return media_config


def _merge_new_fields(template: dict, payload: dict, tenant_id: str, dsn: Optional[str]) -> dict:
    """If the payload has fields not in the template, add them as hidden columns."""
    existing_keys = {c["key"] for c in template["columns"]}
    new_columns = list(template["columns"])
    changed = False
    position = len(new_columns)

    # Walk the new payload to find any new paths
    current_cols = _walk_payload(payload)

    for col in current_cols:
        if col["key"] not in existing_keys:
            col["visible"] = False  # New fields default to hidden
            col["position"] = position
            new_columns.append(col)
            position += 1
            changed = True

    # Also check for new coordinates
    if changed:
        coords = _find_coordinates(payload)
        if coords:
            existing_media_types = {m.get("type") for m in template.get("media_config", [])}
            if "map" not in existing_media_types:
                media_config = list(template.get("media_config", []))
                lat_path, lon_path = coords
                media_config.append({
                    "type": "map", "lat_field": lat_path,
                    "lon_field": lon_path, "label": "Location",
                })
                update_template_columns(template["signal_type"], new_columns, media_config, tenant_id=tenant_id, dsn=dsn)
                template["columns"] = new_columns
                template["media_config"] = media_config
                template["field_count"] = len(new_columns)
                return template

    if changed:
        update_template_columns(template["signal_type"], new_columns, tenant_id=tenant_id, dsn=dsn)
        template["columns"] = new_columns
        template["field_count"] = len(new_columns)

    return template


# ─── Path Resolution ──────────────────────────────────────────────────────────

def _resolve_path(obj: dict, path: str):
    """Resolve a dot-path against an object. Returns None if not found."""
    parts = path.split(".")
    current = obj
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


# ─── Render Card Data ─────────────────────────────────────────────────────────

def render_card(template: dict, payload: dict, signal_id: str = "",
                lat: float = None, lon: float = None) -> dict:
    """
    Render a signal's card data using its template.

    Returns the card recipe that the frontend uses to render.
    Uses path-based field references so the frontend can resolve values
    at render time and build sparklines from history.
    """
    fields = []
    media = []
    trackable = []

    for col in template["columns"]:
        if not col.get("visible"):
            continue

        value = _resolve_path(payload, col["key"])
        if value is None:
            continue

        # Skip complex values for fields (they become media)
        if col.get("is_array") or isinstance(value, (dict, list)):
            continue

        # Build field entry with both path and resolved value
        field = {
            "label": col["label"],
            "path": col["key"],
            "value": _format_value(value, col.get("format", "text")),
            "format": col.get("format", "text"),
        }
        if col.get("format") == "numeric":
            field["highlight"] = True
        if col.get("format") == "badge" and isinstance(value, bool):
            field["value"] = "Yes" if value else "No"
            field["badge_color"] = "green" if value else "gray"
        fields.append(field)

    # Collect trackable fields
    for col in template["columns"]:
        if col.get("trackable"):
            value = _resolve_path(payload, col["key"])
            if isinstance(value, (int, float)):
                trackable.append({
                    "path": col["key"],
                    "label": col["label"],
                    "unit": _guess_unit(col["key"]),
                    "current": value,
                })

    # Build media blocks from media_config
    for mc in template.get("media_config", []):
        media_block = _build_media_block(mc, payload, signal_id, lat, lon)
        if media_block:
            media.append(media_block)

    # Count array items for summary enrichment (network scans, process lists, etc.)
    array_counts = []
    for col in template["columns"]:
        if col.get("is_array") and col.get("format") == "table":
            arr = _resolve_path(payload, col["key"])
            if isinstance(arr, list) and len(arr) > 0:
                array_counts.append(f"{len(arr)} {col['label']}")

    # Auto-summary from top visible fields + array counts
    summary = _build_auto_summary(fields, template, array_counts)

    return {
        "card_type": template.get("signal_type", "generic").split(".")[-1],
        "title": template["display_name"],
        "icon": template["icon"],
        "media": media,
        "fields": fields[:8],
        "trackable": trackable,
        "summary": summary,
        "alert": None,
    }


def _build_auto_summary(fields: list[dict], template: dict,
                        array_counts: list[str] | None = None) -> str:
    """Build a one-line summary from the first 2-3 highlighted or visible fields.

    If array_counts is provided (e.g., ["76 WiFi Networks", "4 Bluetooth Devices"]),
    those are shown first — ideal for array-heavy signal types like network_scan.
    """
    parts = []
    # Array counts first (e.g., "76 WiFi Networks · 4 Bluetooth Devices")
    if array_counts:
        parts.extend(array_counts[:3])
    # Then highlighted fields
    for f in fields:
        if f.get("highlight") and f.get("value"):
            parts.append(f"{f['label']}: {f['value']}")
        if len(parts) >= 3:
            break
    # Fill with remaining visible fields
    if len(parts) < 2:
        for f in fields:
            if not f.get("highlight") and f.get("value"):
                val = str(f["value"])
                if len(val) <= 30:
                    parts.append(f"{f['label']}: {val}")
                if len(parts) >= 3:
                    break
    return " · ".join(parts) if parts else template.get("display_name", "Signal")


def _format_value(value, fmt: str) -> str:
    """Format a value for display."""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        if fmt == "numeric" and abs(value) < 1000:
            return f"{value:.1f}"
        return f"{value:.2f}" if abs(value) < 1000 else f"{value:,.0f}"
    if isinstance(value, list):
        if len(value) <= 3:
            return ", ".join(str(v) for v in value)
        return f"{len(value)} items"
    if isinstance(value, dict):
        return f"{len(value)} fields"
    return str(value)


def _build_media_block(mc: dict, payload: dict, signal_id: str,
                       lat: float, lon: float) -> Optional[dict]:
    """Build a media block from a media_config entry, resolving paths."""
    if mc["type"] == "map":
        lat_val = _resolve_path(payload, mc.get("lat_field", "lat"))
        lon_val = _resolve_path(payload, mc.get("lon_field", "lon"))
        if lat_val is None:
            lat_val = lat
        if lon_val is None:
            lon_val = lon
        if lat_val is not None and lon_val is not None:
            return {
                "type": "map",
                "lat": float(lat_val),
                "lon": float(lon_val),
                "lat_field": mc.get("lat_field", "lat"),
                "lon_field": mc.get("lon_field", "lon"),
                "label": mc.get("label", "Location"),
            }

    elif mc["type"] == "gauge":
        value = _resolve_path(payload, mc.get("source_field", ""))
        if isinstance(value, (int, float)):
            return {
                "type": "gauge",
                "value": value,
                "source_field": mc.get("source_field"),
                "min": mc.get("min", 0),
                "max": mc.get("max", _guess_max(mc.get("source_field", ""), value)),
                "unit": mc.get("unit", _guess_unit(mc.get("source_field", ""))),
                "thresholds": mc.get("thresholds", []),
            }

    elif mc["type"] == "table":
        data = _resolve_path(payload, mc.get("source_field", ""))
        if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
            col_keys = mc.get("columns", [])
            if not col_keys:
                col_keys = [k for k in data[0].keys() if not _is_noise(k)][:8]
            headers = [k.replace("_", " ").title() for k in col_keys]
            rows = []
            for item in data[:15]:
                row = [str(item.get(k, "")) for k in col_keys]
                rows.append(row)
            return {
                "type": "table",
                "headers": headers,
                "rows": rows,
                "source_field": mc.get("source_field"),
                "column_keys": col_keys,
            }

    elif mc["type"] == "graph":
        data = _resolve_path(payload, mc.get("source_field", ""))
        if isinstance(data, list) and len(data) >= 2:
            return {
                "type": "graph",
                "data": [v for v in data[:50] if isinstance(v, (int, float))],
                "label": mc.get("label", "Trend"),
                "source_field": mc.get("source_field"),
            }

    elif mc["type"] == "image":
        url = _resolve_path(payload, mc.get("source_field", ""))
        if not url and signal_id:
            url = f"/api/signals/{signal_id}/content"
        if url:
            return {
                "type": "image",
                "url": url,
                "caption": mc.get("label", ""),
                "source_field": mc.get("source_field"),
            }

    elif mc["type"] == "email":
        if "from" in payload or "subject" in payload:
            return {
                "type": "email",
                "from": str(payload.get("from", "")),
                "to": payload.get("to", []),
                "subject": str(payload.get("subject", "")),
                "body": str(payload.get("body_preview", payload.get("body", "")))[:500],
            }

    return None


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _guess_unit(key: str) -> str:
    key_lower = key.lower().split(".")[-1]
    units = {
        "temperature": "°F", "temp": "°F", "humidity": "%", "pressure": "hPa",
        "voltage": "V", "current": "A", "power": "W", "co2": "ppm",
        "pm25": "µg/m³", "speed": "m/s", "level": "%", "battery": "%",
        "percent": "%", "cpu": "%", "memory": "%", "ram": "%", "disk": "%",
        "cpu_percent": "%", "memory_percent": "%", "disk_percent": "%",
        "swap_percent": "%", "rssi": "dBm", "signal_strength": "dBm",
        "accuracy": "m", "altitude": "m",
    }
    return units.get(key_lower, "")


def _guess_max(key: str, value) -> float:
    key_lower = key.lower().split(".")[-1]
    maxes = {
        "temperature": 120, "temp": 120, "humidity": 100, "pressure": 1100,
        "battery": 100, "level": 100, "percent": 100, "cpu": 100,
        "memory": 100, "ram": 100, "disk": 100, "cpu_percent": 100,
        "memory_percent": 100, "disk_percent": 100, "swap_percent": 100,
        "speed": 120, "voltage": 240, "co2": 2000, "pm25": 500,
        "confidence": 100, "accuracy": 100, "quality": 100, "health": 100,
    }
    if key_lower in maxes:
        return maxes[key_lower]
    if isinstance(value, (int, float)):
        return value * 2 if value > 0 else 100
    return 100


# ─── Icon + Name Helpers ──────────────────────────────────────────────────────

def _type_to_icon(signal_type: str) -> str:
    parts = signal_type.split(".") if signal_type else []
    icons = {
        # Communications
        "email": "mail", "sms": "message-square", "chat_message": "message-circle",
        "notification": "bell", "call_log": "phone", "voicemail": "voicemail",
        "radio_transmission": "radio", "webhook_message": "webhook",
        # Content
        "image": "image", "video": "video", "audio": "music", "document": "file-text",
        "rss_entry": "rss", "news_article": "newspaper", "news": "newspaper",
        "social_post": "share-2", "web_page": "globe", "screenshot": "monitor",
        # Geospatial
        "location": "map-pin", "position_report": "map-pin",
        "aircraft_position": "plane", "vessel_position": "anchor",
        "friendly_ground": "map-pin", "hostile_ground": "crosshair",
        "unknown_ground": "help-circle", "neutral": "circle",
        "sensor_point": "radio", "geofence_event": "hexagon",
        "route": "navigation",
        # Security
        "authentication_failure": "shield-alert", "authentication_success": "lock",
        "network_anomaly": "alert-triangle", "malware_detected": "bug",
        "indicator_match": "crosshair", "intrusion_detected": "shield-alert",
        "policy_violation": "alert-octagon", "unauthorized_access": "lock",
        "threat_feed": "shield",
        # Financial
        "transaction": "dollar-sign", "invoice": "file-text", "payment": "credit-card",
        "expense": "receipt", "balance_update": "trending-up",
        # Environmental
        "temperature": "thermometer", "humidity": "droplets",
        "weather_reading": "cloud-sun", "weather": "cloud-sun",
        "weather_alert": "alert-triangle", "air_quality": "wind",
        "seismic": "activity", "water_level": "waves",
        "aviation_weather": "plane",
        # Device
        "heartbeat": "heart", "sensor_reading": "activity",
        "network_scan": "wifi", "network_flow": "activity",
        "network_connection": "globe", "network_bandwidth": "activity",
        "app_usage": "smartphone", "app_switch": "smartphone",
        "app_focus": "smartphone", "active_window": "monitor",
        "process_list": "cpu", "process_launch": "play",
        "process_exec": "terminal", "process_start": "play",
        "process_exit": "square", "browser_history": "globe",
        "shell_command": "terminal", "git_commit": "git-branch",
        "system_log": "file-text", "system_event": "monitor",
        "login_event": "log-in", "screen_capture": "monitor",
        "clipboard_event": "clipboard", "clipboard": "clipboard",
        "file_operation": "file", "file_event": "file",
        "file_change": "file-text", "file_index": "hard-drive",
        "file_content": "file-text", "file_alert": "alert-triangle",
        "url_navigation": "globe", "web_visit": "globe",
        "dns_query": "search", "dns_cache": "database",
        "usb_event": "usb", "input_activity": "mouse-pointer",
        "cell_environment": "radio", "gap_detected": "alert-circle",
        "command_receipt": "check-circle",
        "service_list": "server", "service_change": "server",
        "startup_items": "power", "scheduled_tasks": "clock",
        "installed_software": "hard-drive", "network_interfaces": "wifi",
        # Infrastructure
        "alpr_reading": "car", "camera_capture": "camera",
        "health_degraded": "alert-triangle", "threshold_exceeded": "trending-up",
        # Intelligence
        "imagery": "image", "sigint": "radio", "humint": "user",
        "osint": "search", "research_finding": "book-open",
        # Medical
        "vital_alert": "heart-pulse", "lab_result": "flask",
        "medication_event": "pill",
        # Operations
        "tasking": "clipboard-list", "reply": "message-square",
        "mission_update": "flag", "resource_request": "package",
        # Vehicle
        "vehicle_location": "car", "vehicle_status": "car",
        # Asset
        "asset_location": "map-pin", "asset_status": "box",
        "maintenance": "wrench",
        # Personal
        "sos": "alert-circle", "proximity": "bluetooth",
        "call_started": "phone-call", "call_ended": "phone-off",
        "check_in": "check-circle", "contacts": "users",
        "calendar_event": "calendar", "message": "message-circle",
        # Business
        "crm_record": "briefcase", "support_ticket": "ticket",
        "inventory_update": "package", "order": "shopping-cart",
        "shipment": "truck", "calendar_event": "calendar",
        # Interest
        "article": "book-open", "event": "calendar", "update": "bell",
        # CBRN
        "chemical_detection": "alert-triangle", "biological_detection": "alert-triangle",
        "radiological_detection": "alert-triangle", "nuclear_detection": "alert-triangle",
        # Custom
        "untyped": "help-circle",
    }
    for part in reversed(parts):
        if part in icons:
            return icons[part]
    domain_icons = {
        "security": "shield-alert", "communications": "mail", "content": "file-text",
        "personal": "map-pin", "financial": "dollar-sign", "environmental": "thermometer",
        "infrastructure": "activity", "geospatial": "map-pin", "vehicle": "car",
        "medical": "heart-pulse", "device": "smartphone",
    }
    if len(parts) >= 2 and parts[1] in domain_icons:
        return domain_icons[parts[1]]
    return "zap"


def _type_to_display_name(signal_type: str) -> str:
    if not signal_type:
        return "Signal"
    # Use known display names for common types
    names = {
        # Device domain
        "signal.device.sensor_reading": "Device Telemetry",
        "signal.device.network_scan": "Network Environment",
        "signal.device.network_flow": "Traffic Log",
        "signal.device.network_connection": "Network Connections",
        "signal.device.network_bandwidth": "Bandwidth",
        "signal.device.app_usage": "Digital Activity",
        "signal.device.app_switch": "App Switch",
        "signal.device.app_focus": "Window Focus",
        "signal.device.active_window": "Active Window",
        "signal.device.system_log": "System Events",
        "signal.device.system_event": "System Event",
        "signal.device.process_list": "Running Processes",
        "signal.device.process_launch": "Process Launch",
        "signal.device.process_exec": "Process Execution",
        "signal.device.process_start": "New Processes",
        "signal.device.process_exit": "Exited Processes",
        "signal.device.browser_history": "Browser History",
        "signal.device.shell_command": "Shell Commands",
        "signal.device.git_commit": "Git Commit",
        "signal.device.installed_software": "Installed Software",
        "signal.device.file_index": "File System Index",
        "signal.device.file_change": "File Changes",
        "signal.device.file_event": "File Event",
        "signal.device.file_operation": "File Operation",
        "signal.device.file_content": "File Content",
        "signal.device.file_alert": "File Alert",
        "signal.device.screenshot": "Screenshot",
        "signal.device.screen_capture": "Screen Capture",
        "signal.device.command_receipt": "Command Receipt",
        "signal.device.dns_cache": "DNS Cache",
        "signal.device.dns_query": "DNS Query",
        "signal.device.service_list": "Services",
        "signal.device.service_change": "Service Change",
        "signal.device.startup_items": "Startup Items",
        "signal.device.scheduled_tasks": "Scheduled Tasks",
        "signal.device.network_interfaces": "Network Interfaces",
        "signal.device.clipboard": "Clipboard",
        "signal.device.clipboard_event": "Clipboard Event",
        "signal.device.usb_event": "USB Event",
        "signal.device.login_event": "Login Event",
        "signal.device.input_activity": "Input Activity",
        "signal.device.url_navigation": "URL Navigation",
        "signal.device.web_visit": "Web Visit",
        "signal.device.cell_environment": "Cell Environment",
        "signal.device.heartbeat": "Heartbeat",
        "signal.device.gap_detected": "Gap Detected",
        # Geospatial domain
        "signal.geospatial.position_report": "Location",
        "signal.geospatial.friendly_ground": "Friendly Ground",
        "signal.geospatial.friendly_air": "Friendly Air",
        "signal.geospatial.hostile_ground": "Hostile Ground",
        "signal.geospatial.hostile_air": "Hostile Air",
        "signal.geospatial.unknown_ground": "Unknown Ground",
        "signal.geospatial.neutral": "Neutral Entity",
        "signal.geospatial.sensor_point": "Sensor Point",
        "signal.geospatial.geofence_event": "Geofence Event",
        "signal.geospatial.route": "Route",
        "signal.geospatial.aircraft_position": "Aircraft Position",
        "signal.geospatial.vessel_position": "Vessel Position",
        # Communications domain
        "signal.communications.email": "Email",
        "signal.communications.sms": "SMS Message",
        "signal.communications.chat_message": "Chat Message",
        "signal.communications.radio_transmission": "Radio Transmission",
        "signal.communications.webhook_message": "Webhook",
        "signal.communications.voicemail": "Voicemail",
        "signal.communications.notification": "Notification",
        "signal.communications.call_log": "Call Log",
        # Security domain
        "signal.security.authentication_failure": "Failed Login",
        "signal.security.network_anomaly": "Network Anomaly",
        "signal.security.malware_detected": "Malware Detected",
        "signal.security.indicator_match": "IOC Match",
        "signal.security.intrusion_detected": "Intrusion Detected",
        "signal.security.policy_violation": "Policy Violation",
        "signal.security.unauthorized_access": "Unauthorized Access",
        "signal.security.threat_feed": "Threat Intelligence",
        # Financial domain
        "signal.financial.transaction": "Transaction",
        "signal.financial.invoice": "Invoice",
        "signal.financial.payment": "Payment",
        "signal.financial.expense": "Expense",
        "signal.financial.balance_update": "Balance Update",
        # Environmental domain
        "signal.environmental.weather_reading": "Weather",
        "signal.environmental.weather": "Weather Observation",
        "signal.environmental.weather_alert": "Weather Alert",
        "signal.environmental.air_quality": "Air Quality",
        "signal.environmental.temperature": "Temperature",
        "signal.environmental.seismic": "Earthquake",
        "signal.environmental.water_level": "Water Level",
        "signal.environmental.aviation_weather": "Aviation Weather",
        # Content domain
        "signal.content.document": "Document",
        "signal.content.image": "Image",
        "signal.content.video": "Video",
        "signal.content.audio": "Audio",
        "signal.content.social_post": "Social Post",
        "signal.content.news_article": "News Article",
        "signal.content.news": "News",
        "signal.content.rss_entry": "Feed Entry",
        "signal.content.web_page": "Web Page",
        "signal.content.screenshot": "Screenshot",
        # Personal domain
        "signal.personal.location": "Location",
        "signal.personal.message": "Message",
        "signal.personal.proximity": "Proximity",
        "signal.personal.sos": "Emergency SOS",
        "signal.personal.call_started": "Call Started",
        "signal.personal.call_ended": "Call Ended",
        "signal.personal.check_in": "Check-In",
        "signal.personal.contacts": "Contacts",
        "signal.personal.calendar_event": "Calendar Event",
        # Business domain
        "signal.business.crm_record": "CRM Record",
        "signal.business.support_ticket": "Support Ticket",
        "signal.business.inventory_update": "Inventory Update",
        "signal.business.order": "Order",
        "signal.business.shipment": "Shipment",
        "signal.business.calendar_event": "Calendar Event",
        # Infrastructure domain
        "signal.infrastructure.health_degraded": "Health Degraded",
        "signal.infrastructure.threshold_exceeded": "Threshold Exceeded",
        "signal.infrastructure.sensor_reading": "Sensor Reading",
        "signal.infrastructure.camera_capture": "Camera Capture",
        "signal.infrastructure.alpr_reading": "License Plate",
        # Intelligence domain
        "signal.intelligence.imagery": "Imagery",
        "signal.intelligence.sigint": "SIGINT",
        "signal.intelligence.humint": "HUMINT Report",
        "signal.intelligence.osint": "OSINT",
        "signal.intelligence.threat_feed": "Threat Feed",
        "signal.intelligence.research_finding": "Research Finding",
        # Medical domain
        "signal.medical.vital_alert": "Vital Signs Alert",
        "signal.medical.lab_result": "Lab Result",
        "signal.medical.medication_event": "Medication Event",
        # Operations domain
        "signal.operations.tasking": "Task",
        "signal.operations.reply": "Task Reply",
        "signal.operations.mission_update": "Mission Update",
        "signal.operations.resource_request": "Resource Request",
        # Vehicle domain
        "signal.vehicle.location": "Vehicle Location",
        "signal.vehicle.status": "Vehicle Status",
        # Asset domain
        "signal.asset.location": "Asset Location",
        "signal.asset.status": "Asset Status",
        "signal.asset.maintenance": "Maintenance Event",
        # Interest domain
        "signal.interest.image": "Image Feed",
        "signal.interest.article": "Article Feed",
        "signal.interest.event": "Event Feed",
        "signal.interest.update": "Subscription Update",
        # CBRN domain
        "signal.cbrn.chemical_detection": "Chemical Detection",
        "signal.cbrn.biological_detection": "Biological Detection",
        "signal.cbrn.radiological_detection": "Radiological Detection",
        "signal.cbrn.nuclear_detection": "Nuclear Detection",
        # Custom
        "signal.custom.untyped": "Unclassified Signal",
    }
    if signal_type in names:
        return names[signal_type]
    parts = signal_type.replace("signal.", "").split(".")
    return " ".join(p.replace("_", " ").title() for p in parts)
