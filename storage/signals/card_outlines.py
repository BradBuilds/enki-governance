"""
Card Outline Seeds — pre-defined card templates for every signal type.

These outlines define the expected payload fields, visibility, format,
and media blocks for every signal type in the vocabulary. They are
seeded into the card_templates table so that:

1. The template editor shows all types before data flows
2. The AI connect wizard can preview what cards will look like
3. First-ingestion templates are curated, not generic

Outlines are merged with auto-detected fields on first real payload.
The seed only runs if no template exists yet for a given type.
"""

from __future__ import annotations

import json
from storage.postgres.db import get_connection
from storage.signals.card_templates import (
    _type_to_icon,
    _type_to_display_name,
)


# ─── Column helpers ──────────────────────────────────────────────────────────

def _col(key, label, visible=True, fmt="text", media_type=None, trackable=False,
         is_array=False, table_columns=None):
    """Shorthand for a column definition."""
    c = {
        "key": key,
        "label": label,
        "visible": visible,
        "format": fmt,
        "media_type": media_type,
        "position": 0,  # set at build time
        "is_array": is_array,
        "trackable": trackable,
    }
    if table_columns:
        c["table_columns"] = table_columns
    return c


def _map_media(lat="latitude", lon="longitude", label="Location"):
    return {"type": "map", "lat_field": lat, "lon_field": lon, "label": label}


def _gauge(field, label, unit="%", max_val=100, min_val=0):
    return {
        "type": "gauge", "source_field": field, "label": label,
        "value": 0, "min": min_val, "max": max_val, "unit": unit,
    }


def _table(field, label, columns):
    return {"type": "table", "source_field": field, "label": label, "columns": columns}


def _email():
    return {"type": "email", "source_field": "email", "label": "Email"}


def _image(field="image_url", label="Image"):
    return {"type": "image", "source_field": field, "label": label}


# ─── Outline Definitions ────────────────────────────────────────────────────
# Each entry: signal_type → {"columns": [...], "media": [...]}
# Only expected/canonical fields are listed. Real payloads may have more.

OUTLINES: dict[str, dict] = {
    # ═══════════════════════════════════════════════════════════════════════
    # GEOSPATIAL
    # ═══════════════════════════════════════════════════════════════════════
    "signal.geospatial.position_report": {
        "columns": [
            _col("callsign", "Callsign"),
            _col("speed", "Speed", fmt="numeric", trackable=True),
            _col("bearing", "Bearing", fmt="numeric"),
            _col("accuracy", "Accuracy", fmt="numeric", media_type="gauge", trackable=True),
            _col("altitude", "Altitude", fmt="numeric", trackable=True),
            _col("battery", "Battery", fmt="numeric", media_type="gauge", trackable=True),
            _col("provider", "Provider", visible=False),
        ],
        "media": [_map_media("latitude", "longitude")],
    },
    "signal.geospatial.friendly_ground": {
        "columns": [
            _col("callsign", "Callsign"),
            _col("type", "Unit Type"),
            _col("speed", "Speed", fmt="numeric", trackable=True),
            _col("bearing", "Course", fmt="numeric"),
            _col("altitude", "Altitude", fmt="numeric"),
        ],
        "media": [_map_media("latitude", "longitude")],
    },
    "signal.geospatial.friendly_air": {
        "columns": [
            _col("callsign", "Callsign"),
            _col("type", "Aircraft Type"),
            _col("speed", "Speed", fmt="numeric", trackable=True),
            _col("altitude", "Altitude", fmt="numeric", trackable=True),
        ],
        "media": [_map_media("latitude", "longitude")],
    },
    "signal.geospatial.hostile_ground": {
        "columns": [
            _col("callsign", "Callsign"),
            _col("type", "Unit Type"),
            _col("speed", "Speed", fmt="numeric"),
            _col("bearing", "Course", fmt="numeric"),
        ],
        "media": [_map_media("latitude", "longitude")],
    },
    "signal.geospatial.hostile_air": {
        "columns": [
            _col("callsign", "Callsign"),
            _col("type", "Aircraft Type"),
            _col("speed", "Speed", fmt="numeric"),
            _col("altitude", "Altitude", fmt="numeric"),
        ],
        "media": [_map_media("latitude", "longitude")],
    },
    "signal.geospatial.unknown_ground": {
        "columns": [
            _col("callsign", "Callsign"),
            _col("type", "Unit Type"),
            _col("speed", "Speed", fmt="numeric"),
        ],
        "media": [_map_media("latitude", "longitude")],
    },
    "signal.geospatial.neutral": {
        "columns": [
            _col("callsign", "Callsign"),
            _col("type", "Entity Type"),
            _col("speed", "Speed", fmt="numeric"),
        ],
        "media": [_map_media("latitude", "longitude")],
    },
    "signal.geospatial.sensor_point": {
        "columns": [
            _col("sensor_name", "Sensor"),
            _col("reading", "Reading", fmt="numeric", trackable=True),
            _col("unit", "Unit"),
            _col("status", "Status", fmt="badge"),
        ],
        "media": [_map_media("latitude", "longitude")],
    },
    "signal.geospatial.geofence_event": {
        "columns": [
            _col("geofence_name", "Geofence"),
            _col("event_type", "Event", fmt="badge"),
            _col("entity_name", "Entity"),
            _col("entity_id", "Entity ID", visible=False),
            _col("duration", "Dwell Time", fmt="numeric"),
            _col("speed", "Speed", fmt="numeric"),
        ],
        "media": [_map_media("latitude", "longitude")],
    },
    "signal.geospatial.route": {
        "columns": [
            _col("entity_name", "Entity"),
            _col("point_count", "Points", fmt="numeric"),
            _col("distance_km", "Distance (km)", fmt="numeric"),
            _col("duration_min", "Duration (min)", fmt="numeric"),
        ],
        "media": [_map_media("start_lat", "start_lon")],
    },
    "signal.geospatial.aircraft_position": {
        "columns": [
            _col("flight", "Flight"),
            _col("r", "Registration"),
            _col("t", "Aircraft Type"),
            _col("alt_baro", "Altitude (ft)", fmt="numeric", trackable=True),
            _col("gs", "Ground Speed (kt)", fmt="numeric", trackable=True),
            _col("track", "Track", fmt="numeric"),
            _col("squawk", "Squawk"),
            _col("category", "Category", visible=False),
        ],
        "media": [_map_media("lat", "lon")],
    },
    "signal.geospatial.vessel_position": {
        "columns": [
            _col("name", "Vessel Name"),
            _col("mmsi", "MMSI"),
            _col("callsign", "Callsign"),
            _col("ship_type", "Ship Type"),
            _col("sog", "Speed (kt)", fmt="numeric", trackable=True),
            _col("cog", "Course", fmt="numeric"),
            _col("heading", "Heading", fmt="numeric"),
            _col("destination", "Destination"),
            _col("draught", "Draught", fmt="numeric", visible=False),
        ],
        "media": [_map_media("lat", "lon")],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # COMMUNICATIONS
    # ═══════════════════════════════════════════════════════════════════════
    "signal.communications.email": {
        "columns": [
            _col("from", "From", fmt="email"),
            _col("to", "To"),
            _col("subject", "Subject"),
            _col("body_preview", "Preview"),
            _col("attachment_count", "Attachments", fmt="numeric"),
            _col("has_attachments", "Has Attachments", fmt="badge", visible=False),
            _col("date", "Date", visible=False),
        ],
        "media": [_email()],
    },
    "signal.communications.sms": {
        "columns": [
            _col("from", "From"),
            _col("to", "To"),
            _col("body", "Message"),
            _col("direction", "Direction", fmt="badge"),
            _col("status", "Status", visible=False),
        ],
        "media": [],
    },
    "signal.communications.chat_message": {
        "columns": [
            _col("from", "From"),
            _col("channel", "Channel"),
            _col("platform", "Platform"),
            _col("body", "Message"),
            _col("attachments", "Attachments", visible=False, fmt="table",
                 is_array=True, media_type="table", table_columns=["name", "type"]),
        ],
        "media": [],
    },
    "signal.communications.radio_transmission": {
        "columns": [
            _col("callsign", "Callsign"),
            _col("frequency", "Frequency"),
            _col("mode", "Mode"),
            _col("message", "Message"),
            _col("signal_strength", "Signal", fmt="numeric", media_type="gauge",
                 trackable=True),
        ],
        "media": [],
    },
    "signal.communications.webhook_message": {
        "columns": [
            _col("source", "Source"),
            _col("event_type", "Event"),
            _col("summary", "Summary"),
        ],
        "media": [],
    },
    "signal.communications.voicemail": {
        "columns": [
            _col("caller", "Caller"),
            _col("duration", "Duration", fmt="numeric"),
            _col("transcript", "Transcript"),
            _col("read", "Read", fmt="badge", visible=False),
        ],
        "media": [],
    },
    "signal.communications.notification": {
        "columns": [
            _col("app", "App"),
            _col("title", "Title"),
            _col("text", "Text"),
            _col("package", "Package", visible=False),
            _col("category", "Category", visible=False),
        ],
        "media": [],
    },
    "signal.communications.call_log": {
        "columns": [
            _col("number", "Number"),
            _col("name", "Contact"),
            _col("type", "Type", fmt="badge"),
            _col("duration", "Duration (s)", fmt="numeric"),
            _col("direction", "Direction"),
            _col("date", "Date", visible=False),
        ],
        "media": [],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # SECURITY
    # ═══════════════════════════════════════════════════════════════════════
    "signal.security.authentication_failure": {
        "columns": [
            _col("username", "Username"),
            _col("src_ip", "Source IP", fmt="ip"),
            _col("action", "Action"),
            _col("result", "Result", fmt="badge"),
            _col("method", "Method", visible=False),
            _col("application", "Application", visible=False),
            _col("geo", "Location", visible=False),
        ],
        "media": [],
    },
    "signal.security.network_anomaly": {
        "columns": [
            _col("anomaly_type", "Anomaly Type"),
            _col("src_ip", "Source IP", fmt="ip"),
            _col("dst_ip", "Dest IP", fmt="ip"),
            _col("port", "Port", fmt="numeric"),
            _col("protocol", "Protocol"),
            _col("bytes", "Bytes", fmt="numeric", visible=False),
        ],
        "media": [],
    },
    "signal.security.malware_detected": {
        "columns": [
            _col("malware_name", "Malware"),
            _col("file_path", "File Path"),
            _col("severity", "Severity", fmt="badge"),
            _col("action", "Action", fmt="badge"),
            _col("scanner", "Scanner", visible=False),
            _col("quarantined", "Quarantined", fmt="badge", visible=False),
        ],
        "media": [],
    },
    "signal.security.indicator_match": {
        "columns": [
            _col("indicator", "Indicator"),
            _col("indicator_type", "Type"),
            _col("threat_type", "Threat"),
            _col("confidence", "Confidence", fmt="numeric", media_type="gauge"),
            _col("source", "Source"),
        ],
        "media": [],
    },
    "signal.security.intrusion_detected": {
        "columns": [
            _col("rule_name", "Rule"),
            _col("src_ip", "Source IP", fmt="ip"),
            _col("dst_ip", "Dest IP", fmt="ip"),
            _col("severity", "Severity", fmt="badge"),
            _col("action", "Action", fmt="badge"),
            _col("protocol", "Protocol", visible=False),
        ],
        "media": [],
    },
    "signal.security.policy_violation": {
        "columns": [
            _col("policy_name", "Policy"),
            _col("user", "User"),
            _col("action", "Action"),
            _col("resource", "Resource"),
            _col("severity", "Severity", fmt="badge"),
        ],
        "media": [],
    },
    "signal.security.unauthorized_access": {
        "columns": [
            _col("user", "User"),
            _col("resource", "Resource"),
            _col("action", "Action"),
            _col("src_ip", "Source IP", fmt="ip"),
            _col("method", "Method", visible=False),
        ],
        "media": [],
    },
    "signal.security.threat_feed": {
        "columns": [
            _col("indicator", "Indicator"),
            _col("indicator_type", "Type"),
            _col("threat_type", "Threat Type"),
            _col("confidence", "Confidence", fmt="numeric", media_type="gauge"),
            _col("source", "Source"),
            _col("tags", "Tags", visible=False),
        ],
        "media": [],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # PERSONAL
    # ═══════════════════════════════════════════════════════════════════════
    "signal.personal.location": {
        "columns": [
            _col("accuracy", "Accuracy (m)", fmt="numeric"),
            _col("speed", "Speed", fmt="numeric", trackable=True),
            _col("altitude", "Altitude", fmt="numeric"),
            _col("battery", "Battery", fmt="numeric", media_type="gauge", trackable=True),
            _col("provider", "Provider", visible=False),
        ],
        "media": [_map_media("latitude", "longitude")],
    },
    "signal.personal.message": {
        "columns": [
            _col("from", "From"),
            _col("to", "To"),
            _col("body", "Message"),
        ],
        "media": [],
    },
    "signal.personal.proximity": {
        "columns": [
            _col("detected_device", "Detected Device"),
            _col("rssi", "Signal Strength", fmt="numeric", media_type="gauge", trackable=True),
            _col("distance", "Distance (m)", fmt="numeric"),
            _col("method", "Method"),
        ],
        "media": [],
    },
    "signal.personal.sos": {
        "columns": [
            _col("message", "Message"),
            _col("battery", "Battery", fmt="numeric", media_type="gauge"),
            _col("accuracy", "Accuracy (m)", fmt="numeric"),
        ],
        "media": [_map_media("latitude", "longitude")],
    },
    "signal.personal.call_started": {
        "columns": [
            _col("number", "Number"),
            _col("contact_name", "Contact"),
            _col("direction", "Direction", fmt="badge"),
        ],
        "media": [],
    },
    "signal.personal.call_ended": {
        "columns": [
            _col("number", "Number"),
            _col("contact_name", "Contact"),
            _col("duration", "Duration (s)", fmt="numeric"),
            _col("direction", "Direction", fmt="badge"),
        ],
        "media": [],
    },
    "signal.personal.check_in": {
        "columns": [
            _col("message", "Message"),
            _col("status", "Status", fmt="badge"),
            _col("battery", "Battery", fmt="numeric", media_type="gauge"),
        ],
        "media": [_map_media("latitude", "longitude")],
    },
    "signal.personal.contacts": {
        "columns": [
            _col("contact_count", "Total Contacts", fmt="numeric"),
            _col("contacts", "Contacts", fmt="table", is_array=True, media_type="table",
                 table_columns=["name", "phone", "email"]),
        ],
        "media": [],
    },
    "signal.personal.calendar_event": {
        "columns": [
            _col("title", "Event"),
            _col("start", "Start"),
            _col("end", "End"),
            _col("location", "Location"),
            _col("organizer", "Organizer", visible=False),
            _col("description", "Description", visible=False),
        ],
        "media": [],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # FINANCIAL
    # ═══════════════════════════════════════════════════════════════════════
    "signal.financial.transaction": {
        "columns": [
            _col("amount", "Amount", fmt="numeric"),
            _col("currency", "Currency"),
            _col("from", "From"),
            _col("to", "To"),
            _col("type", "Type"),
            _col("status", "Status", fmt="badge"),
            _col("reference", "Reference", visible=False),
        ],
        "media": [],
    },
    "signal.financial.invoice": {
        "columns": [
            _col("total", "Total", fmt="numeric"),
            _col("currency", "Currency"),
            _col("vendor", "Vendor"),
            _col("due_date", "Due Date"),
            _col("status", "Status", fmt="badge"),
            _col("line_items", "Items", fmt="table", is_array=True, media_type="table",
                 table_columns=["description", "quantity", "unit_price"], visible=False),
        ],
        "media": [],
    },
    "signal.financial.payment": {
        "columns": [
            _col("amount", "Amount", fmt="numeric"),
            _col("currency", "Currency"),
            _col("from", "From"),
            _col("to", "To"),
            _col("method", "Method"),
            _col("status", "Status", fmt="badge"),
        ],
        "media": [],
    },
    "signal.financial.expense": {
        "columns": [
            _col("amount", "Amount", fmt="numeric"),
            _col("currency", "Currency"),
            _col("category", "Category"),
            _col("vendor", "Vendor"),
            _col("description", "Description"),
            _col("reimbursable", "Reimbursable", fmt="badge", visible=False),
        ],
        "media": [],
    },
    "signal.financial.balance_update": {
        "columns": [
            _col("ticker", "Ticker"),
            _col("name", "Name"),
            _col("price", "Price", fmt="numeric", trackable=True),
            _col("change_percent", "Change %", fmt="numeric", trackable=True),
            _col("market_cap", "Market Cap", fmt="numeric", visible=False),
            _col("volume", "Volume", fmt="numeric", visible=False),
        ],
        "media": [],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # ENVIRONMENTAL
    # ═══════════════════════════════════════════════════════════════════════
    "signal.environmental.weather_reading": {
        "columns": [
            _col("temperature", "Temperature", fmt="numeric", media_type="gauge", trackable=True),
            _col("humidity", "Humidity", fmt="numeric", media_type="gauge", trackable=True),
            _col("pressure", "Pressure", fmt="numeric", trackable=True),
            _col("wind_speed", "Wind Speed", fmt="numeric", trackable=True),
            _col("wind_direction", "Wind Dir", fmt="numeric"),
            _col("conditions", "Conditions"),
        ],
        "media": [_gauge("temperature", "Temperature", "°F", 120),
                  _gauge("humidity", "Humidity", "%", 100)],
    },
    "signal.environmental.weather": {
        "columns": [
            _col("temperature", "Temperature", fmt="numeric", media_type="gauge", trackable=True),
            _col("humidity", "Humidity", fmt="numeric", media_type="gauge", trackable=True),
            _col("wind_speed", "Wind Speed", fmt="numeric", trackable=True),
            _col("conditions", "Conditions"),
            _col("forecast", "Forecast"),
        ],
        "media": [_gauge("temperature", "Temperature", "°F", 120)],
    },
    "signal.environmental.weather_alert": {
        "columns": [
            _col("event", "Event"),
            _col("severity", "Severity", fmt="badge"),
            _col("headline", "Headline"),
            _col("description", "Description", visible=False),
            _col("urgency", "Urgency", fmt="badge"),
            _col("certainty", "Certainty", visible=False),
            _col("effective", "Effective", visible=False),
            _col("expires", "Expires"),
        ],
        "media": [],
    },
    "signal.environmental.air_quality": {
        "columns": [
            _col("aqi", "AQI", fmt="numeric", media_type="gauge", trackable=True),
            _col("pm25", "PM2.5", fmt="numeric", media_type="gauge", trackable=True),
            _col("pm10", "PM10", fmt="numeric", trackable=True),
            _col("ozone", "Ozone", fmt="numeric", trackable=True),
            _col("no2", "NO₂", fmt="numeric", trackable=True),
            _col("station", "Station", visible=False),
        ],
        "media": [_gauge("aqi", "Air Quality Index", "", 500, 0)],
    },
    "signal.environmental.temperature": {
        "columns": [
            _col("temperature", "Temperature", fmt="numeric", media_type="gauge", trackable=True),
            _col("feels_like", "Feels Like", fmt="numeric", trackable=True),
            _col("humidity", "Humidity", fmt="numeric", media_type="gauge", trackable=True),
            _col("pressure", "Pressure", fmt="numeric"),
            _col("wind_speed", "Wind Speed", fmt="numeric"),
            _col("description", "Conditions"),
        ],
        "media": [_gauge("temperature", "Temperature", "°F", 120)],
    },
    "signal.environmental.seismic": {
        "columns": [
            _col("mag", "Magnitude", fmt="numeric", trackable=True),
            _col("place", "Location"),
            _col("depth", "Depth (km)", fmt="numeric"),
            _col("time", "Time"),
            _col("tsunami", "Tsunami Warning", fmt="badge"),
            _col("felt", "Felt Reports", fmt="numeric", visible=False),
        ],
        "media": [_map_media("latitude", "longitude")],
    },
    "signal.environmental.water_level": {
        "columns": [
            _col("v", "Water Level (ft)", fmt="numeric", trackable=True),
            _col("t", "Time"),
            _col("q", "Quality"),
            _col("station", "Station", visible=False),
        ],
        "media": [_gauge("v", "Water Level", "ft", 20, -5)],
    },
    "signal.environmental.aviation_weather": {
        "columns": [
            _col("icaoId", "Station"),
            _col("name", "Airport"),
            _col("rawOb", "Raw Observation"),
            _col("temp", "Temperature (°C)", fmt="numeric", media_type="gauge", trackable=True),
            _col("dewp", "Dewpoint (°C)", fmt="numeric", trackable=True),
            _col("wdir", "Wind Dir (°)", fmt="numeric"),
            _col("wspd", "Wind Speed (kt)", fmt="numeric", trackable=True),
            _col("wgst", "Wind Gust (kt)", fmt="numeric", visible=False),
            _col("visib", "Visibility (sm)", fmt="numeric"),
            _col("fltcat", "Flight Category", fmt="badge"),
            _col("altim", "Altimeter", fmt="numeric", visible=False),
            _col("ceil", "Ceiling (ft)", fmt="numeric", visible=False),
            _col("clouds", "Clouds", visible=False, fmt="table", is_array=True,
                 media_type="table", table_columns=["cover", "base"]),
        ],
        "media": [_map_media("lat", "lon"),
                  _gauge("temp", "Temperature", "°C", 50, -40)],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # CONTENT
    # ═══════════════════════════════════════════════════════════════════════
    "signal.content.document": {
        "columns": [
            _col("title", "Title"),
            _col("filename", "Filename"),
            _col("page_count", "Pages", fmt="numeric"),
            _col("author", "Author"),
            _col("text_preview", "Preview", visible=False),
        ],
        "media": [],
    },
    "signal.content.image": {
        "columns": [
            _col("filename", "Filename"),
            _col("width", "Width", fmt="numeric"),
            _col("height", "Height", fmt="numeric"),
            _col("format", "Format"),
            _col("caption", "Caption"),
            _col("size_bytes", "Size", fmt="numeric", visible=False),
        ],
        "media": [_image("image_url", "Image")],
    },
    "signal.content.video": {
        "columns": [
            _col("filename", "Filename"),
            _col("duration", "Duration (s)", fmt="numeric"),
            _col("width", "Width", fmt="numeric"),
            _col("height", "Height", fmt="numeric"),
            _col("codec", "Codec", visible=False),
        ],
        "media": [],
    },
    "signal.content.audio": {
        "columns": [
            _col("filename", "Filename"),
            _col("duration", "Duration (s)", fmt="numeric"),
            _col("format", "Format"),
            _col("transcript", "Transcript"),
            _col("sample_rate", "Sample Rate", fmt="numeric", visible=False),
        ],
        "media": [],
    },
    "signal.content.social_post": {
        "columns": [
            _col("author", "Author"),
            _col("title", "Title"),
            _col("body", "Body"),
            _col("url", "URL", fmt="url"),
            _col("score", "Score", fmt="numeric"),
            _col("subreddit", "Subreddit", visible=False),
        ],
        "media": [],
    },
    "signal.content.news_article": {
        "columns": [
            _col("title", "Title"),
            _col("source", "Source"),
            _col("url", "URL", fmt="url"),
            _col("author", "Author"),
            _col("published", "Published"),
            _col("summary", "Summary", visible=False),
        ],
        "media": [],
    },
    "signal.content.news": {
        "columns": [
            _col("title", "Title"),
            _col("url", "URL", fmt="url"),
            _col("source", "Source"),
            _col("domain", "Domain"),
            _col("language", "Language", visible=False),
            _col("seendate", "Seen", visible=False),
        ],
        "media": [],
    },
    "signal.content.rss_entry": {
        "columns": [
            _col("title", "Title"),
            _col("link", "Link", fmt="url"),
            _col("author", "Author"),
            _col("summary", "Summary"),
            _col("published", "Published"),
        ],
        "media": [],
    },
    "signal.content.web_page": {
        "columns": [
            _col("url", "URL", fmt="url"),
            _col("title", "Title"),
            _col("text_preview", "Preview"),
            _col("status_code", "Status", fmt="numeric", visible=False),
        ],
        "media": [],
    },
    "signal.content.screenshot": {
        "columns": [
            _col("filename", "Filename"),
            _col("width", "Width", fmt="numeric"),
            _col("height", "Height", fmt="numeric"),
            _col("source_app", "Source App"),
        ],
        "media": [_image("image_url", "Screenshot")],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # BUSINESS
    # ═══════════════════════════════════════════════════════════════════════
    "signal.business.crm_record": {
        "columns": [
            _col("name", "Name"),
            _col("company", "Company"),
            _col("email", "Email", fmt="email"),
            _col("phone", "Phone"),
            _col("stage", "Stage", fmt="badge"),
            _col("value", "Value", fmt="numeric", visible=False),
        ],
        "media": [],
    },
    "signal.business.support_ticket": {
        "columns": [
            _col("title", "Title"),
            _col("status", "Status", fmt="badge"),
            _col("priority", "Priority", fmt="badge"),
            _col("assignee", "Assignee"),
            _col("requester", "Requester"),
            _col("category", "Category", visible=False),
        ],
        "media": [],
    },
    "signal.business.inventory_update": {
        "columns": [
            _col("item_name", "Item"),
            _col("sku", "SKU"),
            _col("quantity", "Quantity", fmt="numeric"),
            _col("location", "Location"),
            _col("change_type", "Change", fmt="badge"),
        ],
        "media": [],
    },
    "signal.business.order": {
        "columns": [
            _col("total", "Total", fmt="numeric"),
            _col("currency", "Currency"),
            _col("status", "Status", fmt="badge"),
            _col("customer", "Customer"),
            _col("items", "Items", fmt="table", is_array=True, media_type="table",
                 table_columns=["name", "quantity", "price"]),
        ],
        "media": [],
    },
    "signal.business.shipment": {
        "columns": [
            _col("tracking_number", "Tracking"),
            _col("carrier", "Carrier"),
            _col("status", "Status", fmt="badge"),
            _col("eta", "ETA"),
            _col("origin", "Origin", visible=False),
            _col("destination", "Destination"),
        ],
        "media": [],
    },
    "signal.business.calendar_event": {
        "columns": [
            _col("title", "Event"),
            _col("start", "Start"),
            _col("end", "End"),
            _col("location", "Location"),
            _col("organizer", "Organizer"),
            _col("attendees", "Attendees", visible=False),
        ],
        "media": [],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # INFRASTRUCTURE
    # ═══════════════════════════════════════════════════════════════════════
    "signal.infrastructure.health_degraded": {
        "columns": [
            _col("service", "Service"),
            _col("status", "Status", fmt="badge"),
            _col("metric", "Metric"),
            _col("value", "Value", fmt="numeric", trackable=True),
            _col("threshold", "Threshold", fmt="numeric"),
            _col("host", "Host", visible=False),
        ],
        "media": [],
    },
    "signal.infrastructure.threshold_exceeded": {
        "columns": [
            _col("metric", "Metric"),
            _col("value", "Value", fmt="numeric", trackable=True),
            _col("threshold", "Threshold", fmt="numeric"),
            _col("host", "Host"),
            _col("service", "Service"),
            _col("severity", "Severity", fmt="badge"),
        ],
        "media": [_gauge("value", "Value", "", 100)],
    },
    "signal.infrastructure.sensor_reading": {
        "columns": [
            _col("sensor_name", "Sensor"),
            _col("value", "Reading", fmt="numeric", media_type="gauge", trackable=True),
            _col("unit", "Unit"),
            _col("status", "Status", fmt="badge"),
            _col("battery", "Battery", fmt="numeric", visible=False, trackable=True),
        ],
        "media": [_gauge("value", "Reading", "", 100)],
    },
    "signal.infrastructure.camera_capture": {
        "columns": [
            _col("camera_id", "Camera"),
            _col("filename", "File"),
            _col("resolution", "Resolution"),
            _col("motion_detected", "Motion", fmt="badge"),
        ],
        "media": [_image("image_url", "Capture")],
    },
    "signal.infrastructure.alpr_reading": {
        "columns": [
            _col("plate_number", "Plate"),
            _col("confidence", "Confidence", fmt="numeric", media_type="gauge"),
            _col("camera_id", "Camera"),
            _col("vehicle_description", "Vehicle"),
            _col("color", "Color", visible=False),
            _col("make", "Make", visible=False),
            _col("model", "Model", visible=False),
            _col("direction", "Direction", visible=False),
        ],
        "media": [_image("image_url", "Plate Image"),
                  _gauge("confidence", "Confidence", "%", 100)],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # INTELLIGENCE
    # ═══════════════════════════════════════════════════════════════════════
    "signal.intelligence.imagery": {
        "columns": [
            _col("title", "Title"),
            _col("source", "Source"),
            _col("classification", "Classification"),
            _col("description", "Description"),
        ],
        "media": [_image("image_url", "Imagery"),
                  _map_media("latitude", "longitude")],
    },
    "signal.intelligence.sigint": {
        "columns": [
            _col("frequency", "Frequency"),
            _col("modulation", "Modulation"),
            _col("emitter_type", "Emitter Type"),
            _col("signal_strength", "Strength", fmt="numeric", media_type="gauge",
                 trackable=True),
            _col("bearing", "Bearing", fmt="numeric"),
            _col("bandwidth", "Bandwidth", visible=False),
        ],
        "media": [_map_media("latitude", "longitude")],
    },
    "signal.intelligence.humint": {
        "columns": [
            _col("source_reliability", "Source Reliability", fmt="badge"),
            _col("information_credibility", "Credibility", fmt="badge"),
            _col("summary", "Summary"),
            _col("subjects", "Subjects"),
            _col("location", "Location"),
        ],
        "media": [],
    },
    "signal.intelligence.osint": {
        "columns": [
            _col("title", "Title"),
            _col("source", "Source"),
            _col("summary", "Summary"),
            _col("confidence", "Confidence", fmt="numeric", media_type="gauge"),
            _col("indicators", "Indicators", visible=False),
        ],
        "media": [],
    },
    "signal.intelligence.threat_feed": {
        "columns": [
            _col("indicator", "Indicator"),
            _col("indicator_type", "Type"),
            _col("threat_type", "Threat"),
            _col("confidence", "Confidence", fmt="numeric", media_type="gauge"),
            _col("source", "Source"),
        ],
        "media": [],
    },
    "signal.intelligence.research_finding": {
        "columns": [
            _col("title", "Title"),
            _col("summary", "Summary"),
            _col("author", "Author"),
            _col("tags", "Tags"),
            _col("references", "References", visible=False),
        ],
        "media": [],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # MEDICAL
    # ═══════════════════════════════════════════════════════════════════════
    "signal.medical.vital_alert": {
        "columns": [
            _col("heart_rate", "Heart Rate", fmt="numeric", media_type="gauge", trackable=True),
            _col("blood_pressure", "Blood Pressure"),
            _col("spo2", "SpO₂", fmt="numeric", media_type="gauge", trackable=True),
            _col("temperature", "Temp", fmt="numeric", media_type="gauge", trackable=True),
            _col("respiratory_rate", "Resp Rate", fmt="numeric", trackable=True),
            _col("alert_type", "Alert", fmt="badge", visible=False),
        ],
        "media": [_gauge("heart_rate", "Heart Rate", "bpm", 200),
                  _gauge("spo2", "SpO₂", "%", 100, 80)],
    },
    "signal.medical.lab_result": {
        "columns": [
            _col("test_name", "Test"),
            _col("result", "Result"),
            _col("unit", "Unit"),
            _col("reference_range", "Reference"),
            _col("status", "Status", fmt="badge"),
        ],
        "media": [],
    },
    "signal.medical.medication_event": {
        "columns": [
            _col("medication", "Medication"),
            _col("dose", "Dose"),
            _col("route", "Route"),
            _col("administered_by", "Administered By"),
            _col("time", "Time"),
        ],
        "media": [],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # OPERATIONS
    # ═══════════════════════════════════════════════════════════════════════
    "signal.operations.tasking": {
        "columns": [
            _col("title", "Task"),
            _col("assignee", "Assignee"),
            _col("priority", "Priority", fmt="badge"),
            _col("status", "Status", fmt="badge"),
            _col("due_date", "Due"),
        ],
        "media": [],
    },
    "signal.operations.reply": {
        "columns": [
            _col("task_title", "Task"),
            _col("responder", "Responder"),
            _col("status", "Status", fmt="badge"),
            _col("message", "Message"),
        ],
        "media": [],
    },
    "signal.operations.mission_update": {
        "columns": [
            _col("mission_name", "Mission"),
            _col("status", "Status", fmt="badge"),
            _col("objective", "Objective"),
            _col("commander", "Commander"),
            _col("phase", "Phase", visible=False),
        ],
        "media": [_map_media("latitude", "longitude")],
    },
    "signal.operations.resource_request": {
        "columns": [
            _col("resource_type", "Resource"),
            _col("quantity", "Quantity", fmt="numeric"),
            _col("priority", "Priority", fmt="badge"),
            _col("requestor", "Requestor"),
            _col("needed_by", "Needed By"),
            _col("location", "Location", visible=False),
        ],
        "media": [],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # VEHICLE
    # ═══════════════════════════════════════════════════════════════════════
    "signal.vehicle.location": {
        "columns": [
            _col("device_name", "Vehicle"),
            _col("speed", "Speed", fmt="numeric", trackable=True),
            _col("heading", "Heading", fmt="numeric"),
            _col("battery", "Battery", fmt="numeric", media_type="gauge", trackable=True),
            _col("ignition", "Ignition", fmt="badge"),
            _col("motion", "Motion", fmt="badge"),
        ],
        "media": [_map_media("latitude", "longitude")],
    },
    "signal.vehicle.status": {
        "columns": [
            _col("ignition", "Ignition", fmt="badge"),
            _col("speed", "Speed", fmt="numeric", trackable=True),
            _col("fuel_level", "Fuel", fmt="numeric", media_type="gauge", trackable=True),
            _col("odometer", "Odometer", fmt="numeric"),
            _col("battery_voltage", "Battery (V)", fmt="numeric", trackable=True),
            _col("engine_temp", "Engine Temp", fmt="numeric", media_type="gauge",
                 visible=False, trackable=True),
        ],
        "media": [_gauge("fuel_level", "Fuel Level", "%", 100)],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # ASSET
    # ═══════════════════════════════════════════════════════════════════════
    "signal.asset.location": {
        "columns": [
            _col("asset_name", "Asset"),
            _col("asset_type", "Type"),
            _col("battery", "Battery", fmt="numeric", media_type="gauge", trackable=True),
            _col("accuracy", "Accuracy (m)", fmt="numeric"),
            _col("zone", "Zone", visible=False),
        ],
        "media": [_map_media("latitude", "longitude")],
    },
    "signal.asset.status": {
        "columns": [
            _col("asset_name", "Asset"),
            _col("status", "Status", fmt="badge"),
            _col("condition", "Condition", fmt="badge"),
            _col("location", "Location"),
            _col("assigned_to", "Assigned To"),
            _col("last_inspection", "Last Inspection", visible=False),
        ],
        "media": [],
    },
    "signal.asset.maintenance": {
        "columns": [
            _col("asset_name", "Asset"),
            _col("maintenance_type", "Type"),
            _col("status", "Status", fmt="badge"),
            _col("technician", "Technician"),
            _col("next_due", "Next Due"),
            _col("notes", "Notes", visible=False),
        ],
        "media": [],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # INTEREST (Subscriptions)
    # ═══════════════════════════════════════════════════════════════════════
    "signal.interest.image": {
        "columns": [
            _col("title", "Title"),
            _col("source", "Source"),
            _col("url", "URL", fmt="url"),
            _col("description", "Description"),
        ],
        "media": [_image("url", "Image")],
    },
    "signal.interest.article": {
        "columns": [
            _col("title", "Title"),
            _col("source", "Source"),
            _col("url", "URL", fmt="url"),
            _col("summary", "Summary"),
            _col("published", "Published"),
        ],
        "media": [],
    },
    "signal.interest.event": {
        "columns": [
            _col("title", "Event"),
            _col("start", "Start"),
            _col("end", "End"),
            _col("location", "Location"),
            _col("url", "URL", fmt="url", visible=False),
        ],
        "media": [],
    },
    "signal.interest.update": {
        "columns": [
            _col("title", "Title"),
            _col("source", "Source"),
            _col("summary", "Summary"),
            _col("url", "URL", fmt="url"),
        ],
        "media": [],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # CBRN
    # ═══════════════════════════════════════════════════════════════════════
    "signal.cbrn.chemical_detection": {
        "columns": [
            _col("agent", "Agent"),
            _col("concentration", "Concentration", fmt="numeric", trackable=True),
            _col("detector", "Detector"),
            _col("alarm_level", "Alarm", fmt="badge"),
            _col("wind_speed", "Wind", fmt="numeric", visible=False),
            _col("wind_direction", "Wind Dir", fmt="numeric", visible=False),
        ],
        "media": [_map_media("latitude", "longitude")],
    },
    "signal.cbrn.biological_detection": {
        "columns": [
            _col("agent", "Agent"),
            _col("concentration", "Concentration", fmt="numeric", trackable=True),
            _col("detector", "Detector"),
            _col("alarm_level", "Alarm", fmt="badge"),
        ],
        "media": [_map_media("latitude", "longitude")],
    },
    "signal.cbrn.radiological_detection": {
        "columns": [
            _col("dose_rate", "Dose Rate", fmt="numeric", media_type="gauge", trackable=True),
            _col("isotope", "Isotope"),
            _col("detector", "Detector"),
            _col("alarm_level", "Alarm", fmt="badge"),
            _col("cumulative_dose", "Cumulative", fmt="numeric", visible=False),
        ],
        "media": [_map_media("latitude", "longitude"),
                  _gauge("dose_rate", "Dose Rate", "µSv/h", 1000)],
    },
    "signal.cbrn.nuclear_detection": {
        "columns": [
            _col("event_type", "Event Type"),
            _col("magnitude", "Magnitude", fmt="numeric"),
            _col("depth", "Depth (km)", fmt="numeric"),
            _col("yield_kt", "Yield (kt)", fmt="numeric"),
        ],
        "media": [_map_media("latitude", "longitude")],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # DEVICE (key types — mobile agent + desktop agent)
    # ═══════════════════════════════════════════════════════════════════════
    "signal.device.heartbeat": {
        "columns": [
            _col("status", "Status", fmt="badge"),
            _col("battery", "Battery", fmt="numeric", media_type="gauge", trackable=True),
            _col("uptime", "Uptime", fmt="numeric"),
            _col("hostname", "Hostname", visible=False),
        ],
        "media": [_gauge("battery", "Battery", "%", 100)],
    },
    "signal.device.sensor_reading": {
        "columns": [
            _col("vitals.level", "Battery Level", fmt="numeric", media_type="gauge", trackable=True),
            _col("vitals.charging", "Charging", fmt="badge"),
            _col("hardware.model", "Model"),
            _col("vitals.cpu", "CPU %", fmt="numeric", media_type="gauge", trackable=True),
            _col("vitals.memory", "Memory %", fmt="numeric", media_type="gauge", trackable=True),
            _col("vitals.disk", "Disk %", fmt="numeric", media_type="gauge", visible=False, trackable=True),
        ],
        "media": [_gauge("vitals.level", "Battery", "%", 100),
                  _gauge("vitals.cpu", "CPU", "%", 100),
                  _gauge("vitals.memory", "Memory", "%", 100)],
    },
    "signal.device.network_scan": {
        "columns": [
            _col("wifi_networks", "WiFi Networks", fmt="table", is_array=True,
                 media_type="table", table_columns=["ssid", "rssi", "bssid", "frequency"]),
            _col("bluetooth_devices", "Bluetooth", fmt="table", is_array=True,
                 media_type="table", table_columns=["name", "address", "rssi"], visible=False),
            _col("carrier", "Carrier"),
            _col("network_type", "Network Type"),
        ],
        "media": [],
    },
    "signal.device.network_flow": {
        "columns": [
            _col("app", "App"),
            _col("dest_ip", "Dest IP", fmt="ip"),
            _col("domain", "Domain"),
            _col("port", "Port", fmt="numeric"),
            _col("proto", "Protocol"),
            _col("bytes", "Bytes", fmt="numeric", visible=False),
        ],
        "media": [],
    },
    "signal.device.app_usage": {
        "columns": [
            _col("app", "App"),
            _col("url", "URL", fmt="url"),
            _col("duration", "Duration (s)", fmt="numeric"),
            _col("activity", "Activity"),
            _col("package", "Package", visible=False),
        ],
        "media": [],
    },
    "signal.device.browser_history": {
        "columns": [
            _col("entry_count", "Entries", fmt="numeric"),
            _col("browser", "Browser"),
            _col("entries", "History", fmt="table", is_array=True, media_type="table",
                 table_columns=["url", "title", "visit_count", "last_visit"]),
        ],
        "media": [],
    },
    "signal.device.process_list": {
        "columns": [
            _col("process_count", "Processes", fmt="numeric"),
            _col("processes", "Processes", fmt="table", is_array=True, media_type="table",
                 table_columns=["name", "pid", "cpu", "memory"]),
        ],
        "media": [],
    },
    "signal.device.system_log": {
        "columns": [
            _col("entry_count", "Entries", fmt="numeric"),
            _col("level", "Level", fmt="badge"),
            _col("source", "Source"),
            _col("message", "Message"),
            _col("is_root", "Root", fmt="badge", visible=False),
        ],
        "media": [],
    },
    "signal.device.shell_command": {
        "columns": [
            _col("command", "Command"),
            _col("exit_code", "Exit Code", fmt="numeric"),
            _col("user", "User"),
            _col("output", "Output"),
            _col("cwd", "Directory", visible=False),
        ],
        "media": [],
    },
    "signal.device.git_commit": {
        "columns": [
            _col("message", "Message"),
            _col("repo", "Repository"),
            _col("branch", "Branch"),
            _col("author", "Author"),
            _col("files_changed", "Files", fmt="numeric"),
            _col("insertions", "Insertions", fmt="numeric", visible=False),
            _col("deletions", "Deletions", fmt="numeric", visible=False),
        ],
        "media": [],
    },
    "signal.device.installed_software": {
        "columns": [
            _col("software_count", "Total", fmt="numeric"),
            _col("programs", "Programs", fmt="table", is_array=True, media_type="table",
                 table_columns=["name", "version", "publisher", "install_date"]),
        ],
        "media": [],
    },
    "signal.device.file_index": {
        "columns": [
            _col("file_count", "Files", fmt="numeric"),
            _col("total_size", "Total Size", fmt="numeric"),
            _col("path", "Path"),
            _col("files", "Files", fmt="table", is_array=True, media_type="table",
                 table_columns=["name", "size", "type", "modified"], visible=False),
        ],
        "media": [],
    },
    "signal.device.dns_cache": {
        "columns": [
            _col("entry_count", "Entries", fmt="numeric"),
            _col("entries", "DNS Cache", fmt="table", is_array=True, media_type="table",
                 table_columns=["name", "address", "type", "ttl"]),
        ],
        "media": [],
    },
    "signal.device.service_list": {
        "columns": [
            _col("service_count", "Services", fmt="numeric"),
            _col("services", "Services", fmt="table", is_array=True, media_type="table",
                 table_columns=["name", "display_name", "status", "start_type"]),
        ],
        "media": [],
    },
    "signal.device.startup_items": {
        "columns": [
            _col("item_count", "Items", fmt="numeric"),
            _col("items", "Startup Items", fmt="table", is_array=True, media_type="table",
                 table_columns=["name", "command", "location", "enabled"]),
        ],
        "media": [],
    },
    "signal.device.scheduled_tasks": {
        "columns": [
            _col("task_count", "Tasks", fmt="numeric"),
            _col("tasks", "Scheduled Tasks", fmt="table", is_array=True, media_type="table",
                 table_columns=["name", "schedule", "status", "last_run"]),
        ],
        "media": [],
    },
    "signal.device.network_interfaces": {
        "columns": [
            _col("interface_count", "Interfaces", fmt="numeric"),
            _col("interfaces", "Interfaces", fmt="table", is_array=True, media_type="table",
                 table_columns=["name", "ip_address", "mac_address", "status"]),
        ],
        "media": [],
    },
    "signal.device.clipboard": {
        "columns": [
            _col("content_type", "Type"),
            _col("content_preview", "Preview"),
            _col("length", "Length", fmt="numeric"),
            _col("source_app", "Source App", visible=False),
        ],
        "media": [],
    },
    "signal.device.command_receipt": {
        "columns": [
            _col("command_type", "Command"),
            _col("status", "Status", fmt="badge"),
            _col("result", "Result"),
            _col("duration_ms", "Duration (ms)", fmt="numeric"),
        ],
        "media": [],
    },
    "signal.device.cell_environment": {
        "columns": [
            _col("carrier", "Carrier"),
            _col("network_type", "Network Type"),
            _col("cell_towers", "Cell Towers", fmt="table", is_array=True, media_type="table",
                 table_columns=["cell_id", "lac", "mcc", "mnc", "rssi"]),
        ],
        "media": [],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # CUSTOM
    # ═══════════════════════════════════════════════════════════════════════
    "signal.custom.untyped": {
        "columns": [
            _col("summary", "Summary"),
            _col("format_detected", "Format"),
            _col("detection_confidence", "Confidence", fmt="numeric", media_type="gauge"),
        ],
        "media": [],
    },
}

# For device types without specific outlines, use a minimal default
_SIMPLE_DEVICE_TYPES = [
    "signal.device.app_switch", "signal.device.app_focus",
    "signal.device.active_window", "signal.device.url_navigation",
    "signal.device.web_visit", "signal.device.file_operation",
    "signal.device.file_event", "signal.device.file_change",
    "signal.device.file_content", "signal.device.file_alert",
    "signal.device.process_launch", "signal.device.process_exec",
    "signal.device.process_start", "signal.device.process_exit",
    "signal.device.clipboard_event", "signal.device.usb_event",
    "signal.device.login_event", "signal.device.system_event",
    "signal.device.screen_capture", "signal.device.screenshot",
    "signal.device.input_activity", "signal.device.gap_detected",
    "signal.device.dns_query", "signal.device.network_connection",
    "signal.device.network_bandwidth", "signal.device.service_change",
]

for _st in _SIMPLE_DEVICE_TYPES:
    if _st not in OUTLINES:
        # Generic device outline: title, details, any numeric value
        OUTLINES[_st] = {
            "columns": [
                _col("name", "Name"),
                _col("details", "Details"),
                _col("status", "Status", fmt="badge"),
                _col("value", "Value", fmt="numeric", trackable=True, visible=False),
            ],
            "media": [],
        }


# ─── Seed Function ────────────────────────────────────────────────────────────

def seed_card_outlines(tenant_id: str = "default", dsn: str | None = None) -> dict:
    """
    Seed card templates for all signal types in the vocabulary.

    Only inserts where no template exists yet. Returns stats.
    """
    inserted = 0
    skipped = 0

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            for signal_type, outline in OUTLINES.items():
                # Check if template already exists
                cur.execute(
                    "SELECT 1 FROM card_templates WHERE signal_type = %s AND tenant_id = %s",
                    (signal_type, tenant_id),
                )
                if cur.fetchone():
                    skipped += 1
                    continue

                display_name = _type_to_display_name(signal_type)
                icon = _type_to_icon(signal_type)
                columns = outline["columns"]

                # Assign positions
                for i, col in enumerate(columns):
                    col["position"] = i

                media_config = outline.get("media", [])

                cur.execute("""
                    INSERT INTO card_templates
                        (signal_type, tenant_id, display_name, icon, columns,
                         media_config, field_count, sample_payload)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (signal_type, tenant_id) DO NOTHING
                """, (
                    signal_type, tenant_id, display_name, icon,
                    json.dumps(columns), json.dumps(media_config),
                    len(columns), json.dumps({}),
                ))
                inserted += 1

            conn.commit()

    return {"inserted": inserted, "skipped": skipped, "total": len(OUTLINES)}
