"""
Geofence management endpoints.

GET    /geofences              — List geofences
POST   /geofences              — Create geofence (+ auto-create detection rule)
GET    /geofences/{geofence_id} — Get single geofence
PUT    /geofences/{geofence_id} — Update geofence
DELETE /geofences/{geofence_id} — Delete geofence
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

from storage.signals.geo import (
    list_geofences, get_geofence, create_geofence,
    update_geofence, delete_geofence,
)

router = APIRouter(prefix="/geofences", tags=["geofences"])


@router.get("")
async def get_geofences(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """List all geofences for this tenant."""
    fences = list_geofences(tenant_id=auth.tenant_id)
    return {"geofences": fences, "count": len(fences)}


@router.post("")
async def create_new_geofence(
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """
    Create a geofence and optionally auto-create a linked detection rule.

    Body: {
        "name": "Home Zone",
        "description": "Alert when entities enter this area",
        "polygon": [[-77.05, 38.89], [-77.03, 38.89], [-77.03, 38.91], [-77.05, 38.91]],
        "alert_on": "enter",
        "color": "#ef4444",
        "created_by": "admin"
    }

    polygon is a list of [longitude, latitude] pairs forming the boundary.
    """
    body = await request.json()

    name = body.get("name")
    polygon = body.get("polygon")
    created_by = body.get("created_by", "system")

    if not name:
        return JSONResponse(status_code=400, content={
            "error": "GEOFENCE.MISSING_NAME", "detail": "name is required"
        })
    if not polygon or len(polygon) < 3:
        return JSONResponse(status_code=400, content={
            "error": "GEOFENCE.INVALID_POLYGON",
            "detail": "polygon requires at least 3 coordinate pairs [[lon, lat], ...]"
        })

    result = create_geofence(
        tenant_id=auth.tenant_id,
        name=name,
        polygon=polygon,
        created_by=created_by,
        description=body.get("description"),
        alert_on=body.get("alert_on", "enter"),
        color=body.get("color", "#ef4444"),
        metadata=body.get("metadata"),
    )

    # Auto-create linked detection rule
    try:
        _create_geofence_rule(
            tenant_id=auth.tenant_id,
            geofence_id=result["geofence_id"],
            name=name,
            polygon=polygon,
            alert_on=body.get("alert_on", "enter"),
            created_by=created_by,
        )
    except Exception:
        pass  # rule creation is best-effort

    return JSONResponse(status_code=201, content=result)


@router.get("/{geofence_id}")
async def get_single_geofence(
    geofence_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Get a single geofence."""
    fence = get_geofence(geofence_id, auth.tenant_id)
    if not fence:
        return JSONResponse(status_code=404, content={
            "error": "GEOFENCE.NOT_FOUND", "detail": f"Geofence {geofence_id} not found"
        })
    return fence


@router.put("/{geofence_id}")
async def update_existing_geofence(
    geofence_id: str,
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Update a geofence."""
    body = await request.json()

    ok = update_geofence(
        geofence_id=geofence_id,
        tenant_id=auth.tenant_id,
        name=body.get("name"),
        description=body.get("description"),
        polygon=body.get("polygon"),
        alert_on=body.get("alert_on"),
        color=body.get("color"),
        enabled=body.get("enabled"),
    )

    if not ok:
        return JSONResponse(status_code=404, content={
            "error": "GEOFENCE.NOT_FOUND", "detail": f"Geofence {geofence_id} not found"
        })

    return {"status": "updated", "geofence_id": geofence_id}


@router.delete("/{geofence_id}")
async def delete_existing_geofence(
    geofence_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Delete a geofence."""
    ok = delete_geofence(geofence_id, auth.tenant_id)
    if not ok:
        return JSONResponse(status_code=404, content={
            "error": "GEOFENCE.NOT_FOUND", "detail": f"Geofence {geofence_id} not found"
        })
    return {"status": "deleted", "geofence_id": geofence_id}


def _create_geofence_rule(
    tenant_id: str,
    geofence_id: str,
    name: str,
    polygon: list[list[float]],
    alert_on: str,
    created_by: str,
):
    """Auto-create a detection rule linked to this geofence."""
    from storage.postgres.db import get_connection

    rule_id = f"rule_{geofence_id}"

    # Convert polygon to boundary format expected by detection engine
    # [[lon, lat], ...] → [[lat, lon], ...] for the ray-casting evaluator
    boundary = [[p[1], p[0]] for p in polygon]

    condition = {
        "boundary": boundary,
        "trigger": alert_on,
        "geofence_id": geofence_id,
    }

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO detection_rules
                    (rule_id, tenant_id, name, description, rule_type, enabled,
                     severity, condition, actions, created_by)
                VALUES (%s, %s, %s, %s, 'geofence', TRUE, 'medium', %s, '{}', %s)
                ON CONFLICT (rule_id) DO UPDATE SET
                    condition = EXCLUDED.condition,
                    name = EXCLUDED.name,
                    updated_at = NOW()
            """, (
                rule_id, tenant_id,
                f"Geofence: {name}",
                f"Auto-generated rule for geofence {geofence_id}",
                json.dumps(condition), created_by,
            ))
            conn.commit()

    # Link rule back to geofence
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE geofences SET rule_id = %s WHERE geofence_id = %s AND tenant_id = %s
            """, (rule_id, geofence_id, tenant_id))
            conn.commit()
