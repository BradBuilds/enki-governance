"""
Cross-Signal Correlation — find all signals near a point in a time window.

Joins position reports, photos, calendar events, contacts, financial
transactions, and any other geolocated signal within a radius and time range.

Returns grouped by signal type so the UI can render a multi-signal timeline.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from storage.postgres.db import get_connection

router = APIRouter(tags=["correlation"])


@router.get("/correlate")
async def correlate_signals(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    lat: float = Query(...),
    lon: float = Query(...),
    radius_m: float = Query(500),
    after: str = Query(""),
    before: str = Query(""),
    device_id: str = Query(""),
    limit: int = Query(200, le=500),
):
    """Find all signals near a point within a time window, grouped by type."""

    # Parse time range (default: last 24 hours)
    now = datetime.now(timezone.utc)
    try:
        t_after = datetime.fromisoformat(after.replace("Z", "+00:00")) if after else now - timedelta(hours=24)
    except Exception:
        t_after = now - timedelta(hours=24)
    try:
        t_before = datetime.fromisoformat(before.replace("Z", "+00:00")) if before else now
    except Exception:
        t_before = now

    tenant_id = auth.tenant_id
    results = {
        "gps": [],
        "photos": [],
        "calendar": [],
        "contacts": [],
        "financial": [],
        "communications": [],
        "other": [],
    }
    total = 0

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Build spatial filter
                # Use simple bounding box approximation (1 degree ≈ 111km)
                radius_deg = radius_m / 111000.0
                lat_min = lat - radius_deg
                lat_max = lat + radius_deg
                lon_min = lon - radius_deg
                lon_max = lon + radius_deg

                device_clause = ""
                params: list = [tenant_id, t_after, t_before, lat_min, lat_max, lon_min, lon_max, limit]
                if device_id:
                    device_clause = "AND device_id = %s"
                    params = [tenant_id, t_after, t_before, lat_min, lat_max, lon_min, lon_max, device_id, limit]

                cur.execute(f"""
                    SELECT signal_id, signal_type, timestamp, latitude, longitude,
                           device_id, payload, severity, confidence
                    FROM signals
                    WHERE tenant_id = %s
                      AND timestamp BETWEEN %s AND %s
                      AND latitude BETWEEN %s AND %s
                      AND longitude BETWEEN %s AND %s
                      {device_clause}
                    ORDER BY timestamp ASC
                    LIMIT %s
                """, tuple(params))

                for row in cur.fetchall():
                    sig = {
                        "signal_id": row[0],
                        "signal_type": row[1],
                        "timestamp": row[2].isoformat() if row[2] else None,
                        "latitude": float(row[3]) if row[3] else None,
                        "longitude": float(row[4]) if row[4] else None,
                        "device_id": row[5],
                        "summary": _extract_summary(row[1], row[6]),
                        "severity": row[7],
                        "confidence": row[8],
                    }

                    # Categorize by signal type
                    st = (row[1] or "").lower()
                    if "position" in st or "location" in st or "geospatial" in st:
                        results["gps"].append(sig)
                    elif "photo" in st or "image" in st:
                        results["photos"].append(sig)
                    elif "calendar" in st or "event" in st or "appointment" in st:
                        results["calendar"].append(sig)
                    elif "contact" in st or "sms" in st or "call" in st:
                        results["contacts"].append(sig) if "contact" in st else results["communications"].append(sig)
                    elif "financial" in st or "transaction" in st:
                        results["financial"].append(sig)
                    else:
                        results["other"].append(sig)

                    total += 1

                # Also check for calendar events that don't have coordinates
                # but overlap in time (calendar events often don't have GPS)
                cur.execute(f"""
                    SELECT signal_id, signal_type, timestamp, latitude, longitude,
                           device_id, payload
                    FROM signals
                    WHERE tenant_id = %s
                      AND signal_type IN (
                          'signal.personal.calendar_event',
                          'signal.personal.calendar_bulk'
                      )
                      AND timestamp BETWEEN %s AND %s
                      AND (latitude IS NULL OR latitude = 0)
                      {device_clause}
                    ORDER BY timestamp ASC
                    LIMIT 20
                """, tuple(params[:3] + ([device_id] if device_id else []) + [20]))

                for row in cur.fetchall():
                    sig = {
                        "signal_id": row[0],
                        "signal_type": row[1],
                        "timestamp": row[2].isoformat() if row[2] else None,
                        "latitude": None,
                        "longitude": None,
                        "device_id": row[5],
                        "summary": _extract_summary(row[1], row[6]),
                        "note": "No GPS — time-correlated only",
                    }
                    results["calendar"].append(sig)
                    total += 1

    except Exception as e:
        return {"groups": results, "total": 0, "error": str(e),
                "center": {"lat": lat, "lon": lon}, "radius_m": radius_m}

    return {
        "groups": results,
        "total": total,
        "center": {"lat": lat, "lon": lon},
        "radius_m": radius_m,
        "time_range": {"after": t_after.isoformat(), "before": t_before.isoformat()},
    }


def _extract_summary(signal_type: str, payload) -> str:
    """Extract a human-readable summary from signal payload."""
    if not payload:
        return ""
    if isinstance(payload, str):
        import json
        try:
            payload = json.loads(payload)
        except Exception:
            return payload[:100]
    if not isinstance(payload, dict):
        return str(payload)[:100]

    st = (signal_type or "").lower()
    if "calendar" in st:
        return payload.get("title") or payload.get("summary") or payload.get("event_name") or ""
    if "photo" in st or "image" in st:
        return payload.get("filename") or payload.get("title") or "Photo"
    if "sms" in st or "message" in st:
        return payload.get("body", "")[:100] if payload.get("body") else payload.get("from", "")
    if "call" in st:
        return f"{payload.get('type', 'call')} — {payload.get('number', payload.get('from', ''))}"
    if "financial" in st or "transaction" in st:
        amt = payload.get("amount", "")
        merchant = payload.get("merchant_name") or payload.get("description", "")
        return f"{merchant} {amt}" if merchant else str(amt)
    if "contact" in st:
        return payload.get("name") or payload.get("display_name") or ""

    # Generic: first meaningful string field
    for key in ("title", "summary", "description", "name", "headline", "body"):
        val = payload.get(key)
        if val and isinstance(val, str):
            return val[:100]
    return ""
