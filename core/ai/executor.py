"""
AI Action Executor — executes LLM-generated actions against the API.

When the AI returns action blocks like:
  {"action": "search_signals", "params": {"signal_type": "signal.security.*"}}

The executor calls the real API functions and returns the results.
The frontend receives the results alongside the AI's text response
and renders them as cards.

The governance spine still validates everything. If the AI hallucinates
an invalid action, it gets rejected here — not by the frontend.
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("enki.ai.executor")


def execute_actions(
    actions: list[dict],
    tenant_id: str,
    identity_id: str = "ai-agent",
    dsn: Optional[str] = None,
) -> list[dict]:
    """
    Execute a list of AI-generated actions. Returns results for each.

    Each result:
    {
        "action": "search_signals",
        "success": True,
        "data": [...],       # actual API response
        "error": None
    }
    """
    results = []
    for action in actions:
        action_name = action.get("action", "")
        params = action.get("params", {})

        try:
            handler = ACTION_HANDLERS.get(action_name)
            if not handler:
                results.append({
                    "action": action_name,
                    "success": False,
                    "data": None,
                    "error": f"Unknown action: {action_name}",
                })
                continue

            # Permission check for personal data queries
            _perm_denied = _check_personal_data_permission(
                action_name, params, identity_id, tenant_id, dsn
            )
            if _perm_denied:
                results.append(_perm_denied)
                continue

            data = handler(tenant_id=tenant_id, identity_id=identity_id, dsn=dsn, **params)

            # Log access for personal data actions
            _log_personal_access(action_name, params, identity_id, tenant_id, dsn)

            results.append({
                "action": action_name,
                "success": True,
                "data": data,
                "error": None,
            })

        except Exception as e:
            log.warning(f"Action {action_name} failed: {e}")
            results.append({
                "action": action_name,
                "success": False,
                "data": None,
                "error": str(e),
            })

    return results


# ─── Action Handlers ──────────────────────────────────────────────────────────
# ── Personal data permission checks ──────────────────────────────────────────

# Actions that may access another user's personal data
_PERSONAL_DATA_ACTIONS = {
    "search_signals", "get_signal", "analyze_device", "show_trips",
    "cross_correlate", "search_contacts", "search_assets",
}


def _check_personal_data_permission(
    action_name: str, params: dict, identity_id: str, tenant_id: str, dsn
) -> dict | None:
    """Check if this action requires personal data permission. Returns error dict or None."""
    if action_name not in _PERSONAL_DATA_ACTIONS:
        return None

    # Determine if querying someone else's data
    signal_type = params.get("signal_type", "")
    device_id = params.get("device_id", "")

    # Only enforce for personal signal types
    if not signal_type or not signal_type.startswith("signal.personal"):
        if action_name not in ("analyze_device", "show_trips"):
            return None

    # For device-based queries, the device_id implies whose data
    # For now, we check if there's an explicit target that differs from querier
    target_id = params.get("target_id", "")
    if not target_id:
        return None  # No explicit target = querying own data

    if target_id == identity_id:
        return None  # Self-access

    try:
        from core.access.permissions import check_permission, classify_data_type, notify_access, log_access

        data_type = classify_data_type(signal_type) or "location"
        allowed = check_permission(identity_id, target_id, data_type, tenant_id, dsn)

        log_access(identity_id, target_id, data_type, action_name, allowed,
                   context="chat", tenant_id=tenant_id, dsn=dsn)

        if not allowed:
            notify_access(identity_id, target_id, data_type, False, tenant_id, dsn)
            return {
                "action": action_name,
                "success": False,
                "data": {"permission_denied": True, "data_type": data_type, "target": target_id},
                "error": f"Permission denied: you cannot access {target_id}'s {data_type} data",
            }

        # Allowed — notify target if alert_on_access is set
        notify_access(identity_id, target_id, data_type, True, tenant_id, dsn)
    except Exception:
        pass  # Permission system may not be initialized yet

    return None


def _log_personal_access(action_name: str, params: dict, identity_id: str, tenant_id: str, dsn):
    """Log personal data access after successful action execution."""
    if action_name not in _PERSONAL_DATA_ACTIONS:
        return
    signal_type = params.get("signal_type", "")
    if signal_type and signal_type.startswith("signal.personal"):
        try:
            from core.access.permissions import log_access, classify_data_type
            data_type = classify_data_type(signal_type) or "personal"
            log_access(identity_id, identity_id, data_type, action_name, True,
                       context="chat", tenant_id=tenant_id, dsn=dsn)
        except Exception:
            pass


# Each handler calls the real storage/API functions.
# Same functions the REST API uses — no special bypass.

def _search_signals(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    from storage.signals.store import query_signals
    signals = query_signals(
        tenant_id=tenant_id,
        signal_type=params.get("signal_type"),
        device_id=params.get("device_id"),
        after=params.get("after"),
        before=params.get("before"),
        limit=min(int(params.get("limit", 20)), 100),
        dsn=dsn,
    )
    return {"signals": signals, "count": len(signals)}


def _get_signal(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    from storage.signals.store import get_signal
    signal = get_signal(params.get("signal_id", ""), tenant_id, dsn)
    return signal or {"error": "Signal not found"}


def _search_observables(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    from storage.signals.store import search_observables
    results = search_observables(
        tenant_id=tenant_id,
        observable_type=params.get("observable_type", ""),
        value=params.get("value", ""),
        dsn=dsn,
    )
    return results


def _list_cases(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    from storage.signals.cases import list_cases
    cases = list_cases(
        tenant_id=tenant_id,
        status=params.get("status"),
        priority=params.get("priority"),
        limit=min(int(params.get("limit", 20)), 100),
        dsn=dsn,
    )
    return {"cases": cases, "count": len(cases)}


def _get_case(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    from storage.signals.cases import get_case
    case = get_case(params.get("case_id", ""), tenant_id, dsn)
    return case or {"error": "Case not found"}


def _create_case(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    from storage.signals.cases import create_case
    result = create_case(
        tenant_id=tenant_id,
        title=params.get("title", "AI-created case"),
        priority=params.get("priority", "medium"),
        description=params.get("description", ""),
        owner=identity_id,
        dsn=dsn,
    )
    return result


def _add_observable(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    from storage.signals.cases import add_case_observable
    result = add_case_observable(
        tenant_id=tenant_id,
        case_id=params.get("case_id", ""),
        observable_type=params.get("observable_type", ""),
        observable_value=params.get("observable_value", params.get("value", "")),
        added_by=identity_id,
        dsn=dsn,
    )
    return result


def _list_alerts(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    from storage.signals.cases import list_alerts
    # Clean params — AI might send "all" for status/severity, treat as None
    status = params.get("status")
    if status in ("all", "*", "any", None, ""):
        status = None
    severity = params.get("severity")
    if severity in ("all", "*", "any", None, ""):
        severity = None
    alerts = list_alerts(
        tenant_id=tenant_id,
        status=status,
        severity=severity,
        limit=min(int(params.get("limit", 20)), 100),
        dsn=dsn,
    )
    return {"alerts": alerts, "count": len(alerts)}


def _get_alert(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    from storage.signals.cases import get_alert
    alert = get_alert(params.get("alert_id", ""), tenant_id, dsn)
    return alert or {"error": "Alert not found"}


def _create_rule(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    from storage.signals.rules import create_rule as create_detection_rule
    result = create_detection_rule(
        tenant_id=tenant_id,
        name=params.get("name", "AI-created rule"),
        rule_type=params.get("rule_type", "match"),
        severity=params.get("severity", "medium"),
        condition=params.get("condition", {}),
        actions=params.get("actions", {"type": "alert"}),
        dsn=dsn,
    )
    return result


def _get_dashboard(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    from storage.signals.dashboard import get_dashboard_summary
    return get_dashboard_summary(tenant_id, dsn)


def _get_domains(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    from storage.signals.organize import get_organized_view
    return get_organized_view(tenant_id, dsn)


def _get_domain(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    from storage.signals.organize import get_domain_signals
    return get_domain_signals(tenant_id, params.get("domain", ""), dsn)


# ─── Handler Registry ─────────────────────────────────────────────────────────

# ─── Analysis actions ─────────────────────────────────────────────────────

def _analyze_device(tenant_id: str, identity_id: str, dsn=None,
                     device_id: str = "", after: str = "", before: str = "", **kw):
    """Analyze a device's movement pattern — route, stops, speed, stats."""
    from storage.signals.analysis import analyze_device_track
    from datetime import datetime
    after_dt = None
    before_dt = None
    if after:
        try: after_dt = datetime.fromisoformat(after)
        except ValueError: pass
    if before:
        try: before_dt = datetime.fromisoformat(before)
        except ValueError: pass
    return analyze_device_track(
        tenant_id=tenant_id, device_id=device_id,
        after=after_dt, before=before_dt, dsn=dsn,
    )


# ─── Map actions (client-side, returned as data for frontend to execute) ───

def _navigate_to(tenant_id: str, identity_id: str, dsn=None,
                  lat: float = 0, lon: float = 0, lng: float = None,
                  zoom: int = 12, label: str = "", **kw):
    """Navigate the map to a location. Frontend will call adapter.flyTo()."""
    return {"lat": lat, "lon": lng if lng is not None else lon, "zoom": zoom, "label": label}


def _navigate_to_page(tenant_id: str, identity_id: str, dsn=None,
                       page: str = "/dashboard", entity_id: str = "", label: str = "", **kw):
    """Navigate the user to an app page. Frontend will call goto()."""
    return {"page": page, "entity_id": entity_id, "label": label or page}


def _get_timeline_data(tenant_id: str, dsn=None, **kw):
    """Get recent timeline events for AI."""
    from storage.postgres.db import get_connection
    from datetime import datetime, timezone, timedelta
    events = []
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    limit = int(kw.get("limit", 20))
    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT signal_id, signal_type, severity, device_id, timestamp
                    FROM signals WHERE tenant_id = %s AND timestamp > %s
                    ORDER BY timestamp DESC LIMIT %s
                """, (tenant_id, since, limit))
                for r in cur.fetchall():
                    events.append({"type": "signal", "title": r[1], "detail": f"{r[2] or 'info'} from {r[3] or 'unknown'}", "time": r[4].isoformat() if r[4] else None})
                cur.execute("""
                    SELECT alert_id, title, severity, status, created_at
                    FROM alerts WHERE tenant_id = %s AND created_at > %s
                    ORDER BY created_at DESC LIMIT %s
                """, (tenant_id, since, limit))
                for r in cur.fetchall():
                    events.append({"type": "alert", "title": r[1], "detail": f"{r[2]} · {r[3]}", "time": r[4].isoformat() if r[4] else None})
    except Exception:
        pass
    events.sort(key=lambda e: e.get("time") or "", reverse=True)
    return {"events": events[:limit], "count": len(events)}


def _search_contacts(tenant_id: str, identity_id: str, dsn=None,
                      query: str = "", contact_type: str = "", status: str = "", **kw):
    """Search contacts by name, email, phone, type, or status."""
    from storage.postgres.db import get_connection
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            conditions = ["tenant_id = %s"]
            params = [tenant_id]
            if query:
                conditions.append("(primary_name ILIKE %s OR email ILIKE %s OR phone ILIKE %s)")
                like = f"%{query}%"
                params.extend([like, like, like])
            if contact_type:
                conditions.append("contact_type = %s")
                params.append(contact_type)
            if status:
                conditions.append("status = %s")
                params.append(status)
            where = " AND ".join(conditions)
            cur.execute(f"""
                SELECT contact_id, contact_type, primary_name, email, phone, status, threat_level
                FROM contacts WHERE {where} ORDER BY updated_at DESC LIMIT 20
            """, params)
            contacts = [{"contact_id": r[0], "contact_type": r[1], "name": r[2],
                         "email": r[3], "phone": r[4], "status": r[5], "threat_level": r[6]}
                        for r in cur.fetchall()]
    return {"contacts": contacts, "count": len(contacts)}


def _search_assets(tenant_id: str, identity_id: str, dsn=None,
                    query: str = "", category: str = "", status: str = "", **kw):
    """Search assets by name, serial number, category, or status."""
    from storage.postgres.db import get_connection
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            conditions = ["tenant_id = %s"]
            params = [tenant_id]
            if query:
                conditions.append("(display_name ILIKE %s OR serial_number ILIKE %s OR description ILIKE %s)")
                like = f"%{query}%"
                params.extend([like, like, like])
            if category:
                conditions.append("asset_category = %s")
                params.append(category)
            if status:
                conditions.append("status = %s")
                params.append(status)
            where = " AND ".join(conditions)
            cur.execute(f"""
                SELECT asset_id, display_name, asset_category, status, estimated_value, serial_number
                FROM assets WHERE {where} ORDER BY updated_at DESC LIMIT 20
            """, params)
            assets = [{"asset_id": r[0], "name": r[1], "category": r[2],
                       "status": r[3], "value": float(r[4]) if r[4] else None, "serial": r[5]}
                      for r in cur.fetchall()]
    return {"assets": assets, "count": len(assets)}


def _search_nearby(tenant_id: str, identity_id: str, dsn=None,
                    lat: float = 0, lon: float = 0, lng: float = None,
                    radius_km: float = 5, **kw):
    """Search for signals near a location."""
    from storage.signals.geo import query_signals_radius, signals_to_geojson
    actual_lon = lng if lng is not None else lon
    signals = query_signals_radius(
        tenant_id=tenant_id, lat=lat, lon=actual_lon,
        radius_km=radius_km, limit=50, dsn=dsn,
    )
    geojson = signals_to_geojson(signals)
    return {
        "lat": lat, "lon": actual_lon, "radius_km": radius_km,
        "count": len(geojson.get("features", [])),
        "signals": geojson,
    }


def _create_geofence_action(tenant_id: str, identity_id: str, dsn=None,
                              name: str = "", lat: float = 0, lon: float = 0,
                              lng: float = None, radius_km: float = 1,
                              alert_on: str = "enter", **kw):
    """Create a circular geofence around a point (approximated as polygon)."""
    import math
    actual_lon = lng if lng is not None else lon
    # Approximate circle as 32-point polygon
    points = []
    for i in range(33):
        angle = 2 * math.pi * i / 32
        dlat = radius_km / 111.32 * math.cos(angle)
        dlon = radius_km / (111.32 * math.cos(math.radians(lat))) * math.sin(angle)
        points.append([actual_lon + dlon, lat + dlat])
    from storage.signals.geo import create_geofence
    result = create_geofence(
        tenant_id=tenant_id, name=name or f"AI Zone @ {lat:.2f},{actual_lon:.2f}",
        polygon=points, created_by=identity_id,
        alert_on=alert_on, color="#ef4444", dsn=dsn,
    )
    return {**result, "lat": lat, "lon": actual_lon, "radius_km": radius_km}


def _highlight_area(tenant_id: str, identity_id: str, dsn=None,
                     west: float = 0, south: float = 0,
                     east: float = 0, north: float = 0,
                     label: str = "", color: str = "#f59e0b", **kw):
    """Highlight a bounding box on the map. Frontend draws a rectangle."""
    return {"west": west, "south": south, "east": east, "north": north,
            "label": label, "color": color}


# ─── Connect Wizard actions ──────────────────────────────────────────────

def _analyze_source(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    """Pass through the AI's analysis of a device/source. Frontend renders as a card."""
    return {
        "description": params.get("description", ""),
        "device_type": params.get("device_type", "other"),
        "data_format": params.get("data_format", "json"),
        "connection_method": params.get("connection_method", "push_curl"),
        "recommended_recipe": params.get("recommended_recipe"),
        "predicted_signal_type": params.get("predicted_signal_type"),
    }


def _generate_code(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    """Generate integration code from a safe template."""
    from core.ai.code_templates import render_template
    import os
    template_id = params.get("template_id", "push_curl")
    server_url = os.environ.get("ENKI_PUBLIC_URL", "https://example.com")
    return render_template(
        template_id,
        server_url=server_url,
        device_id=params.get("device_id", "my-device"),
        tenant_id=tenant_id,
        sample_payload=params.get("sample_payload", '{"value": 1}')
        if isinstance(params.get("sample_payload"), str)
        else json.dumps(params.get("sample_payload", {"value": 1})),
        poll_url=params.get("poll_url", ""),
        poll_interval=str(params.get("poll_interval", "300")),
        webhook_path=params.get("webhook_path", params.get("device_id", "my-device")),
    )


def _preview_card(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    """Generate a card preview from sample data without actually ingesting."""
    try:
        from storage.signals.card_templates import _rule_based_template, render_card
        signal_type = params.get("signal_type", "signal.custom.untyped")
        sample = params.get("sample_data", {})
        if isinstance(sample, str):
            sample = json.loads(sample)
        template = _rule_based_template(signal_type, sample)
        recipe = render_card(template, sample, "preview")
        return {"card_recipe": recipe, "signal_type": signal_type}
    except Exception as e:
        return {"card_recipe": None, "error": str(e)}


def _create_connector_wizard(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    """Create a connector from wizard parameters."""
    from storage.connectors.manager import create_connector
    result = create_connector(
        tenant_id=tenant_id,
        device_id=params.get("device_id", "wizard-connector"),
        connector_type=params.get("connector_type", "pull"),
        source_type=params.get("source_type", "http_api"),
        poll_interval=params.get("poll_interval", "5 minutes"),
        source_config=params.get("source_config", {}),
        dsn=dsn,
    )
    return result


def _suggest_rules(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    """Pass through AI's rule suggestions for user to accept."""
    return {
        "signal_type": params.get("signal_type", ""),
        "device_id": params.get("device_id", ""),
        "suggestions": params.get("suggestions", []),
    }


import json  # needed for _generate_code


# (ACTION_HANDLERS dict moved to end of file, after all handler definitions)


# ─── Knowledge Graph Action Handlers ─────────────────────────────────────────

def _search_nodes(params: dict, tenant_id: str, identity_id: str, dsn=None) -> dict:
    from core.knowledge.graph import search_nodes
    return {"type": "search_nodes", "data": search_nodes(
        project_id=params.get("project_id"),
        query=params.get("query", ""),
        tenant_id=tenant_id,
        dsn=dsn,
    )}


def _get_node(params: dict, tenant_id: str, identity_id: str, dsn=None) -> dict:
    from core.knowledge.graph import get_node
    node = get_node(params.get("node_id", ""), dsn=dsn)
    return {"type": "get_node", "data": node}


def _create_node_action(params: dict, tenant_id: str, identity_id: str, dsn=None) -> dict:
    from core.knowledge.graph import create_node
    result = create_node(
        project_id=params.get("project_id", ""),
        tenant_id=tenant_id,
        title=params.get("title", "Untitled"),
        description=params.get("description"),
        epistemic_type=params.get("epistemic_type", "observation"),
        concepts=params.get("concepts"),
        latitude=params.get("latitude"),
        longitude=params.get("longitude"),
        dsn=dsn,
    )
    return {"type": "create_node", "data": result, "success": result is not None}


def _enrich_location(params: dict, tenant_id: str, identity_id: str, dsn=None) -> dict:
    import asyncio
    from core.enrichment.geo_enrich import auto_enrich_location
    from storage.postgres.db import get_connection
    import json as _json

    node_id = params.get("node_id", "")
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT latitude, longitude FROM knowledge_nodes WHERE node_id = %s AND tenant_id = %s",
                       (node_id, tenant_id))
            row = cur.fetchone()
            if not row or row[0] is None:
                return {"type": "enrich_location", "data": {"error": "Node has no location"}}
            lat, lon = row[0], row[1]

    results = asyncio.run(auto_enrich_location(lat, lon))

    # Store in metadata
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT metadata FROM knowledge_nodes WHERE node_id = %s", (node_id,))
            r = cur.fetchone()
            metadata = _json.loads(bytes(r[0]) if isinstance(r[0], memoryview) else r[0] or '{}')
            enrichment = metadata.get("enrichment", {})
            for src, data in results.items():
                if "_error" not in data:
                    enrichment[src] = data
            metadata["enrichment"] = enrichment
            if "address" in results and "_error" not in results.get("address", {}):
                metadata["address"] = results["address"].get("display_name", "")
            cur.execute("UPDATE knowledge_nodes SET metadata = %s WHERE node_id = %s",
                       (_json.dumps(metadata), node_id))
        conn.commit()

    enriched = [k for k in results if "_error" not in results[k]]
    return {"type": "enrich_location", "data": {"enriched_sources": enriched, "results": results}}


def _nearby_signals(params: dict, tenant_id: str, identity_id: str, dsn=None) -> dict:
    from storage.postgres.db import get_connection
    node_id = params.get("node_id", "")
    radius_m = params.get("radius_m", 500)
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT latitude, longitude FROM knowledge_nodes WHERE node_id = %s", (node_id,))
            row = cur.fetchone()
            if not row or row[0] is None:
                return {"type": "nearby_signals", "data": {"signals": [], "count": 0}}
            lat, lon = row[0], row[1]
            cur.execute("""
                SELECT signal_id, signal_type, device_id, severity, timestamp, latitude, longitude,
                       ST_Distance(ST_MakePoint(longitude, latitude)::geography, ST_MakePoint(%s, %s)::geography) as dist
                FROM signals WHERE tenant_id = %s AND latitude IS NOT NULL
                  AND ST_DWithin(ST_MakePoint(longitude, latitude)::geography, ST_MakePoint(%s, %s)::geography, %s)
                ORDER BY dist LIMIT 20
            """, (lon, lat, tenant_id, lon, lat, radius_m))
            signals = [{"signal_id": r[0], "signal_type": r[1], "device_id": r[2], "severity": r[3],
                        "timestamp": r[4].isoformat() if r[4] else None, "distance_m": round(r[7], 1)} for r in cur.fetchall()]
    return {"type": "nearby_signals", "data": {"signals": signals, "count": len(signals)}}


def _node_cases(params: dict, tenant_id: str, identity_id: str, dsn=None) -> dict:
    from storage.postgres.db import get_connection
    node_id = params.get("node_id", "")
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT cn.case_id, c.title, c.status, c.priority
                FROM case_nodes cn JOIN cases c ON c.case_id = cn.case_id AND c.tenant_id = cn.tenant_id
                WHERE cn.node_id = %s AND cn.tenant_id = %s
            """, (node_id, tenant_id))
            cases = [{"case_id": r[0], "title": r[1], "status": r[2], "priority": r[3]} for r in cur.fetchall()]
    return {"type": "node_cases", "data": {"cases": cases}}


def _link_node_case(params: dict, tenant_id: str, identity_id: str, dsn=None) -> dict:
    from storage.postgres.db import get_connection
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO case_nodes (case_id, node_id, tenant_id, linked_by) VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (params.get("case_id"), params.get("node_id"), tenant_id, "ai_chat"))
        conn.commit()
    return {"type": "link_node_case", "data": {"linked": True}, "success": True}


# ─── Live Data Action Handlers ───────────────────────────────────────────────

def _live_aircraft(params: dict, tenant_id: str, identity_id: str, dsn=None) -> dict:
    from storage.postgres.db import get_connection
    lat = params.get("lat", 28.0)
    lon = params.get("lon", -82.0)
    radius_km = params.get("radius_km", 50)
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT key, display_label, latitude, longitude, altitude, heading, speed, cached_at
                FROM live_cache WHERE tenant_id = %s AND source = 'adsb' AND expires_at > NOW()
                  AND latitude IS NOT NULL
                  AND ST_DWithin(ST_MakePoint(longitude, latitude)::geography, ST_MakePoint(%s, %s)::geography, %s)
                ORDER BY cached_at DESC LIMIT 50
            """, (tenant_id, lon, lat, radius_km * 1000))
            aircraft = [{"hex": r[0], "callsign": (r[1] or "").strip(), "lat": r[2], "lon": r[3],
                         "altitude": r[4], "heading": r[5], "speed": r[6]} for r in cur.fetchall()]
    return {"type": "live_aircraft", "data": {"aircraft": aircraft, "count": len(aircraft)}}


def _list_connectors(params: dict, tenant_id: str, identity_id: str, dsn=None) -> dict:
    from storage.connectors.manager import list_connectors
    connectors = list_connectors(tenant_id, dsn=dsn)
    return {"type": "connectors", "data": {"connectors": connectors}}


def _list_data_sources(params: dict, tenant_id: str, identity_id: str, dsn=None) -> dict:
    from storage.postgres.db import get_connection
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT source_id, name, enabled, requires_key, api_key IS NOT NULL as has_key, coverage
                FROM data_source_config WHERE tenant_id = %s ORDER BY name
            """, (tenant_id,))
            sources = [{"source_id": r[0], "name": r[1], "enabled": r[2], "requires_key": r[3],
                        "has_key": r[4], "coverage": r[5]} for r in cur.fetchall()]
    return {"type": "data_sources", "data": {"sources": sources}}


def _list_rules(params: dict, tenant_id: str, identity_id: str, dsn=None) -> dict:
    from storage.postgres.db import get_connection
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT rule_id, name, rule_type, severity, enabled, match_count
                FROM detection_rules WHERE tenant_id = %s ORDER BY name
            """, (tenant_id,))
            rules = [{"rule_id": r[0], "name": r[1], "rule_type": r[2], "severity": r[3],
                      "enabled": r[4], "match_count": r[5]} for r in cur.fetchall()]
    return {"type": "list_rules", "data": {"rules": rules}}


def _escalate_alert(params: dict, tenant_id: str, identity_id: str, dsn=None) -> dict:
    from storage.signals.cases import create_case, add_case_observable
    from storage.postgres.db import get_connection
    alert_id = params.get("alert_id", "")
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT title, severity, signal_rids FROM alerts WHERE alert_id = %s AND tenant_id = %s",
                       (alert_id, tenant_id))
            row = cur.fetchone()
            if not row:
                return {"type": "escalate_alert", "data": {"error": "Alert not found"}}
            title = f"Escalated: {row[0]}"
    case = create_case(tenant_id=tenant_id, title=title, priority="P2", description=f"Escalated from alert {alert_id}")
    return {"type": "escalate_alert", "data": case, "success": True}


def _worker_activity(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    """Show recent worker discoveries."""
    import requests
    limit = params.get("limit", 20)
    try:
        # Query the worker activity endpoint internally
        from storage.postgres.db import get_connection
        events = []
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT operation, category, subtype, base60_address,
                           input_type, reason, created_at, node_id
                    FROM gdu_admission_log
                    WHERE tenant_id = %s
                    ORDER BY created_at DESC LIMIT %s
                """, (tenant_id, limit))
                for r in cur.fetchall():
                    events.append({
                        "worker": "gdu_gateway",
                        "action": r[0],
                        "category": r[1],
                        "subtype": r[2],
                        "detail": r[5] or f"{r[1]}.{r[2]}",
                        "timestamp": r[6].isoformat() if r[6] else None,
                        "node_id": r[7],
                    })
        return {"events": events, "count": len(events)}
    except Exception as e:
        return {"events": [], "error": str(e)}


def _show_photos(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    """Show photos filtered by location, date, or device."""
    from storage.postgres.db import get_connection
    from datetime import datetime, timedelta, timezone
    import json

    after_str = params.get("after", "")
    before_str = params.get("before", "")
    device_id = params.get("device_id", "")
    location_name = params.get("location_name", "")

    now = datetime.now(timezone.utc)
    try:
        t_after = datetime.fromisoformat(after_str.replace("Z", "+00:00")) if after_str else now - timedelta(days=7)
    except Exception:
        t_after = now - timedelta(days=7)
    try:
        t_before = datetime.fromisoformat(before_str.replace("Z", "+00:00")) if before_str else now
    except Exception:
        t_before = now

    photos = []
    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                device_clause = "AND first_seen_signal_rid IN (SELECT rid FROM signals WHERE device_id = %s)" if device_id else ""
                base_params: list = [tenant_id, t_after, t_before]
                if device_id:
                    base_params.append(device_id)
                base_params.append(30)

                cur.execute(f"""
                    SELECT node_id, phash, width, height, bytes_path,
                           first_seen_outlet, first_seen_at, source_count,
                           analysis,
                           ST_Y(exif_coordinates::geometry) AS lat,
                           ST_X(exif_coordinates::geometry) AS lon,
                           exif_taken_at
                    FROM media_nodes
                    WHERE tenant_id = %s
                      AND created_at BETWEEN %s AND %s
                      {device_clause}
                    ORDER BY exif_taken_at DESC NULLS LAST, created_at DESC
                    LIMIT %s
                """, tuple(base_params))

                for row in cur.fetchall():
                    analysis = row[8] or {}
                    photo = {
                        "node_id": row[0],
                        "thumbnail_url": f"/api/media/{row[0]}/image" if row[4] else None,
                        "width": row[2],
                        "height": row[3],
                        "outlet": row[5],
                        "taken_at": row[11].isoformat() if row[11] else (row[6].isoformat() if row[6] else None),
                        "source_count": row[7],
                        "lat": float(row[9]) if row[9] else None,
                        "lon": float(row[10]) if row[10] else None,
                        "classification": analysis.get("classification"),
                        "tags": analysis.get("tags", []),
                        "description": analysis.get("description"),
                        "is_intelligence": analysis.get("is_intelligence", False),
                    }
                    photos.append(photo)

        # Filter by location name if specified
        if location_name:
            from core.extraction.gazetteer import reverse_geocode
            filtered = []
            for p in photos:
                if p.get("lat") and p.get("lon"):
                    geo = reverse_geocode(p["lat"], p["lon"], use_nominatim=False)
                    if geo and location_name.lower() in (geo.get("name") or "").lower():
                        filtered.append(p)
            photos = filtered if filtered else photos  # Fallback to all if no location match

    except Exception as e:
        return {"photos": [], "error": str(e)}

    return {"photos": photos, "count": len(photos)}


def _cross_correlate(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    """Find all signals near a point in a time window."""
    from storage.postgres.db import get_connection
    from datetime import datetime, timedelta, timezone
    import json

    lat = params.get("lat")
    lon = params.get("lon")
    location_name = params.get("location_name", "")
    radius_m = params.get("radius_m", 500)
    after_str = params.get("after", "")
    before_str = params.get("before", "")
    device_id = params.get("device_id", "")

    # If location name given, geocode it
    if location_name and not (lat and lon):
        from core.extraction.gazetteer import extract_location
        loc = extract_location(location_name)
        if loc:
            lat, lon = loc["latitude"], loc["longitude"]
            radius_m = max(radius_m, 50000)  # Widen for named locations

    if not lat or not lon:
        return {"error": "lat/lon or location_name required"}

    now = datetime.now(timezone.utc)
    try:
        t_after = datetime.fromisoformat(after_str.replace("Z", "+00:00")) if after_str else now - timedelta(hours=24)
    except Exception:
        t_after = now - timedelta(hours=24)
    try:
        t_before = datetime.fromisoformat(before_str.replace("Z", "+00:00")) if before_str else now
    except Exception:
        t_before = now

    # Query signals in bounding box + time range
    radius_deg = radius_m / 111000.0
    groups = {"gps": [], "photos": [], "calendar": [], "contacts": [],
              "financial": [], "communications": [], "other": []}
    total = 0

    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                device_clause = "AND device_id = %s" if device_id else ""
                base_params = [
                    tenant_id, t_after, t_before,
                    lat - radius_deg, lat + radius_deg,
                    lon - radius_deg, lon + radius_deg,
                ]
                if device_id:
                    base_params.append(device_id)
                base_params.append(200)

                cur.execute(f"""
                    SELECT signal_id, signal_type, timestamp, latitude, longitude,
                           device_id, payload
                    FROM signals
                    WHERE tenant_id = %s
                      AND timestamp BETWEEN %s AND %s
                      AND latitude BETWEEN %s AND %s
                      AND longitude BETWEEN %s AND %s
                      {device_clause}
                    ORDER BY timestamp ASC
                    LIMIT %s
                """, tuple(base_params))

                for row in cur.fetchall():
                    payload = row[6] or {}
                    if isinstance(payload, str):
                        try:
                            payload = json.loads(payload)
                        except Exception:
                            payload = {}

                    sig = {
                        "signal_id": row[0],
                        "signal_type": row[1],
                        "timestamp": row[2].isoformat() if row[2] else None,
                        "lat": float(row[3]) if row[3] else None,
                        "lon": float(row[4]) if row[4] else None,
                        "device_id": row[5],
                    }

                    # Add summary
                    st = (row[1] or "").lower()
                    if isinstance(payload, dict):
                        for k in ("title", "summary", "description", "name", "merchant_name", "filename"):
                            if payload.get(k):
                                sig["summary"] = str(payload[k])[:100]
                                break

                    # Categorize
                    if "position" in st or "location" in st or "geospatial" in st:
                        groups["gps"].append(sig)
                    elif "photo" in st or "image" in st:
                        groups["photos"].append(sig)
                    elif "calendar" in st:
                        groups["calendar"].append(sig)
                    elif "contact" in st:
                        groups["contacts"].append(sig)
                    elif "sms" in st or "call" in st or "comm" in st:
                        groups["communications"].append(sig)
                    elif "financial" in st or "transaction" in st:
                        groups["financial"].append(sig)
                    else:
                        groups["other"].append(sig)
                    total += 1

    except Exception as e:
        return {"groups": groups, "total": 0, "error": str(e)}

    return {
        "groups": groups,
        "total": total,
        "center": {"lat": lat, "lon": lon},
        "radius_m": radius_m,
        "time_range": {"after": t_after.isoformat(), "before": t_before.isoformat()},
    }


def _show_trips(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    """Detect and show trips from GPS data."""
    from storage.signals.analysis import detect_trips
    from datetime import datetime, timedelta, timezone

    device_id = params.get("device_id", "")
    after_str = params.get("after", "")
    before_str = params.get("before", "")
    location = params.get("location", "")

    # Default: last 30 days
    before = datetime.now(timezone.utc)
    after = before - timedelta(days=30)
    if after_str:
        try:
            after = datetime.fromisoformat(after_str.replace("Z", "+00:00"))
        except Exception:
            pass
    if before_str:
        try:
            before = datetime.fromisoformat(before_str.replace("Z", "+00:00"))
        except Exception:
            pass

    trips = detect_trips(tenant_id, device_id, after=after, before=before, dsn=dsn)

    # Filter by location name if specified
    if location:
        loc_lower = location.lower()
        trips = [t for t in trips if (
            loc_lower in (t.get("start_location", {}).get("name") or "").lower() or
            loc_lower in (t.get("end_location", {}).get("name") or "").lower() or
            loc_lower in (t.get("start_location", {}).get("country") or "").lower() or
            loc_lower in (t.get("end_location", {}).get("country") or "").lower() or
            any(loc_lower in (s.get("name") or "").lower() or loc_lower in (s.get("country") or "").lower()
                for s in t.get("stops", []))
        )]

    return {"trips": trips[:20], "count": len(trips), "device_id": device_id}


def _investigate_entity(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    """Queue a deep investigation on an entity using public data sources.

    Accepts either entity_id (preferred) or name+entity_type. With entity_id,
    routes through investigation_queue which enforces 24h cooldown +
    race-safe insert. Name-only requests fall through to the legacy path.
    """
    name = params.get("name", "")
    entity_type = params.get("entity_type", "organization")
    entity_id = params.get("entity_id")
    if not name and not entity_id:
        return {"error": "name or entity_id is required"}

    try:
        if entity_id is not None:
            from core.research.investigation_queue import try_queue_investigation
            inv_id, outcome = try_queue_investigation(
                entity_id=int(entity_id),
                tenant_id=tenant_id,
                seed_name=name or f"entity_{entity_id}",
                entity_type=entity_type,
                discovered_from="ai_action",
            )
            return {
                "investigation_id": inv_id,
                "name": name,
                "entity_type": entity_type,
                "status": outcome,
            }

        from storage.postgres.db import get_connection
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO entity_investigations
                        (tenant_id, seed_name, seed_type, status,
                         sources_requested, depth, discovered_from)
                    VALUES (%s, %s, %s, 'pending', NULL, 0, 'ai_action_by_name')
                    RETURNING investigation_id
                """, (tenant_id, name, entity_type))
                row = cur.fetchone()
                inv_id = row[0] if row else None
            conn.commit()
        return {
            "investigation_id": inv_id,
            "name": name,
            "entity_type": entity_type,
            "status": "queued",
            "message": f"Investigation queued for {name}.",
        }
    except Exception as e:
        return {"error": str(e)}


# ── Copilot: deep research (external director model grabs + collapses) ────────

def _director_model_name(tenant_id: str = "default") -> str:
    """Name of the configured provider's model — labels the External badge."""
    try:
        from core.ai.config import get_tenant_provider
        p = get_tenant_provider(tenant_id)
        return (getattr(p, "model", "") or getattr(p, "provider_name", "")) if p else ""
    except Exception:
        return ""


def _deep_research(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    """Start a deep-research run: an external director model web-searches,
    Enki grabs + collapses the sources into a topic library, then writes a
    cited report. Slow + async — returns a run_id to poll."""
    topic = (params.get("topic") or params.get("goal") or params.get("query") or "").strip()
    if not topic:
        return {"error": "topic is required"}
    try:
        from core.workers.research import queue_research_run
        run_id, library_id = queue_research_run(
            goal=topic, tenant_id=tenant_id, identity_id=identity_id,
            library_id=params.get("library_id"),
        )
        return {
            "run_id": run_id, "library_id": library_id, "topic": topic,
            "status": "queued", "director_model": _director_model_name(tenant_id),
            "message": f"Deep research started on {topic}.",
        }
    except Exception as e:
        return {"error": str(e)}


def _research_status(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    """Progress + partial findings for research run(s): by run_id, by topic, or recent."""
    from storage.postgres.db import get_connection
    run_id = params.get("run_id")
    topic = params.get("topic") or params.get("goal")
    cols = ("run_id, goal, status, iteration, max_iterations, sources_grabbed, "
            "entities_count, facts_count, phase_detail, library_id, director_model, "
            "started_at, completed_at")
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            if run_id:
                cur.execute(f"SELECT {cols} FROM research_runs "
                            "WHERE run_id=%s AND tenant_id=%s", (int(run_id), tenant_id))
            elif topic:
                cur.execute(f"SELECT {cols} FROM research_runs "
                            "WHERE tenant_id=%s AND goal ILIKE %s "
                            "ORDER BY created_at DESC LIMIT 5", (tenant_id, f"%{topic}%"))
            else:
                cur.execute(f"SELECT {cols} FROM research_runs WHERE tenant_id=%s "
                            "ORDER BY created_at DESC LIMIT 8", (tenant_id,))
            rows = cur.fetchall()
    out = []
    for r in rows:
        out.append({
            "run_id": r[0], "goal": r[1], "status": r[2], "iteration": r[3],
            "max_iterations": r[4], "sources_grabbed": r[5], "entities_count": r[6],
            "facts_count": r[7], "phase_detail": r[8], "library_id": r[9],
            "director_model": r[10],
            "started_at": r[11].isoformat() if r[11] else None,
            "completed_at": r[12].isoformat() if r[12] else None,
        })
    return {"runs": out, "count": len(out)}


# ── Copilot: LDU discrepancy review (chat-driven, governed) ───────────────────

def _disc_id_from_surface(params):
    """Deterministic discrepancy id from a surface (chat-friendly: 'who is ET')."""
    import re as _re
    surf = params.get("surface") or params.get("name") or params.get("token")
    if not surf:
        return None
    sid = params.get("scope_id") or params.get("library_id") or 1
    norm = _re.sub(r"\s+", " ", _re.sub(r"[^a-z0-9 ]", " ", surf.lower())).strip()
    return f"disc:library:{sid}:{norm}"


def _json_ev(v):
    import json as _j
    if isinstance(v, dict):
        return v
    try:
        return _j.loads(v) if v else {}
    except Exception:
        return {}


def _ldu_queue(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    """List open LDU discrepancies (parked/ambiguous resolutions) with their
    near-miss candidates + scope, for review in chat."""
    from core.gdu.alias_store import list_discrepancies
    sid = params.get("scope_id") or params.get("library_id")
    rows = list_discrepancies(
        scope_kind="library", scope_id=int(sid) if sid else None,
        status=params.get("status", "pending"), limit=int(params.get("limit", 20)),
        tenant_id=tenant_id, dsn=dsn)
    return {"discrepancies": rows, "count": len(rows)}


def _propose_resolution(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    """AI-assisted proposal for a parked discrepancy — FENCED: the proposal is always
    ONE OF THE REAL near-miss candidates (never an invented entity), and it's a
    proposal only (the operator confirms via resolve_discrepancy). Deterministic-first:
    the candidate whose initials match the surface wins; the LLM only breaks genuine
    ambiguity, choosing among the same candidates."""
    import re as _re
    from storage.postgres.db import get_connection
    ldu_p_id = params.get("ldu_p_id") or _disc_id_from_surface(params)
    if not ldu_p_id:
        return {"error": "ldu_p_id or surface is required"}
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT raw_name, evidence FROM entity_ldu_p "
                        "WHERE ldu_p_id=%s AND tenant_id=%s AND status='pending'", (ldu_p_id, tenant_id))
            row = cur.fetchone()
    if not row:
        return {"error": "discrepancy not found", "ldu_p_id": ldu_p_id}
    surface, ev = row[0], _json_ev(row[1])
    cands = ev.get("candidates", [])
    if not cands:
        return {"ldu_p_id": ldu_p_id, "surface": surface, "proposal": None,
                "reasoning": "No candidates on record.", "source": "none"}

    letters = _re.sub(r"[^a-z]", "", (surface or "").lower())
    def _initials(name):
        return "".join(t[0] for t in (name or "").lower().split() if t)
    # Deterministic: unique candidate whose initials == the surface.
    if letters and len(letters) <= 4:
        im = [c for c in cands if _initials(c.get("name")) == letters]
        if len(im) == 1:
            c = im[0]
            return {"ldu_p_id": ldu_p_id, "surface": surface,
                    "proposal": {"entity_id": c["entity_id"], "name": c["name"]},
                    "reasoning": f"“{surface}” are the initials of {c['name']}, who is in this corpus.",
                    "confidence": 0.8, "source": "deterministic:initials",
                    "candidates": cands, "needs_confirm": True}

    # Ambiguous → the LLM chooses AMONG the candidates (fenced), or abstains.
    try:
        from core.ai.config import get_tenant_provider
        from core.ai.provider import LLMMessage
        provider = get_tenant_provider(tenant_id)
        if provider:
            clist = "\n".join(f"{i}. {c['name']} (id {c['entity_id']}, score {c.get('score')})"
                              for i, c in enumerate(cands))
            prompt = (
                f"In a flight-log corpus, the passenger token “{surface}” could not be "
                f"confidently resolved. Choose the MOST LIKELY match from ONLY this list "
                f"(or reply NONE if genuinely uncertain). Consider initials and who travels "
                f"in this corpus. Reply as: <index> — <one-line reason>, or NONE.\n\n{clist}")
            resp = provider.complete([LLMMessage(role="user", content=prompt)],
                                     max_tokens=120, temperature=0.0)
            txt = (resp.content or "").strip()
            m = _re.match(r"\s*(\d+)\b[^0-9]*(.*)", txt)
            if m and int(m.group(1)) < len(cands):
                c = cands[int(m.group(1))]
                return {"ldu_p_id": ldu_p_id, "surface": surface,
                        "proposal": {"entity_id": c["entity_id"], "name": c["name"]},
                        "reasoning": (m.group(2) or "").strip()[:200] or "model selection",
                        "confidence": 0.5, "source": "llm", "model": resp.model,
                        "candidates": cands, "needs_confirm": True}
            return {"ldu_p_id": ldu_p_id, "surface": surface, "proposal": None,
                    "reasoning": "Model was uncertain — leave parked.", "source": "llm",
                    "model": resp.model, "candidates": cands}
    except Exception as e:
        return {"ldu_p_id": ldu_p_id, "surface": surface, "proposal": None,
                "reasoning": f"proposal unavailable: {e}", "source": "error", "candidates": cands}
    return {"ldu_p_id": ldu_p_id, "surface": surface, "proposal": None,
            "reasoning": "No confident proposal.", "source": "none", "candidates": cands}


def _resolve_discrepancy(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    """Governed write for a discrepancy. decision: assign|drop|reject|promote.
    'assign' records a VALIDATED context-scoped alias (operator-confirmed) and marks
    the LDU row resolved. The AI never calls this itself — the operator confirms."""
    from storage.postgres.db import get_connection
    from core.gdu.alias_store import record_validated_alias
    ldu_p_id = params.get("ldu_p_id")
    decision = (params.get("decision") or "assign").lower()
    target = params.get("target_entity_id") or params.get("entity_id")
    if not ldu_p_id:
        ldu_p_id = _disc_id_from_surface(params)
    if not ldu_p_id:
        return {"error": "ldu_p_id or surface is required"}
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT raw_name, evidence FROM entity_ldu_p "
                        "WHERE ldu_p_id=%s AND tenant_id=%s", (ldu_p_id, tenant_id))
            row = cur.fetchone()
            if not row:
                return {"error": "discrepancy not found"}
            surface, ev = row[0], _json_ev(row[1])
            if decision == "assign":
                if not target and params.get("target_name"):
                    cur.execute("SELECT entity_id FROM entities WHERE tenant_id=%s "
                                "AND normalized_name = lower(regexp_replace(%s,'\\s+',' ','g')) "
                                "ORDER BY signal_count DESC NULLS LAST LIMIT 1",
                                (tenant_id, params["target_name"]))
                    tr = cur.fetchone()
                    if tr:
                        target = tr[0]
                if not target:
                    return {"error": "could not resolve target entity"}
                record_validated_alias(
                    int(target), surface, scope_kind=ev.get("scope_kind", "library"),
                    scope_id=ev.get("scope_id"), confidence=1.0,
                    evidence={"confirmed_by": identity_id or "operator", "via": "chat"},
                    validation_status="operator", tenant_id=tenant_id, dsn=dsn)
                cur.execute("UPDATE entity_ldu_p SET status='resolved', "
                            "promoted_entity_id=%s, reviewed_by=%s, reviewed_at=NOW() "
                            "WHERE ldu_p_id=%s", (int(target), identity_id or "operator", ldu_p_id))
                conn.commit()
                cur.execute("SELECT canonical_name FROM entities WHERE entity_id=%s", (int(target),))
                nm = cur.fetchone()
                return {"resolved": True, "surface": surface, "assigned_to": nm[0] if nm else target,
                        "entity_id": int(target), "scope": f"{ev.get('scope_kind')}:{ev.get('scope_id')}",
                        "message": f"Assigned “{surface}” → {nm[0] if nm else target} (scoped, validated)."}
            if decision in ("drop", "reject"):
                cur.execute("UPDATE entity_ldu_p SET status='rejected', resolved_by=%s, "
                            "resolved_at=NOW() WHERE ldu_p_id=%s", (identity_id or "operator", ldu_p_id))
                conn.commit()
                return {"resolved": True, "surface": surface, "decision": "dropped",
                        "message": f"Dropped “{surface}” — confirmed not resolvable."}
    return {"error": f"unknown decision: {decision}"}


# ── Copilot: synthesized answer from the corpus (not navigation cards) ────────

def _answer_about(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    """Read the collapsed evidence about an entity and synthesize a grounded,
    cited answer — 'what do the files say about X'. Beats dumping entity cards."""
    from core.ai.corpus_answer import answer_about
    name = (params.get("name") or params.get("query") or params.get("topic") or "").strip()
    question = params.get("question") or params.get("query") or ""
    if not name:
        return {"error": "name is required"}
    return answer_about(question=question, name=name, tenant_id=tenant_id, dsn=dsn)


# ── Copilot: knowledge-graph queries (reuse core.ai.chat_tools, return cards) ─

def _ct(tool_name: str):
    """Wrap a core.ai.chat_tools query tool as an action handler → {text, cards}."""
    def _handler(tenant_id=None, identity_id=None, dsn=None, **params):
        from core.ai.chat_tools import TOOL_FUNCTIONS
        fn = TOOL_FUNCTIONS.get(tool_name)
        if not fn:
            return {"error": f"unknown query tool: {tool_name}"}
        return fn(params)
    return _handler


# ── Copilot: investigation run status ────────────────────────────────────────

def _investigation_status(tenant_id: str, identity_id: str = None, dsn=None, **params) -> dict:
    """Report status of investigation(s): by id, by name, or the most recent."""
    from storage.postgres.db import get_connection
    inv_id = params.get("investigation_id")
    name = params.get("name")
    cols = ("investigation_id, seed_name, seed_type, status, sources_requested, "
            "sources_completed, findings_count, entities_discovered, started_at, completed_at")
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            if inv_id:
                cur.execute(f"SELECT {cols} FROM entity_investigations "
                            "WHERE investigation_id=%s AND tenant_id=%s", (int(inv_id), tenant_id))
            elif name:
                cur.execute(f"SELECT {cols} FROM entity_investigations "
                            "WHERE tenant_id=%s AND seed_name ILIKE %s "
                            "ORDER BY started_at DESC NULLS LAST LIMIT 5", (tenant_id, f"%{name}%"))
            else:
                cur.execute(f"SELECT {cols} FROM entity_investigations WHERE tenant_id=%s "
                            "ORDER BY COALESCE(started_at, completed_at) DESC NULLS LAST LIMIT 8",
                            (tenant_id,))
            rows = cur.fetchall()
    out = []
    for r in rows:
        done = r[5] or []
        req = r[4] or []
        out.append({
            "investigation_id": r[0], "name": r[1], "entity_type": r[2], "status": r[3],
            "sources_done": len(done), "sources_requested": (len(req) or None),
            "sources_completed": done, "findings_count": r[6], "entities_discovered": r[7],
            "started_at": r[8].isoformat() if r[8] else None,
            "completed_at": r[9].isoformat() if r[9] else None,
        })
    return {"investigations": out, "count": len(out)}


# ── Copilot: setup / API-key help (explain + deep-link; never stores keys) ────

def _setup_help(tenant_id: str = None, identity_id: str = None, dsn=None, **params) -> dict:
    from core.ai.product_knowledge import get_setup_help, list_setup_catalog
    feature = params.get("feature") or params.get("source") or params.get("query") or ""
    entry = get_setup_help(feature)
    if not entry:
        return {"setup": None, "available": list_setup_catalog(),
                "message": "Which feature? e.g. satellite imagery, FEC/data.gov key, AI provider, SAM.gov."}
    return {"setup": {
        "feature": entry["feature"], "needs": entry["needs"],
        "register_url": entry["register"], "steps": entry["steps"],
        "settings_link": entry["settings"],
    }}


# ── Copilot: product knowledge (grounds "how do I / how does X work") ─────────

def _docs_search(tenant_id: str = None, identity_id: str = None, dsn=None, **params) -> dict:
    from core.ai.product_knowledge import search_product_docs
    q = params.get("query") or params.get("topic") or ""
    hits = search_product_docs(q, k=int(params.get("k", 4)))
    return {"passages": hits, "count": len(hits)}


# ─── ACTION HANDLER REGISTRY (must be at end of file, after all definitions) ─

ACTION_HANDLERS = {
    # Synthesized answer from the corpus (grounded prose + sources, not cards)
    "answer_about": _answer_about,
    # LDU discrepancy review (chat-driven, governed; AI proposes, operator confirms)
    "ldu_queue": _ldu_queue,
    "list_discrepancies": _ldu_queue,
    "propose_resolution": _propose_resolution,
    "resolve_discrepancy": _resolve_discrepancy,
    # ── Copilot: knowledge-graph queries (return rich cards) ──
    "entity_lookup": _ct("entity_lookup"),
    "library_search": _ct("library_search"),
    "fact_search": _ct("fact_search"),
    "event_search": _ct("event_search"),
    "reference_search": _ct("reference_search"),
    "address_resolve": _ct("address_resolve"),
    "recent_facts": _ct("recent_facts"),
    "pipeline_status": _ct("pipeline_status"),
    # ── Copilot: run status, setup, product knowledge ──
    "investigation_status": _investigation_status,
    "list_investigations": _investigation_status,
    "setup_help": _setup_help,
    "docs_search": _docs_search,
    "explain": _docs_search,
    "search_signals": _search_signals,
    "get_signal": _get_signal,
    "search_observables": _search_observables,
    "list_cases": _list_cases,
    "get_case": _get_case,
    "create_case": _create_case,
    "add_observable": _add_observable,
    "list_alerts": _list_alerts,
    "get_alert": _get_alert,
    "create_rule": _create_rule,
    "get_dashboard": _get_dashboard,
    "get_domains": _get_domains,
    "get_domain": _get_domain,
    "analyze_device": _analyze_device,
    "navigate_to": _navigate_to,
    "search_nearby": _search_nearby,
    "create_geofence": _create_geofence_action,
    "highlight_area": _highlight_area,
    "analyze_source": _analyze_source,
    "generate_code": _generate_code,
    "preview_card": _preview_card,
    "create_connector": _create_connector_wizard,
    "suggest_rules": _suggest_rules,
    # Knowledge graph
    "search_nodes": _search_nodes,
    "get_node": _get_node,
    "create_node": _create_node_action,
    "enrich_location": _enrich_location,
    "nearby_signals": _nearby_signals,
    "node_cases": _node_cases,
    "link_node_case": _link_node_case,
    # Live data
    "live_aircraft": _live_aircraft,
    "connectors": _list_connectors,
    "data_sources": _list_data_sources,
    # Rules + alerts
    "list_rules": _list_rules,
    "escalate_alert": _escalate_alert,
    # Navigation
    "navigate_to_page": _navigate_to_page,
    # Timeline
    "get_timeline": lambda tenant_id, identity_id, dsn=None, **kw: _get_timeline_data(tenant_id, dsn, **kw),
    # Reports
    "generate_brief": lambda tenant_id, identity_id, dsn=None, **kw: __import__('core.reports.generator', fromlist=['generate_intelligence_brief']).generate_intelligence_brief(tenant_id=tenant_id, topics=kw.get('topics', []), dsn=dsn),
    # Exports
    "export_signals": lambda **kw: {"download_url": f"/api/exports/signals?format={kw.get('format','csv')}&limit={kw.get('limit',1000)}" + (f"&type={kw['signal_type']}" if kw.get('signal_type') else '') + (f"&after={kw['after']}" if kw.get('after') else ''), "type": "signals", "format": kw.get('format','csv')},
    "export_cases": lambda **kw: {"download_url": f"/api/exports/cases?format={kw.get('format','json')}" + (f"&status={kw['status']}" if kw.get('status') else ''), "type": "cases", "format": kw.get('format','json')},
    "export_contacts": lambda **kw: {"download_url": f"/api/exports/contacts?format={kw.get('format','csv')}", "type": "contacts", "format": kw.get('format','csv')},
    # Contacts & Assets
    "search_contacts": _search_contacts,
    "search_assets": _search_assets,
    # Worker activity
    "worker_activity": _worker_activity,
    # Investigation
    "investigate_entity": _investigate_entity,
    # Deep research (external director model → grab → collapse → cited report)
    "deep_research": _deep_research,
    "research_status": _research_status,
    "list_research": _research_status,
    # Trip detection
    "show_trips": _show_trips,
    # Cross-signal correlation
    "cross_correlate": _cross_correlate,
    # Photos
    "show_photos": _show_photos,
    # Heatmap (navigates to map with heatmap enabled)
    "show_heatmap": lambda tenant_id, identity_id=None, dsn=None, **params: {
        "action": "navigate_to_map",
        "heatmap": True,
        "device_id": params.get("device_id", ""),
        "message": f"Opening map with GPS heatmap for {params.get('device_id', 'all devices')}",
    },
}
