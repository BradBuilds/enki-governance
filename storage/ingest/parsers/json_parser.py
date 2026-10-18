"""
JSON parser — handles any valid JSON payload.

Covers the widest range of data sources: REST APIs, webhooks,
sensors, CRM exports, financial data, etc.

Structural inference attempts to identify the data domain from keys.
"""

import json
import re
from typing import Optional

from storage.ingest.parsers import ParseResult, register_parser
from storage.ingest.parsers.base import BaseParser
from storage.signals.models import Observable


# Key patterns that suggest specific signal types
_TYPE_HINTS = [
    # (required_keys, signal_type_hint, domain)
    ({"resourceType"}, "signal.medical.vital_alert", "medical_fhir"),
    ({"agent", "rule", "decoder"}, "signal.security.intrusion_detected", "wazuh"),
    ({"agent", "rule"}, "signal.security.indicator_match", "wazuh"),
    ({"lat", "lon"}, "signal.geospatial.position_report", "gps"),
    ({"latitude", "longitude"}, "signal.geospatial.position_report", "gps"),
    ({"wifi_networks"}, "signal.device.network_scan", "device"),
    ({"bluetooth_devices"}, "signal.device.network_scan", "device"),
    ({"cell_towers"}, "signal.device.network_scan", "device"),
    ({"accelerometer"}, "signal.device.sensor_reading", "device"),
    ({"pressure_hpa"}, "signal.device.sensor_reading", "device"),
    ({"temperature"}, "signal.environmental.temperature", "sensor"),
    ({"humidity"}, "signal.environmental.weather_reading", "sensor"),
    ({"amount", "currency"}, "signal.financial.transaction", "financial"),
    ({"amount", "customer"}, "signal.financial.transaction", "financial"),
    ({"url", "title", "visit_time"}, "signal.device.url_navigation", "browser"),
    ({"url", "title"}, "signal.content.web_page", "web"),
    ({"from", "to", "subject"}, "signal.communications.email", "email"),
    ({"src_ip", "dst_ip"}, "signal.security.network_anomaly", "network"),
    ({"source_ip", "destination_ip"}, "signal.security.network_anomaly", "network"),
    ({"ticker", "price"}, "signal.financial.balance_update", "financial"),
    ({"heart_rate"}, "signal.medical.vital_alert", "medical"),
    ({"plate", "camera"}, "signal.infrastructure.alpr_reading", "alpr"),
    ({"license_plate"}, "signal.infrastructure.alpr_reading", "alpr"),
]

# Patterns for extracting observables from values
# ── Field-name-aware observable classification ──────────────────────────────
# Maps field name patterns to observable types.
# When a field name matches, the value is classified regardless of regex.
_FIELD_NAME_OBSERVABLE_MAP: dict[str, tuple[str, ...]] = {
    # Aviation
    "aircraft_registration": ("r", "registration", "tail", "tail_number", "reg"),
    "callsign": ("flight", "callsign", "call", "call_sign"),
    "icao_hex": ("hex", "icao", "icao24", "icao_address", "mode_s"),
    "squawk_code": ("squawk",),
    # Vessels
    "vessel_mmsi": ("mmsi",),
    "vessel_imo": ("imo",),
    "vessel_name": ("shipname", "vessel_name", "ship_name"),
    # Vehicles
    "license_plate": ("plate", "license_plate", "tag", "lpr", "plate_number", "vrm"),
    "vin": ("vin", "vehicle_vin"),
    # People / Identity
    "phone_number": ("phone", "mobile", "tel", "telephone", "phone_number", "cell"),
    "person_name": ("suspect", "suspect_name", "poi_name", "person_of_interest"),
    # Crypto / Financial
    "crypto_address": ("wallet", "wallet_address", "btc_address", "eth_address", "crypto_address"),
    "ticker_symbol": ("symbol", "ticker", "coin_id"),
    "account_number": ("account_number", "acct", "account_no", "acct_no"),
    "routing_number": ("routing_number", "routing", "aba_number"),
    "merchant_name": ("merchant", "merchant_name", "payee", "vendor"),
    # Generic identifiers
    "hash_sha256": ("sha256", "hash", "file_hash", "checksum"),
    "hash_md5": ("md5",),
}

# Invert for fast lookup: field_name_lower → observable_type
_FIELD_TO_TYPE: dict[str, str] = {}
for _obs_type, _fields in _FIELD_NAME_OBSERVABLE_MAP.items():
    for _f in _fields:
        _FIELD_TO_TYPE[_f.lower()] = _obs_type

# Phone number regex (international and US formats)
_PHONE_PATTERN = re.compile(
    r'^\+?1?[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}$'
)

# Aircraft registration: N-numbers (US), G-XXXX (UK), etc.
_AIRCRAFT_REG_PATTERN = re.compile(
    r'^[A-Z]{1,2}-?[A-Z0-9]{2,5}$', re.IGNORECASE
)

_MAC_PATTERN = re.compile(
    r'^(?:[0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}$'
)
_IP_PATTERN = re.compile(
    r'^(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)$'
)
_EMAIL_PATTERN = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')
_URL_PATTERN = re.compile(r'^https?://.+')
_DOMAIN_PATTERN = re.compile(r'^(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}$')


@register_parser
class JsonParser(BaseParser):
    name = "json"
    format_id = "json"

    def can_parse(self, raw_bytes: bytes, content_type: Optional[str] = None) -> float:
        # Content-type hint
        if content_type and "json" in content_type.lower():
            return 0.90

        # Try to parse
        try:
            text = raw_bytes.decode("utf-8", errors="strict").strip()
        except (UnicodeDecodeError, ValueError):
            return 0.0

        if not text:
            return 0.0

        # Must start with { or [
        if text[0] not in "{[":
            return 0.0

        try:
            json.loads(text)
            return 0.80
        except (json.JSONDecodeError, ValueError):
            return 0.0

    def parse(self, raw_bytes: bytes) -> ParseResult:
        text = raw_bytes.decode("utf-8", errors="replace").strip()
        data = json.loads(text)

        # Normalize to dict (if array, wrap it)
        if isinstance(data, list):
            parsed = {"items": data, "_array_length": len(data)}
            keys = set()
            if data and isinstance(data[0], dict):
                keys = set(data[0].keys())
        elif isinstance(data, dict):
            parsed = data
            keys = set(data.keys())
        else:
            parsed = {"value": data}
            keys = set()

        # Priority 1: Explicit signal_type in payload (self-describing signals)
        # Desktop agents, connectors, and any source that knows its own type
        signal_type_hint = None
        source_hint = "http_api"
        explicit_type = parsed.get("signal_type") if isinstance(parsed, dict) else None
        if explicit_type and isinstance(explicit_type, str) and explicit_type.startswith("signal."):
            signal_type_hint = explicit_type
            source_hint = explicit_type.split(".")[1] if "." in explicit_type else "self_described"
        else:
            # Priority 2: Infer signal type from key patterns
            for required_keys, type_hint, domain in _TYPE_HINTS:
                if required_keys.issubset(keys):
                    signal_type_hint = type_hint
                    source_hint = domain
                    break

        # Extract observables from values
        observables = _extract_observables_from_dict(parsed)

        # Compute confidence based on structure quality
        confidence = 0.80
        if explicit_type:
            confidence = 0.95  # self-described = high confidence
        elif signal_type_hint:
            confidence = 0.85

        return ParseResult(
            format_detected="json",
            confidence=confidence,
            parsed=parsed,
            metadata={"source_hint": source_hint, "key_count": len(keys)},
            observables=observables,
            signal_type_hint=signal_type_hint,
            parser_name=self.name,
        )


def _extract_observables_from_dict(
    data: dict,
    prefix: str = "",
    max_depth: int = 5,
) -> list[Observable]:
    """Walk a dict and extract observable-like values."""
    if max_depth <= 0:
        return []

    observables = []

    items = data.items() if isinstance(data, dict) else enumerate(data) if isinstance(data, list) else []

    for key, value in items:
        field_name = f"{prefix}.{key}" if prefix else str(key)

        if isinstance(value, str):
            obs = _classify_string_value(value, field_name)
            if obs:
                observables.append(obs)
        elif isinstance(value, dict):
            observables.extend(_extract_observables_from_dict(value, field_name, max_depth - 1))
        elif isinstance(value, list) and len(value) <= 200:
            for i, item in enumerate(value):
                if isinstance(item, str):
                    obs = _classify_string_value(item, f"{field_name}[{i}]")
                    if obs:
                        observables.append(obs)
                elif isinstance(item, dict):
                    observables.extend(
                        _extract_observables_from_dict(item, f"{field_name}[{i}]", max_depth - 1)
                    )

    return observables


def _classify_string_value(value: str, field_name: str) -> Optional[Observable]:
    """Classify a string value as an observable if it matches a known pattern.

    Two strategies:
    1. Field-name-aware: if the field name matches a known pattern (e.g., "flight",
       "hex", "plate"), classify the value as that observable type regardless of format.
    2. Regex-based: if the value itself looks like a known pattern (IP, email, MAC, URL),
       classify it regardless of field name.
    """
    value = value.strip()
    if not value or len(value) > 1000:
        return None

    # ── Strategy 1: Field-name-aware classification ──────────────────────
    # Extract the leaf field name (e.g., "payload.flight" → "flight")
    leaf = field_name.rsplit(".", 1)[-1].rsplit("[", 1)[0].lower()
    obs_type = _FIELD_TO_TYPE.get(leaf)

    if obs_type and len(value) >= 2:
        # Normalize to lowercase for consistent matching with case watchlists
        normalized = value.strip().lower()

        # Skip very short noise values
        if len(normalized) < 2:
            return None

        return Observable(
            observable_type=obs_type,
            observable_value=value,
            normalized_value=normalized,
            source_field=field_name,
        )

    # ── Strategy 2: Regex-based classification (existing patterns) ───────

    if _MAC_PATTERN.match(value):
        return Observable(
            observable_type="mac_address",
            observable_value=value,
            normalized_value=value.upper().replace("-", ":"),
            source_field=field_name,
        )

    if _IP_PATTERN.match(value):
        return Observable(
            observable_type="ip_address",
            observable_value=value,
            normalized_value=value,
            source_field=field_name,
        )

    if _EMAIL_PATTERN.match(value):
        return Observable(
            observable_type="email_address",
            observable_value=value,
            normalized_value=value.lower(),
            source_field=field_name,
        )

    if _URL_PATTERN.match(value):
        return Observable(
            observable_type="url",
            observable_value=value,
            normalized_value=value.split("?")[0].lower(),
            source_field=field_name,
        )

    if _DOMAIN_PATTERN.match(value) and "." in value and len(value) > 4:
        tld = value.rsplit(".", 1)[-1].lower()
        if tld in {"com", "org", "net", "gov", "edu", "io", "dev", "mil", "co", "us", "uk"}:
            return Observable(
                observable_type="domain_name",
                observable_value=value,
                normalized_value=value.lower(),
                source_field=field_name,
            )

    # ── Strategy 3: Regex for types without field-name matches ───────────

    if _PHONE_PATTERN.match(value):
        normalized = re.sub(r'[^\d+]', '', value)
        return Observable(
            observable_type="phone_number",
            observable_value=value,
            normalized_value=normalized,
            source_field=field_name,
        )

    if _AIRCRAFT_REG_PATTERN.match(value) and leaf in ("r", "registration", "tail"):
        return Observable(
            observable_type="aircraft_registration",
            observable_value=value,
            normalized_value=value.upper().replace("-", ""),
            source_field=field_name,
        )

    return None
