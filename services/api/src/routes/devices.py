"""
Device pairing API — QR-based mobile device onboarding.

Auth-required (operator/admin actions):
  POST /devices/pair/generate   — Generate pairing QR payload
  GET  /devices                 — List paired devices
  GET  /devices/feed            — Device feed with latest signals
  GET  /devices/{device_id}     — Get device details
  PATCH /devices/{device_id}    — Update config (GPS interval, toggles)
  DELETE /devices/{device_id}   — Revoke device
  POST /devices/{device_id}/commands — Send command to device
  GET  /devices/{device_id}/commands — List commands for device

Public (device protocol — phone calls these with pairing token):
  POST /devices/pair/activate   — Phone activates after scanning QR
  GET  /devices/{device_id}/config — Phone polls for config updates
  POST /devices/{device_id}/commands/{id}/complete — Phone reports completion
"""

import json
import os
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from storage.signals.device_pairing import (
    generate_pairing_token,
    activate_device,
    list_devices,
    get_device,
    update_device_config,
    revoke_device,
    purge_device,
    get_pending_commands,
    queue_command,
    complete_command,
    list_commands,
    COMMAND_TYPES,
)
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

router = APIRouter(prefix="/devices", tags=["devices"])


def _server_url(request: Request) -> str:
    """Best guess at this server's public URL."""
    configured = os.environ.get("ENKI_PUBLIC_URL", "")
    if configured:
        return configured
    # Fall back to request host
    scheme = request.headers.get("x-forwarded-proto", "http")
    host = request.headers.get("x-forwarded-host", request.headers.get("host", "localhost:8000"))
    return f"{scheme}://{host}"


# ── Pairing ──

@router.post("/pair/generate")
async def generate_pair(request: Request, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Generate a pairing token + QR payload. Requires authentication."""
    body = await request.json()

    result = generate_pairing_token(
        tenant_id=auth.tenant_id,
        display_name=body.get("display_name", "My Phone"),
        device_type=body.get("device_type", "phone"),
        server_url=_server_url(request),
        gps_interval_s=body.get("gps_interval_s", 30),
        upload_photos=body.get("upload_photos", True),
        upload_health=body.get("upload_health", False),
    )
    return JSONResponse(result, status_code=201)


@router.post("/pair/activate")
async def activate_pair(request: Request):
    """Phone calls this after scanning QR. Public — uses pairing token.

    If the device sends a public_key, it will be stored for Ed25519/ECDSA
    signed request verification. This enables secure access through
    public tunnels (localhost).
    """
    body = await request.json()
    token = body.get("token", "")
    platform = body.get("platform", "unknown")
    public_key = body.get("public_key")  # base64 encoded
    public_key_algorithm = body.get("public_key_algorithm", "ECDSA_P256")

    if not token:
        return JSONResponse({"error": "token required"}, status_code=400)

    result = activate_device(token, platform)
    if result is None:
        return JSONResponse({"error": "invalid token"}, status_code=404)
    if "error" in result:
        return JSONResponse(result, status_code=400)

    # Store the device's public key for signed request verification
    if public_key and result.get("device_id"):
        try:
            from storage.postgres.db import get_connection
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE paired_devices SET
                            config = COALESCE(config, '{}'::jsonb) || %s::jsonb
                        WHERE device_id = %s
                    """, (
                        json.dumps({
                            "public_key": public_key,
                            "public_key_algorithm": public_key_algorithm,
                        }),
                        result["device_id"],
                    ))
                conn.commit()
        except Exception as e:
            pass  # Non-fatal — signing is optional upgrade

    return result


# ── Device Feed (monitoring view) — must be before /{device_id} routes ──

@router.get("/feed")
async def device_feed(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Get all devices with latest signal per type — single call for feed page."""
    from storage.signals.device_feed import get_device_feed
    devices = get_device_feed(auth.tenant_id)
    return {"devices": devices}


@router.get("/feed/{device_id}/{signal_type:path}")
async def device_type_history(device_id: str, signal_type: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Get signal history for a specific type on a specific device."""
    from storage.signals.device_feed import get_type_history
    limit = 20
    signals = get_type_history(auth.tenant_id, device_id, signal_type, limit)
    return {"signals": signals, "count": len(signals)}


# ── CRUD ──

@router.get("")
async def list_all(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """List all paired devices."""
    devices = list_devices(auth.tenant_id)
    return {"devices": devices, "count": len(devices)}


@router.get("/{device_id}")
async def get_one(device_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Get device details."""
    device = get_device(device_id, auth.tenant_id)
    if not device:
        return JSONResponse({"error": "not found"}, status_code=404)
    return device


@router.patch("/{device_id}")
async def update_config(device_id: str, request: Request, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Update device config (GPS interval, photo/health toggles)."""
    body = await request.json()
    updated = update_device_config(device_id, auth.tenant_id, body)
    if not updated:
        return JSONResponse({"error": "not found or not active"}, status_code=404)
    return {"updated": True, "device_id": device_id}


@router.delete("/{device_id}")
async def delete_device(device_id: str, request: Request, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Fully delete a device and all its signals."""
    purge = request.query_params.get("purge", "true")  # default to full purge
    if purge == "true":
        result = purge_device(device_id, auth.tenant_id)
        if not result["deleted"]:
            return JSONResponse({"error": "not found"}, status_code=404)
        return result
    else:
        revoked = revoke_device(device_id, auth.tenant_id)
        if not revoked:
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"revoked": True, "device_id": device_id}


@router.get("/{device_id}/config")
async def poll_config(request: Request, device_id: str):
    """Phone polls this to check for config changes AND pending commands.
    Public — part of the device protocol (phone uses pairing token)."""
    tenant_id = request.headers.get("x-tenant-id", "default")
    device = get_device(device_id, tenant_id)
    if not device or device["status"] != "active":
        return JSONResponse({"error": "not found or revoked"}, status_code=404)
    commands = get_pending_commands(device_id, tenant_id)
    return {"config": device["config"], "status": device["status"], "commands": commands}


# ─── Commands ────────────────────────────────────────────────────────────

@router.get("/command-types")
async def get_command_types():
    """List all available command types."""
    return {"command_types": COMMAND_TYPES}


@router.post("/{device_id}/commands")
async def send_command(device_id: str, request: Request, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Queue a remote command for a device. Requires authentication."""
    body = await request.json()
    cmd_type = body.get("type", "")
    params = body.get("params", {})

    if not cmd_type:
        return JSONResponse({"error": "type required"}, status_code=400)

    result = queue_command(device_id, auth.tenant_id, cmd_type, params)
    if "error" in result:
        return JSONResponse(result, status_code=400)

    return JSONResponse(result, status_code=201)


@router.get("/{device_id}/commands")
async def get_commands(device_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """List recent commands for a device."""
    commands = list_commands(device_id, auth.tenant_id)
    return {"commands": commands, "count": len(commands)}


@router.post("/{device_id}/commands/{command_id}/complete")
async def mark_complete(request: Request, device_id: str, command_id: str):
    """Phone reports command completion. Public — part of device protocol."""
    tenant_id = request.headers.get("x-tenant-id", "default")
    body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
    signal_id = body.get("signal_id")
    updated = complete_command(command_id, tenant_id, signal_id)
    if not updated:
        return JSONResponse({"error": "command not found"}, status_code=404)
    return {"completed": True, "command_id": command_id}
