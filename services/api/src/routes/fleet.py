"""
Fleet Management — station heartbeat, update distribution, and remote monitoring.

Every Enki Station periodically checks in with the update registry, reports
its health, and checks for pending updates. Updates are signed by the vendor
key and require physical watch approval before applying.

PUBLIC (station-to-registry, authenticated by node signature):
  POST /fleet/heartbeat          — station reports status + checks for updates
  GET  /fleet/updates/available  — list pending updates for this station
  POST /fleet/updates/applied    — station reports an update was applied

ADMIN (fleet dashboard, requires admin auth):
  GET  /fleet/stations           — list all registered stations
  GET  /fleet/stations/{id}      — station detail + history
  POST /fleet/updates/publish    — publish an update to all stations
  GET  /fleet/updates            — list all published updates
  GET  /fleet/dashboard          — fleet overview (counts, health, versions)

The update flow:
  1. Vendor publishes update (signed package) via POST /fleet/updates/publish
  2. Stations check in via POST /fleet/heartbeat, receive pending updates
  3. Station queues update locally (does NOT apply yet)
  4. Operator plugs in watch → sees "UPDATE: ..." on e-paper → presses MENU
  5. Station applies update, signs a receipt with the watch
  6. Receipt sent back via POST /fleet/updates/applied
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo, require_role

logger = logging.getLogger("enki.fleet")
router = APIRouter(prefix="/fleet", tags=["fleet"])


# ─── Models ──────────────────────────────────────────────────────────────────

class HeartbeatRequest(BaseModel):
    node_id: str
    node_signature: Optional[str] = None  # Ed25519 signature of heartbeat payload
    gdu_version: str                       # e.g., "v6.0.0"
    firmware_version: Optional[str] = None # watch firmware version
    platform_version: str                  # e.g., "1.2.0"
    auth_mode: str                         # "full" or "dev"
    hardware_profile: str                  # "PRODUCTION" or "EMULATED"
    user_count: int = 0
    connector_count: int = 0
    signal_count: int = 0
    uptime_hours: float = 0
    health: str = "healthy"               # healthy, degraded, error
    health_details: Optional[dict] = None


class RegisterNodeRequest(BaseModel):
    node_id: str
    public_key: str                        # Ed25519 public key (base64)
    display_name: Optional[str] = None
    registration_token: str                # one-time token from hub approval
    api_endpoint: Optional[str] = None     # node's reachable URL (optional)


class PublishUpdateRequest(BaseModel):
    update_type: str              # "gdu_pack", "firmware", "security_patch", "config"
    version: str                  # e.g., "v7.0.0"
    title: str                    # "GDU v7 — entity vocabulary update"
    description: str              # what changed and why
    severity: str = "normal"      # normal, important, critical
    package_hash: Optional[str] = None  # SHA256 of the update package
    package_url: Optional[str] = None   # download URL (signed)
    requires_watch: bool = True   # must operator approve with watch?
    target_nodes: Optional[list[str]] = None  # null = all stations


# ─── Node Registration ──────────────────────────────────────────────────────

@router.post("/register")
async def register_node(body: RegisterNodeRequest):
    """Node registers with the hub using a one-time registration token.

    The token was generated when the hub admin approved the preregistration.
    On success, the node is activated and its public key is stored.
    """
    from storage.postgres.db import get_connection

    token_hash = hashlib.sha256(body.registration_token.encode()).hexdigest()
    now = datetime.now(timezone.utc)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Find the node registry entry with matching token hash
                cur.execute("""
                    SELECT node_id, owner_email, display_name, token_expires_at, status
                    FROM node_registry
                    WHERE registration_token_hash = %s
                """, (token_hash,))
                row = cur.fetchone()

                if not row:
                    return JSONResponse(status_code=403, content={
                        "error": "INVALID_TOKEN",
                        "detail": "Registration token not recognized",
                    })

                reg_node_id, owner_email, display_name, expires_at, status = row

                if status == "active":
                    return JSONResponse(status_code=409, content={
                        "error": "ALREADY_REGISTERED",
                        "detail": f"Node {reg_node_id} is already registered",
                    })

                if expires_at and now > expires_at:
                    return JSONResponse(status_code=410, content={
                        "error": "TOKEN_EXPIRED",
                        "detail": "Registration token has expired. Request a new one.",
                    })

                # Activate the node: store public key, clear token, set active
                cur.execute("""
                    UPDATE node_registry
                    SET status = 'active',
                        public_key = %s,
                        registration_token_hash = NULL,
                        token_expires_at = NULL,
                        last_heartbeat = %s,
                        health = 'healthy',
                        health_snapshot = %s
                    WHERE registration_token_hash = %s
                """, (
                    body.public_key,
                    now,
                    json.dumps({"registered_at": now.isoformat()}),
                    token_hash,
                ))

                # Retire the hub install companion for this preregistration —
                # the user's node is now alive, so the chat at /status/{token}
                # hands off to the node-side assistant.
                # No-op if the user never opened the chat (no install_conversations row).
                # See the install-assistant design notes → "Hub-side trigger".
                cur.execute("""
                    UPDATE install_conversations
                       SET retired_at = NOW()
                     WHERE retired_at IS NULL
                       AND status_token = (
                           SELECT status_token
                             FROM preregistrations
                            WHERE node_id = %s
                       )
                """, (reg_node_id,))

            conn.commit()

    except Exception as e:
        logger.error("Node registration failed: %s", e)
        return JSONResponse(status_code=500, content={"error": str(e)})

    logger.info("Node %s registered successfully for %s", reg_node_id, owner_email)

    return {
        "registered": True,
        "node_id": reg_node_id,
        "owner_email": owner_email,
        "display_name": display_name,
        "heartbeat_interval_seconds": 300,
        "hub_endpoint": os.getenv("ENKI_PUBLIC_URL", "https://example.com"),
    }


class AppliedUpdateRequest(BaseModel):
    node_id: str
    update_id: str
    applied_at: str
    watch_signature: Optional[str] = None  # signed by operator's watch
    previous_version: str
    new_version: str


# ─── Station Heartbeat ───────────────────────────────────────────────────────

@router.post("/heartbeat")
async def station_heartbeat(body: HeartbeatRequest):
    """Station checks in. Reports health, receives pending updates.

    No user auth required — stations authenticate with node_id + signature.
    In v1, we accept heartbeats by node_id for simplicity. Future: verify
    the node_signature against the station's registered public key.
    """
    from storage.postgres.db import get_connection

    now = datetime.now(timezone.utc)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Upsert station record
                cur.execute("""
                    INSERT INTO fleet_stations
                        (node_id, gdu_version, platform_version, firmware_version,
                         auth_mode, hardware_profile, user_count, connector_count,
                         signal_count, uptime_hours, health, health_details,
                         last_heartbeat, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (node_id) DO UPDATE SET
                        gdu_version = EXCLUDED.gdu_version,
                        platform_version = EXCLUDED.platform_version,
                        firmware_version = EXCLUDED.firmware_version,
                        auth_mode = EXCLUDED.auth_mode,
                        hardware_profile = EXCLUDED.hardware_profile,
                        user_count = EXCLUDED.user_count,
                        connector_count = EXCLUDED.connector_count,
                        signal_count = EXCLUDED.signal_count,
                        uptime_hours = EXCLUDED.uptime_hours,
                        health = EXCLUDED.health,
                        health_details = EXCLUDED.health_details,
                        last_heartbeat = EXCLUDED.last_heartbeat,
                        updated_at = EXCLUDED.updated_at
                """, (
                    body.node_id, body.gdu_version, body.platform_version,
                    body.firmware_version, body.auth_mode, body.hardware_profile,
                    body.user_count, body.connector_count, body.signal_count,
                    body.uptime_hours, body.health,
                    json.dumps(body.health_details) if body.health_details else None,
                    now, now, now,
                ))

                # Check for pending updates for this station
                cur.execute("""
                    SELECT update_id, update_type, version, title, description,
                           severity, package_hash, package_url, requires_watch, published_at
                    FROM fleet_updates
                    WHERE (target_nodes IS NULL OR %s = ANY(target_nodes))
                      AND update_id NOT IN (
                          SELECT update_id FROM fleet_update_receipts WHERE node_id = %s
                      )
                    ORDER BY published_at DESC
                """, (body.node_id, body.node_id))

                pending_updates = []
                for r in cur.fetchall():
                    pending_updates.append({
                        "update_id": r[0], "update_type": r[1], "version": r[2],
                        "title": r[3], "description": r[4], "severity": r[5],
                        "package_hash": r[6], "package_url": r[7],
                        "requires_watch": r[8],
                        "published_at": r[9].isoformat() if r[9] else None,
                    })

            conn.commit()

    except Exception as e:
        # If fleet tables don't exist yet, just acknowledge the heartbeat
        logger.debug("Fleet heartbeat storage failed (tables may not exist): %s", e)
        return {
            "acknowledged": True,
            "node_id": body.node_id,
            "pending_updates": [],
            "server_time": now.isoformat(),
        }

    return {
        "acknowledged": True,
        "node_id": body.node_id,
        "pending_updates": pending_updates,
        "pending_count": len(pending_updates),
        "server_time": now.isoformat(),
    }


# ─── Update Distribution ─────────────────────────────────────────────────────

@router.post("/updates/publish")
async def publish_update(
    body: PublishUpdateRequest,
    auth: UnifiedAuthInfo = Depends(require_role("admin")),
):
    """Publish an update to all stations (or targeted nodes).

    The update is queued — stations pick it up on their next heartbeat.
    Stations won't apply until the operator approves with the watch.
    """
    from storage.postgres.db import get_connection

    update_id = f"upd_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO fleet_updates
                        (update_id, update_type, version, title, description,
                         severity, package_hash, package_url, requires_watch,
                         target_nodes, published_by, published_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    update_id, body.update_type, body.version, body.title,
                    body.description, body.severity, body.package_hash,
                    body.package_url, body.requires_watch,
                    body.target_nodes, auth.subject_id, now,
                ))
            conn.commit()

    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

    target_desc = f"{len(body.target_nodes)} stations" if body.target_nodes else "all stations"
    logger.info("Update published: %s (%s) → %s", update_id, body.title, target_desc)

    return {
        "update_id": update_id,
        "title": body.title,
        "version": body.version,
        "severity": body.severity,
        "target": target_desc,
        "requires_watch": body.requires_watch,
        "message": f"Update queued for {target_desc}. Stations will receive it on next heartbeat.",
    }


@router.post("/updates/applied")
async def report_update_applied(body: AppliedUpdateRequest):
    """Station reports that an update was applied (with watch signature)."""
    from storage.postgres.db import get_connection

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO fleet_update_receipts
                        (node_id, update_id, applied_at, watch_signature,
                         previous_version, new_version, reported_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (node_id, update_id) DO NOTHING
                """, (
                    body.node_id, body.update_id, body.applied_at,
                    body.watch_signature, body.previous_version, body.new_version,
                ))
            conn.commit()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

    logger.info("Update %s applied on %s (v%s → v%s)",
                body.update_id, body.node_id, body.previous_version, body.new_version)

    return {"acknowledged": True, "node_id": body.node_id, "update_id": body.update_id}


# ─── Public Globe Data ───────────────────────────────────────────────────────

@router.get("/globe")
async def globe_data():
    """Public endpoint — returns anonymized node locations for the marketing globe.

    Returns only: lat, lng, health status. No node IDs, no emails, no details.
    Real registered nodes are merged in when available.
    """
    from storage.postgres.db import get_connection

    real_nodes = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Only return nodes that have opted in to globe visibility
                # via health_snapshot containing a "globe" field with lat/lng
                cur.execute("""
                    SELECT health, health_snapshot
                    FROM fleet_stations
                    WHERE last_heartbeat > NOW() - INTERVAL '1 hour'
                """)
                for r in cur.fetchall():
                    snapshot = r[1] if isinstance(r[1], dict) else {}
                    globe_loc = snapshot.get("globe")
                    if globe_loc and "lat" in globe_loc and "lng" in globe_loc:
                        real_nodes.append({
                            "lat": globe_loc["lat"],
                            "lng": globe_loc["lng"],
                            "health": r[0] or "healthy",
                            "real": True,
                        })
    except Exception:
        pass  # Tables may not exist on first run

    return {
        "real_nodes": real_nodes,
        "real_count": len(real_nodes),
    }


# ─── Fleet Dashboard ─────────────────────────────────────────────────────────

@router.get("/stations")
async def list_stations(auth: UnifiedAuthInfo = Depends(require_role("admin"))):
    """List all registered Enki Stations."""
    from storage.postgres.db import get_connection

    stations = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT node_id, gdu_version, platform_version, firmware_version,
                           auth_mode, hardware_profile, user_count, connector_count,
                           signal_count, uptime_hours, health, last_heartbeat, created_at
                    FROM fleet_stations
                    ORDER BY last_heartbeat DESC
                """)
                now = datetime.now(timezone.utc)
                for r in cur.fetchall():
                    last_hb = r[11]
                    age = (now - last_hb).total_seconds() if last_hb else None
                    stations.append({
                        "node_id": r[0],
                        "gdu_version": r[1],
                        "platform_version": r[2],
                        "firmware_version": r[3],
                        "auth_mode": r[4],
                        "hardware_profile": r[5],
                        "user_count": r[6],
                        "connector_count": r[7],
                        "signal_count": r[8],
                        "uptime_hours": r[9],
                        "health": r[10],
                        "last_heartbeat": last_hb.isoformat() if last_hb else None,
                        "online": age is not None and age < 600,  # 10 min threshold
                        "registered": r[12].isoformat() if r[12] else None,
                    })
    except Exception:
        pass

    return {"stations": stations, "count": len(stations)}


@router.get("/stations/{node_id}")
async def get_station(node_id: str, auth: UnifiedAuthInfo = Depends(require_role("admin"))):
    """Detailed view of a single station including update history."""
    from storage.postgres.db import get_connection

    station = None
    updates_applied = []

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT node_id, gdu_version, platform_version, firmware_version,
                           auth_mode, hardware_profile, user_count, connector_count,
                           signal_count, uptime_hours, health, health_details,
                           last_heartbeat, created_at
                    FROM fleet_stations WHERE node_id = %s
                """, (node_id,))
                r = cur.fetchone()
                if r:
                    station = {
                        "node_id": r[0], "gdu_version": r[1],
                        "platform_version": r[2], "firmware_version": r[3],
                        "auth_mode": r[4], "hardware_profile": r[5],
                        "user_count": r[6], "connector_count": r[7],
                        "signal_count": r[8], "uptime_hours": r[9],
                        "health": r[10],
                        "health_details": r[11] if isinstance(r[11], dict) else json.loads(r[11]) if r[11] else None,
                        "last_heartbeat": r[12].isoformat() if r[12] else None,
                        "registered": r[13].isoformat() if r[13] else None,
                    }

                # Update history
                cur.execute("""
                    SELECT r.update_id, u.title, u.update_type, u.version,
                           r.previous_version, r.new_version, r.applied_at,
                           r.watch_signature IS NOT NULL as watch_approved
                    FROM fleet_update_receipts r
                    JOIN fleet_updates u ON u.update_id = r.update_id
                    WHERE r.node_id = %s
                    ORDER BY r.applied_at DESC LIMIT 50
                """, (node_id,))
                for r in cur.fetchall():
                    updates_applied.append({
                        "update_id": r[0], "title": r[1], "update_type": r[2],
                        "version": r[3], "from_version": r[4], "to_version": r[5],
                        "applied_at": r[6], "watch_approved": r[7],
                    })
    except Exception:
        pass

    if not station:
        return JSONResponse(status_code=404, content={"error": "Station not found"})

    return {**station, "updates_applied": updates_applied}


@router.get("/dashboard")
async def fleet_dashboard(auth: UnifiedAuthInfo = Depends(require_role("admin"))):
    """Fleet overview — counts, health distribution, version distribution."""
    from storage.postgres.db import get_connection

    dashboard = {
        "total_stations": 0,
        "online": 0,
        "offline": 0,
        "healthy": 0,
        "degraded": 0,
        "error": 0,
        "versions": {},
        "hardware_modes": {"PRODUCTION": 0, "EMULATED": 0},
        "pending_updates": 0,
        "total_users": 0,
        "total_signals": 0,
    }

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                now = datetime.now(timezone.utc)
                threshold = now - timedelta(minutes=10)

                cur.execute("""
                    SELECT health, hardware_profile, gdu_version,
                           user_count, signal_count, last_heartbeat
                    FROM fleet_stations
                """)
                for r in cur.fetchall():
                    dashboard["total_stations"] += 1
                    online = r[5] and r[5] > threshold
                    if online:
                        dashboard["online"] += 1
                    else:
                        dashboard["offline"] += 1

                    health = r[0] or "unknown"
                    if health in dashboard:
                        dashboard[health] += 1

                    v = r[2] or "unknown"
                    dashboard["versions"][v] = dashboard["versions"].get(v, 0) + 1

                    hw = r[1] or "EMULATED"
                    if hw in dashboard["hardware_modes"]:
                        dashboard["hardware_modes"][hw] += 1

                    dashboard["total_users"] += r[3] or 0
                    dashboard["total_signals"] += r[4] or 0

                # Pending updates (published but not fully applied)
                cur.execute("SELECT COUNT(*) FROM fleet_updates")
                total_updates = cur.fetchone()[0]
                dashboard["total_updates_published"] = total_updates

    except Exception:
        pass

    return dashboard


@router.get("/updates")
async def list_updates(auth: UnifiedAuthInfo = Depends(require_role("admin"))):
    """List all published updates with application stats."""
    from storage.postgres.db import get_connection

    updates = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT u.update_id, u.update_type, u.version, u.title,
                           u.description, u.severity, u.requires_watch,
                           u.published_at, u.published_by,
                           COUNT(r.node_id) as applied_count
                    FROM fleet_updates u
                    LEFT JOIN fleet_update_receipts r ON r.update_id = u.update_id
                    GROUP BY u.update_id, u.update_type, u.version, u.title,
                             u.description, u.severity, u.requires_watch,
                             u.published_at, u.published_by
                    ORDER BY u.published_at DESC
                """)
                for r in cur.fetchall():
                    updates.append({
                        "update_id": r[0], "update_type": r[1], "version": r[2],
                        "title": r[3], "description": r[4], "severity": r[5],
                        "requires_watch": r[6],
                        "published_at": r[7].isoformat() if r[7] else None,
                        "published_by": r[8],
                        "applied_count": r[9],
                    })
    except Exception:
        pass

    return {"updates": updates, "count": len(updates)}


# ═══════════════════════════════════════════════════════════════════════════
# FEDERATION PHASE 1 — TOPIC PUBLISH/SUBSCRIBE
# ═══════════════════════════════════════════════════════════════════════════
#
# Nodes call POST /fleet/topics/publish when they admit a topic locally.
# The hub aggregates by canonical_topic_key — same key across nodes means
# same real-world topic. Lookups go through GET /fleet/topics/lookup.
#
# Phase 1 is identity-only: canonical_name, subtype, member_count, base60.
# No actual membership rows transit yet (that's Phase 2 with redaction).


class PublishTopicRequest(BaseModel):
    canonical_topic_key: str
    canonical_name: str
    subtype: Optional[str] = "investigation"
    base60_address: Optional[str] = None
    member_count: int = 0
    pack_versions: dict = {}
    node_id: Optional[str] = None
    local_topic_id: Optional[int] = None


@router.post("/topics/publish")
async def publish_topic(body: PublishTopicRequest, request: Request):
    """Receive a topic publication from a node and aggregate into the
    federated_topics registry. Idempotent on canonical_topic_key — multiple
    nodes publishing the same topic merge into one row with multiple
    publishing_nodes entries."""
    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(status_code=503, content={"ok": False, "error": "db_unavailable"})

    header_node = request.headers.get("X-Enki-Node-Id") or ""
    node_id = (body.node_id or header_node or "unknown").strip()[:128]
    now = datetime.now(timezone.utc)

    node_entry = {
        "node_id": node_id,
        "local_topic_id": body.local_topic_id,
        "base60_address": body.base60_address,
        "member_count": int(body.member_count or 0),
        "last_published_at": now.isoformat(),
    }

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT federation_id, publishing_nodes, pack_versions_seen
                         FROM federated_topics
                        WHERE canonical_topic_key = %s
                        FOR UPDATE""",
                    (body.canonical_topic_key,),
                )
                row = cur.fetchone()
                if row:
                    fed_id, nodes, pack_versions = row
                    nodes_list = [
                        n for n in (nodes or [])
                        if isinstance(n, dict) and n.get("node_id") != node_id
                    ]
                    nodes_list.append(node_entry)
                    new_total = sum(int(n.get("member_count") or 0) for n in nodes_list)
                    merged_packs = dict(pack_versions or {})
                    merged_packs.update(body.pack_versions or {})
                    cur.execute(
                        """UPDATE federated_topics
                              SET publishing_nodes = %s::jsonb,
                                  network_member_count = %s,
                                  pack_versions_seen = %s::jsonb,
                                  last_updated_at = NOW(),
                                  canonical_name = %s,
                                  subtype = %s
                            WHERE federation_id = %s""",
                        (json.dumps(nodes_list), new_total,
                         json.dumps(merged_packs),
                         body.canonical_name, body.subtype, fed_id),
                    )
                    action = "merged"
                else:
                    cur.execute(
                        """INSERT INTO federated_topics (
                             canonical_topic_key, canonical_name, subtype,
                             publishing_nodes, network_member_count,
                             pack_versions_seen
                           ) VALUES (%s, %s, %s, %s::jsonb, %s, %s::jsonb)""",
                        (body.canonical_topic_key, body.canonical_name,
                         body.subtype, json.dumps([node_entry]),
                         int(body.member_count or 0),
                         json.dumps(body.pack_versions or {})),
                    )
                    action = "created"
            conn.commit()
    except Exception as e:
        logger.exception("federation publish failed for key %s: %s",
                         body.canonical_topic_key, e)
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)[:200]})

    # Update node trust: a successful publication is +1 total_publications.
    # Whether it counts as corroboration depends on what's already in the
    # registry — if this node's publication merged with existing entries
    # (action='merged'), that's mutual corroboration with the prior nodes.
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                corroboration_delta = 1 if action == "merged" else 0
                cur.execute(
                    """INSERT INTO federation_node_trust (
                         node_id, total_publications, corroboration_count
                       ) VALUES (%s, 1, %s)
                       ON CONFLICT (node_id) DO UPDATE SET
                         total_publications = federation_node_trust.total_publications + 1,
                         corroboration_count = federation_node_trust.corroboration_count + %s,
                         trust_score = LEAST(1.0, GREATEST(0.0,
                           0.5
                           + (federation_node_trust.corroboration_count + %s - federation_node_trust.contradiction_count)::REAL
                             / GREATEST(federation_node_trust.total_publications + 1, 1)::REAL * 0.5
                         )),
                         last_updated_at = NOW()""",
                    (node_id, corroboration_delta, corroboration_delta, corroboration_delta),
                )
            conn.commit()
    except Exception:
        # Trust update is best-effort; don't fail the publication on it
        logger.debug("trust update failed for node %s", node_id)

    return {"ok": True, "action": action,
            "canonical_topic_key": body.canonical_topic_key}


def _decorate_with_trust(nodes: list[dict]) -> tuple[list[dict], float]:
    """Pull trust scores from federation_node_trust for each contributing
    node. Returns (decorated_nodes, trust_weighted_sum). The trust score
    floats with corroboration history; unknown nodes get the innocent
    prior (0.5)."""
    if not nodes:
        return [], 0.0
    node_ids = [n.get("node_id") for n in nodes if isinstance(n, dict) and n.get("node_id")]
    if not node_ids:
        return nodes, float(len(nodes))
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT node_id, trust_score
                         FROM federation_node_trust
                        WHERE node_id = ANY(%s)""",
                    (node_ids,),
                )
                trust_map = {r[0]: float(r[1] or 0.5) for r in cur.fetchall()}
    except Exception:
        trust_map = {}
    weighted_sum = 0.0
    decorated = []
    for n in nodes:
        if not isinstance(n, dict):
            decorated.append(n)
            continue
        nid = n.get("node_id") or "unknown"
        t = trust_map.get(nid, 0.5)  # innocent prior
        nn = dict(n)
        nn["trust_score"] = t
        decorated.append(nn)
        weighted_sum += t
    return decorated, weighted_sum


@router.get("/topics/lookup")
async def lookup_topic(canonical: str, request: Request):
    """Look up a topic by canonical_topic_key. Returns the network's view
    + trust-weighted aggregation (each contributing node's signal is
    multiplied by its trust_score)."""
    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(status_code=503, content={"ok": False, "error": "db_unavailable"})

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT federation_id, canonical_name, subtype,
                              publishing_nodes, network_member_count,
                              pack_versions_seen, first_published_at,
                              last_updated_at
                         FROM federated_topics
                        WHERE canonical_topic_key = %s""",
                    (canonical,),
                )
                row = cur.fetchone()
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)[:200]})

    if not row:
        return {"found": False, "canonical_topic_key": canonical}
    decorated_nodes, trust_weight = _decorate_with_trust(row[3] or [])
    return {
        "found": True,
        "canonical_topic_key": canonical,
        "canonical_name": row[1],
        "subtype": row[2],
        "publishing_nodes": decorated_nodes,
        "n_nodes": len(decorated_nodes),
        "trust_weighted_n_nodes": round(trust_weight, 2),
        "network_member_count": int(row[4] or 0),
        "pack_versions_seen": row[5] or {},
        "first_published_at": row[6].isoformat() if row[6] else None,
        "last_updated_at": row[7].isoformat() if row[7] else None,
    }


# ─── Federation Phase 4 — trust, redirects, conflicts ─────────────────────


class PublishRedirectRequest(BaseModel):
    origin_canonical_key: str
    target_canonical_key: str
    reason: str = "fission_rebind"
    originating_node_id: Optional[str] = None
    audit_payload: dict = {}


@router.post("/entities/redirects/publish")
async def publish_entity_redirect(body: PublishRedirectRequest, request: Request):
    """Hub receives a fission/rebind decision. Other nodes pull this feed."""
    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(status_code=503, content={"ok": False, "error": "db_unavailable"})

    header_node = request.headers.get("X-Enki-Node-Id") or ""
    node_id = (body.originating_node_id or header_node or "unknown").strip()[:128]

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO federation_entity_redirects (
                         origin_canonical_key, target_canonical_key,
                         reason, originating_node_id, audit_payload
                       ) VALUES (%s, %s, %s, %s, %s::jsonb)
                       ON CONFLICT (origin_canonical_key, target_canonical_key)
                       DO UPDATE SET applied_at = NOW(),
                                     reason = EXCLUDED.reason,
                                     audit_payload = EXCLUDED.audit_payload""",
                    (body.origin_canonical_key, body.target_canonical_key,
                     body.reason, node_id, json.dumps(body.audit_payload)),
                )
            conn.commit()
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)[:200]})

    return {"ok": True, "origin_canonical_key": body.origin_canonical_key,
            "target_canonical_key": body.target_canonical_key}


@router.get("/entities/redirects/recent")
async def fetch_recent_redirects(seconds: int = 3600):
    """Pull recent entity redirects for subscriber nodes."""
    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(status_code=503, content={"ok": False, "error": "db_unavailable"})

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT origin_canonical_key, target_canonical_key,
                              reason, originating_node_id, applied_at
                         FROM federation_entity_redirects
                        WHERE applied_at > NOW() - INTERVAL '1 second' * %s
                        ORDER BY applied_at DESC LIMIT 200""",
                    (seconds,),
                )
                rows = cur.fetchall()
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)[:200]})

    return {
        "redirects": [
            {
                "origin_canonical_key": r[0],
                "target_canonical_key": r[1],
                "reason": r[2],
                "originating_node_id": r[3],
                "applied_at": r[4].isoformat() if r[4] else None,
            }
            for r in rows
        ],
        "count": len(rows),
    }


class PublishConflictRequest(BaseModel):
    canonical_entity_key: str
    property_key: str
    value_a: str
    value_b: str
    node_id: Optional[str] = None


@router.post("/conflicts/publish")
async def publish_conflict(body: PublishConflictRequest, request: Request):
    """Hub receives a node's conflict detection. Aggregates contributors."""
    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(status_code=503, content={"ok": False, "error": "db_unavailable"})

    header_node = request.headers.get("X-Enki-Node-Id") or ""
    node_id = (body.node_id or header_node or "unknown").strip()[:128]
    now = datetime.now(timezone.utc).isoformat()
    # Normalize value_a/value_b so order doesn't double-count
    va, vb = sorted([body.value_a, body.value_b])

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT conflict_id, contributing_nodes
                         FROM federation_conflicts
                        WHERE canonical_entity_key = %s
                          AND property_key = %s
                          AND value_a = %s AND value_b = %s
                        FOR UPDATE""",
                    (body.canonical_entity_key, body.property_key, va, vb),
                )
                row = cur.fetchone()
                contributor = {"node_id": node_id, "last_seen": now}
                if row:
                    conflict_id, nodes = row
                    nodes_list = [
                        n for n in (nodes or [])
                        if isinstance(n, dict) and n.get("node_id") != node_id
                    ]
                    nodes_list.append(contributor)
                    cur.execute(
                        """UPDATE federation_conflicts
                              SET contributing_nodes = %s::jsonb,
                                  last_updated_at = NOW()
                            WHERE conflict_id = %s""",
                        (json.dumps(nodes_list), conflict_id),
                    )
                else:
                    cur.execute(
                        """INSERT INTO federation_conflicts (
                             canonical_entity_key, property_key,
                             value_a, value_b, contributing_nodes
                           ) VALUES (%s, %s, %s, %s, %s::jsonb)""",
                        (body.canonical_entity_key, body.property_key, va, vb,
                         json.dumps([contributor])),
                    )
            conn.commit()
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)[:200]})

    return {"ok": True}


@router.get("/conflicts/entity")
async def conflicts_for_entity(canonical: str):
    """Get all conflicts the network knows about for an entity."""
    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(status_code=503, content={"ok": False, "error": "db_unavailable"})

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT property_key, value_a, value_b,
                              jsonb_array_length(contributing_nodes),
                              last_updated_at
                         FROM federation_conflicts
                        WHERE canonical_entity_key = %s
                        ORDER BY last_updated_at DESC LIMIT 50""",
                    (canonical,),
                )
                rows = cur.fetchall()
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)[:200]})

    return {
        "conflicts": [
            {
                "property_key": r[0],
                "value_a": r[1],
                "value_b": r[2],
                "n_nodes": int(r[3] or 0),
                "last_updated_at": r[4].isoformat() if r[4] else None,
            }
            for r in rows
        ],
        "count": len(rows),
    }


@router.get("/nodes/trust")
async def get_node_trust(node_id: str):
    """Return a node's trust score. Default 0.5 if never seen."""
    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(status_code=503, content={"ok": False, "error": "db_unavailable"})

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT trust_score, total_publications,
                              corroboration_count, contradiction_count
                         FROM federation_node_trust WHERE node_id = %s""",
                    (node_id,),
                )
                row = cur.fetchone()
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)[:200]})

    if not row:
        return {"node_id": node_id, "trust_score": 0.5, "innocent_prior": True}
    return {
        "node_id": node_id,
        "trust_score": float(row[0] or 0.5),
        "total_publications": int(row[1] or 0),
        "corroboration_count": int(row[2] or 0),
        "contradiction_count": int(row[3] or 0),
    }


@router.get("/nodes/trust/leaderboard")
async def trust_leaderboard(limit: int = 20):
    """Top nodes by trust score."""
    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(status_code=503, content={"ok": False, "error": "db_unavailable"})

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT node_id, trust_score, total_publications,
                              corroboration_count, contradiction_count
                         FROM federation_node_trust
                        ORDER BY trust_score DESC, total_publications DESC
                        LIMIT %s""",
                    (limit,),
                )
                rows = cur.fetchall()
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)[:200]})

    return {
        "nodes": [
            {
                "node_id": r[0],
                "trust_score": float(r[1] or 0.5),
                "total_publications": int(r[2] or 0),
                "corroboration_count": int(r[3] or 0),
                "contradiction_count": int(r[4] or 0),
            }
            for r in rows
        ],
        "count": len(rows),
    }


# ─── Federation Phase 3 — selective membership sharing ───────────────────


class PublishTopicMembersRequest(BaseModel):
    canonical_topic_key: str
    node_id: Optional[str] = None
    policy: Optional[str] = "full"   # node-declared policy under which this batch is shared
    members: list[dict] = []   # [{canonical_entity_key, score}]


class PublishTopicMemberCountsRequest(BaseModel):
    canonical_topic_key: str
    node_id: Optional[str] = None
    policy: Optional[str] = "anonymized"
    counts: list[dict] = []    # [{member_type, count}]


class RetractTopicMembersRequest(BaseModel):
    canonical_topic_key: str
    node_id: Optional[str] = None
    reason: Optional[str] = None   # e.g. "policy_changed_to_none"


@router.post("/topics/members/publish")
async def publish_topic_members(body: PublishTopicMembersRequest, request: Request):
    """Receive a topic's membership rows from a node. Aggregates per
    (canonical_topic_key, canonical_entity_key) into federated_topic_members."""
    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(status_code=503, content={"ok": False, "error": "db_unavailable"})

    header_node = request.headers.get("X-Enki-Node-Id") or ""
    node_id = (body.node_id or header_node or "unknown").strip()[:128]
    now = datetime.now(timezone.utc).isoformat()
    # The node declares the policy under which it's sharing this batch.
    # Identity rows are only ever returned by the lookup endpoint when
    # the originating node's most-recent policy is 'full'.
    policy = (body.policy or "full").lower().strip()[:32]

    if not body.members:
        return {"ok": True, "rows_upserted": 0}

    rows_upserted = 0
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                for m in body.members:
                    ek = m.get("canonical_entity_key")
                    score = float(m.get("score") or 1.0)
                    if not ek:
                        continue
                    # Upsert + node contribution
                    cur.execute(
                        """SELECT membership_id, contributing_nodes
                             FROM federated_topic_members
                            WHERE canonical_topic_key = %s
                              AND canonical_entity_key = %s
                            FOR UPDATE""",
                        (body.canonical_topic_key, ek),
                    )
                    existing = cur.fetchone()
                    if existing:
                        mem_id, nodes = existing
                        nodes_list = [
                            n for n in (nodes or [])
                            if isinstance(n, dict) and n.get("node_id") != node_id
                        ]
                        nodes_list.append({"node_id": node_id, "score": score,
                                           "policy": policy, "last_seen": now})
                        agg = sum(float(n.get("score") or 0) for n in nodes_list)
                        cur.execute(
                            """UPDATE federated_topic_members
                                  SET contributing_nodes = %s::jsonb,
                                      aggregate_score = %s,
                                      last_updated_at = NOW()
                                WHERE membership_id = %s""",
                            (json.dumps(nodes_list), agg, mem_id),
                        )
                    else:
                        cur.execute(
                            """INSERT INTO federated_topic_members (
                                 canonical_topic_key, canonical_entity_key,
                                 contributing_nodes, aggregate_score
                               ) VALUES (%s, %s, %s::jsonb, %s)""",
                            (body.canonical_topic_key, ek,
                             json.dumps([{"node_id": node_id, "score": score,
                                          "policy": policy, "last_seen": now}]),
                             score),
                        )
                    rows_upserted += 1
            conn.commit()
    except Exception as e:
        logger.exception("membership publish failed for topic %s",
                         body.canonical_topic_key)
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)[:200]})

    return {"ok": True, "rows_upserted": rows_upserted,
            "canonical_topic_key": body.canonical_topic_key}


@router.post("/topics/members/publish_counts")
async def publish_topic_member_counts(body: PublishTopicMemberCountsRequest,
                                        request: Request):
    """Anonymized membership — per-type counts only, no identities."""
    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(status_code=503, content={"ok": False, "error": "db_unavailable"})

    header_node = request.headers.get("X-Enki-Node-Id") or ""
    node_id = (body.node_id or header_node or "unknown").strip()[:128]

    rows = 0
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                for c in (body.counts or []):
                    t = (c.get("member_type") or "unknown").lower()[:64]
                    n = int(c.get("count") or 0)
                    cur.execute(
                        """INSERT INTO federated_topic_counts
                              (canonical_topic_key, contributing_node_id,
                               member_type, count, last_updated_at)
                            VALUES (%s, %s, %s, %s, NOW())
                            ON CONFLICT (canonical_topic_key, contributing_node_id, member_type)
                            DO UPDATE SET count = EXCLUDED.count,
                                          last_updated_at = NOW()""",
                        (body.canonical_topic_key, node_id, t, n),
                    )
                    rows += 1
            conn.commit()
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)[:200]})

    return {"ok": True, "rows_upserted": rows}


@router.get("/topics/{canonical_topic_key}/members")
async def get_topic_network_members(canonical_topic_key: str, limit: int = 50):
    """Network view of a topic's members. Redaction-aware: a contribution
    only appears here when the originating node's most recent published
    policy was 'full'. Nodes that have since retracted or downgraded
    (policy = 'none' / 'anonymized') are filtered out server-side, even
    if their historical identity rows remain in the table.

    A member row is suppressed entirely if no remaining contribution
    qualifies under 'full'."""
    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(status_code=503, content={"ok": False, "error": "db_unavailable"})

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT canonical_entity_key, contributing_nodes,
                              aggregate_score, last_updated_at
                         FROM federated_topic_members
                        WHERE canonical_topic_key = %s
                        ORDER BY aggregate_score DESC NULLS LAST,
                                 last_updated_at DESC
                        LIMIT %s""",
                    (canonical_topic_key, limit * 4),  # over-fetch to survive filtering
                )
                raw_rows = cur.fetchall()

                # Try to attach friendly names if federated_entities knows them
                ekeys = [r[0] for r in raw_rows]
                names: dict[str, tuple] = {}
                if ekeys:
                    cur.execute(
                        """SELECT canonical_entity_key, canonical_name, canonical_type
                             FROM federated_entities
                            WHERE canonical_entity_key = ANY(%s)""",
                        (ekeys,),
                    )
                    for ek, n, t in cur.fetchall():
                        names[ek] = (n, t)

                # Anonymized counts (if any)
                cur.execute(
                    """SELECT member_type, SUM(count) AS total,
                              COUNT(DISTINCT contributing_node_id) AS contributors
                         FROM federated_topic_counts
                        WHERE canonical_topic_key = %s
                        GROUP BY member_type""",
                    (canonical_topic_key,),
                )
                anon_counts = [
                    {"member_type": r[0], "total_count": int(r[1] or 0),
                     "contributing_nodes": int(r[2] or 0)}
                    for r in cur.fetchall()
                ]
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)[:200]})

    items = []
    redacted = 0
    for ek, nodes_json, score, last_updated in raw_rows:
        # Each contribution carries its policy snapshot. A missing 'policy'
        # field is historical (pre-Phase 5c) — treat as 'full' for backward
        # compatibility; otherwise only 'full' qualifies for identity exposure.
        full_contribs = [
            n for n in (nodes_json or [])
            if isinstance(n, dict)
            and (n.get("policy") or "full").lower() == "full"
        ]
        if not full_contribs:
            redacted += 1
            continue
        # Recompute aggregate from only qualifying contributors
        agg = sum(float(n.get("score") or 0) for n in full_contribs)
        name, etype = names.get(ek, (None, None))
        items.append({
            "canonical_entity_key": ek,
            "canonical_name": name,
            "canonical_type": etype,
            "n_nodes": len(full_contribs),
            "aggregate_score": agg,
            "last_updated_at": last_updated.isoformat() if last_updated else None,
        })
        if len(items) >= limit:
            break

    items.sort(key=lambda x: (-x["aggregate_score"], -x["n_nodes"]))
    return {
        "canonical_topic_key": canonical_topic_key,
        "members": items,
        "members_count": len(items),
        "redacted_count": redacted,
        "anonymized_counts": anon_counts,
    }


@router.post("/topics/members/retract")
async def retract_topic_members(body: RetractTopicMembersRequest, request: Request):
    """Node explicitly removes its prior identity contributions for a
    topic — called by the publisher when policy tightens to 'none' or
    'anonymized'. Empties this node's entries from contributing_nodes;
    deletes the member row if no contributors remain."""
    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(status_code=503, content={"ok": False, "error": "db_unavailable"})

    header_node = request.headers.get("X-Enki-Node-Id") or ""
    node_id = (body.node_id or header_node or "unknown").strip()[:128]

    rows_updated = rows_deleted = 0
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT membership_id, contributing_nodes
                         FROM federated_topic_members
                        WHERE canonical_topic_key = %s
                        FOR UPDATE""",
                    (body.canonical_topic_key,),
                )
                for mem_id, nodes in cur.fetchall():
                    remaining = [
                        n for n in (nodes or [])
                        if isinstance(n, dict) and n.get("node_id") != node_id
                    ]
                    if len(remaining) == len(nodes or []):
                        continue  # this node wasn't a contributor
                    if not remaining:
                        cur.execute(
                            "DELETE FROM federated_topic_members WHERE membership_id = %s",
                            (mem_id,),
                        )
                        rows_deleted += 1
                    else:
                        agg = sum(float(n.get("score") or 0) for n in remaining)
                        cur.execute(
                            """UPDATE federated_topic_members
                                  SET contributing_nodes = %s::jsonb,
                                      aggregate_score = %s,
                                      last_updated_at = NOW()
                                WHERE membership_id = %s""",
                            (json.dumps(remaining), agg, mem_id),
                        )
                        rows_updated += 1
            conn.commit()
    except Exception as e:
        logger.exception("retract failed for topic %s", body.canonical_topic_key)
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)[:200]})

    return {
        "ok": True,
        "canonical_topic_key": body.canonical_topic_key,
        "node_id": node_id,
        "rows_updated": rows_updated,
        "rows_deleted": rows_deleted,
        "reason": body.reason,
    }


# ─── Federation Phase 2 — entity identity ─────────────────────────────────

class PublishEntityRequest(BaseModel):
    canonical_entity_key: str
    canonical_name: str
    canonical_type: Optional[str] = None
    base60_address: Optional[str] = None
    signal_count: int = 0
    pack_versions: dict = {}
    node_id: Optional[str] = None
    local_entity_id: Optional[int] = None


@router.post("/entities/publish")
async def publish_entity(body: PublishEntityRequest, request: Request):
    """Receive an entity publication from a node. Aggregates by
    canonical_entity_key (same key across nodes = same real-world entity)."""
    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(status_code=503, content={"ok": False, "error": "db_unavailable"})

    header_node = request.headers.get("X-Enki-Node-Id") or ""
    node_id = (body.node_id or header_node or "unknown").strip()[:128]
    now = datetime.now(timezone.utc)

    node_entry = {
        "node_id": node_id,
        "local_entity_id": body.local_entity_id,
        "base60_address": body.base60_address,
        "signal_count": int(body.signal_count or 0),
        "last_published_at": now.isoformat(),
    }

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT federation_id, publishing_nodes, pack_versions_seen
                         FROM federated_entities
                        WHERE canonical_entity_key = %s FOR UPDATE""",
                    (body.canonical_entity_key,),
                )
                row = cur.fetchone()
                if row:
                    fed_id, nodes, pack_versions = row
                    nodes_list = [
                        n for n in (nodes or [])
                        if isinstance(n, dict) and n.get("node_id") != node_id
                    ]
                    nodes_list.append(node_entry)
                    total_signals = sum(int(n.get("signal_count") or 0) for n in nodes_list)
                    merged_packs = dict(pack_versions or {})
                    merged_packs.update(body.pack_versions or {})
                    cur.execute(
                        """UPDATE federated_entities
                              SET publishing_nodes = %s::jsonb,
                                  network_signal_count = %s,
                                  pack_versions_seen = %s::jsonb,
                                  last_updated_at = NOW(),
                                  canonical_name = %s,
                                  canonical_type = %s
                            WHERE federation_id = %s""",
                        (json.dumps(nodes_list), total_signals,
                         json.dumps(merged_packs),
                         body.canonical_name, body.canonical_type, fed_id),
                    )
                    action = "merged"
                else:
                    cur.execute(
                        """INSERT INTO federated_entities (
                             canonical_entity_key, canonical_name, canonical_type,
                             publishing_nodes, network_signal_count,
                             pack_versions_seen
                           ) VALUES (%s, %s, %s, %s::jsonb, %s, %s::jsonb)""",
                        (body.canonical_entity_key, body.canonical_name,
                         body.canonical_type, json.dumps([node_entry]),
                         int(body.signal_count or 0),
                         json.dumps(body.pack_versions or {})),
                    )
                    action = "created"
            conn.commit()
    except Exception as e:
        logger.exception("federation entity publish failed for key %s: %s",
                         body.canonical_entity_key, e)
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)[:200]})

    return {"ok": True, "action": action,
            "canonical_entity_key": body.canonical_entity_key}


@router.get("/entities/lookup")
async def lookup_entity(canonical: str):
    """Look up an entity by canonical_entity_key. Returns the network's view."""
    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(status_code=503, content={"ok": False, "error": "db_unavailable"})

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT federation_id, canonical_name, canonical_type,
                              publishing_nodes, network_signal_count,
                              pack_versions_seen, first_published_at,
                              last_updated_at
                         FROM federated_entities
                        WHERE canonical_entity_key = %s""",
                    (canonical,),
                )
                row = cur.fetchone()
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)[:200]})

    if not row:
        return {"found": False, "canonical_entity_key": canonical}
    decorated_nodes, trust_weight = _decorate_with_trust(row[3] or [])
    return {
        "found": True,
        "canonical_entity_key": canonical,
        "canonical_name": row[1],
        "canonical_type": row[2],
        "publishing_nodes": decorated_nodes,
        "n_nodes": len(decorated_nodes),
        "trust_weighted_n_nodes": round(trust_weight, 2),
        "network_signal_count": int(row[4] or 0),
        "pack_versions_seen": row[5] or {},
        "first_published_at": row[6].isoformat() if row[6] else None,
        "last_updated_at": row[7].isoformat() if row[7] else None,
    }


@router.get("/entities/network")
async def network_entity_summary(limit: int = 20):
    """Top entities across the network by node count + signal count."""
    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(status_code=503, content={"ok": False, "error": "db_unavailable"})

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT canonical_entity_key, canonical_name, canonical_type,
                              jsonb_array_length(publishing_nodes) AS n_nodes,
                              network_signal_count, last_updated_at
                         FROM federated_entities
                        ORDER BY n_nodes DESC, network_signal_count DESC NULLS LAST
                        LIMIT %s""",
                    (limit,),
                )
                rows = cur.fetchall()
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)[:200]})

    items = [
        {
            "canonical_entity_key": r[0],
            "canonical_name": r[1],
            "canonical_type": r[2],
            "n_nodes": int(r[3] or 0),
            "network_signal_count": int(r[4] or 0),
            "last_updated_at": r[5].isoformat() if r[5] else None,
        }
        for r in rows
    ]
    return {"entities": items, "count": len(items)}


@router.get("/topics/network")
async def network_topic_summary(limit: int = 20):
    """Top topics across the network by node count + member count. Useful
    for marketing-page 'topics the network knows about' rolling display."""
    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(status_code=503, content={"ok": False, "error": "db_unavailable"})

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT canonical_topic_key, canonical_name, subtype,
                              jsonb_array_length(publishing_nodes) AS n_nodes,
                              network_member_count, last_updated_at
                         FROM federated_topics
                        ORDER BY n_nodes DESC, network_member_count DESC NULLS LAST
                        LIMIT %s""",
                    (limit,),
                )
                rows = cur.fetchall()
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)[:200]})

    items = [
        {
            "canonical_topic_key": r[0],
            "canonical_name": r[1],
            "subtype": r[2],
            "n_nodes": int(r[3] or 0),
            "network_member_count": int(r[4] or 0),
            "last_updated_at": r[5].isoformat() if r[5] else None,
        }
        for r in rows
    ]
    return {"topics": items, "count": len(items)}


# ─── Public Library subscription network (May 20 plan, Stage D) ────────────
#
# A library can opt itself into a public pool. Other ENKI nodes
# subscribe and receive document-processing work assignments. Each
# completed assignment moves yield_score on the subscriber. See
# 089_public_libraries.sql / 090_library_work_assignments.sql.


class LibrarySubscribeRequest(BaseModel):
    node_id: str
    max_docs_per_day: Optional[int] = None
    notes: Optional[str] = None


@router.get("/libraries/public")
async def list_public_libraries(limit: int = 100):
    """Catalog of libraries that have opted into public subscription."""
    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(status_code=503,
                            content={"ok": False, "error": "db_unavailable"})

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT library_id, canonical_name, library_subtype,
                           focal_term, description,
                           subscription_policy, min_yield_required,
                           reference_count, document_count, entity_count,
                           last_updated_at
                    FROM libraries
                    WHERE is_public = TRUE
                    ORDER BY last_updated_at DESC NULLS LAST
                    LIMIT %s
                    """,
                    (max(1, min(limit, 500)),),
                )
                rows = cur.fetchall()
    except Exception as e:
        return JSONResponse(status_code=500,
                            content={"ok": False, "error": str(e)[:200]})

    items = [
        {
            "library_id": int(r[0]),
            "canonical_name": r[1],
            "library_subtype": r[2],
            "focal_term": r[3],
            "description": r[4],
            "subscription_policy": r[5],
            "min_yield_required": float(r[6] or 0.0),
            "reference_count": int(r[7] or 0),
            "document_count": int(r[8] or 0),
            "entity_count": int(r[9] or 0),
            "last_updated_at": r[10].isoformat() if r[10] else None,
        }
        for r in rows
    ]
    return {"libraries": items, "count": len(items)}


# ── Cross-node extraction-result sharing (Batch B of federation) ───────────
# These three endpoints let nodes publish + discover + serve extraction
# envelopes. The point: when N nodes have the same document (matching
# content_sha256), only ONE needs to run the LLM. The others pull the
# producer's envelope and apply the rows locally.
#
# Endpoint roles:
#   POST /fleet/extractions/publish              (hub-side; producer announces)
#   GET  /fleet/extractions/find                 (hub-side; subscriber queries)
#   POST /fleet/extractions/store/{...}          (hub-side; producer uploads bytes — Phase 9)
#   GET  /fleet/extractions/serve/{...}          (hub-side; subscriber fallback when producer unreachable)
#   GET  /p2p/extraction/{sha}/{fp}              (node-side; producer serves bytes — see p2p.py)


@router.post("/extractions/publish")
async def publish_extraction(body: dict):
    """Producer node announces it has extracted (content_sha, profile) →
    envelope_sha. Subscriber nodes will then look this up via
    /fleet/extractions/find and pull the actual envelope from the
    producer's /p2p/extraction/ endpoint.

    Request body:
        {
          "content_sha256":      "<64-char hex>",
          "profile_fingerprint": "<profile id, e.g. legacy_v1 or sha256>",
          "producer_node_id":    "node:abc123...",
          "result_envelope_sha": "<64-char hex>",
          "result_row_counts":   {"mentions": N, "statements": M, ...},
          "produced_at":         "2026-05-25T18:00:00Z",
          "signature":           "<optional Ed25519 b64>"
        }

    Idempotent — UPSERT on (content_sha256, profile_fingerprint,
    producer_node_id). Republishing replaces row_counts + envelope_sha,
    typical when a producer re-extracts with the same profile.
    """
    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(status_code=503,
                            content={"ok": False, "error": "db_unavailable"})

    content_sha = (body.get("content_sha256") or "").strip()
    profile_fp = (body.get("profile_fingerprint") or "").strip()
    producer = (body.get("producer_node_id") or "").strip()
    envelope_sha = (body.get("result_envelope_sha") or "").strip()
    row_counts = body.get("result_row_counts") or {}
    produced_at = body.get("produced_at")
    signature = body.get("signature")

    if not all([content_sha, profile_fp, producer, envelope_sha]):
        return JSONResponse(status_code=400, content={
            "ok": False,
            "error": "content_sha256, profile_fingerprint, producer_node_id, "
                     "result_envelope_sha all required",
        })
    if len(content_sha) != 64 or len(envelope_sha) != 64:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "sha256 values must be 64 hex chars (no prefix)",
        })

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO extraction_publications (
                        content_sha256, profile_fingerprint, producer_node_id,
                        result_envelope_sha, result_row_counts, produced_at,
                        signature
                    ) VALUES (%s, %s, %s, %s, %s,
                              COALESCE(%s::timestamptz, NOW()), %s)
                    ON CONFLICT (content_sha256, profile_fingerprint, producer_node_id)
                    DO UPDATE SET
                        result_envelope_sha = EXCLUDED.result_envelope_sha,
                        result_row_counts   = EXCLUDED.result_row_counts,
                        produced_at         = EXCLUDED.produced_at,
                        signature           = EXCLUDED.signature,
                        published_at        = NOW()
                    RETURNING publication_id, published_at
                    """,
                    (content_sha, profile_fp, producer, envelope_sha,
                     json.dumps(row_counts), produced_at, signature),
                )
                row = cur.fetchone()
            conn.commit()
    except Exception as e:
        return JSONResponse(status_code=500,
                            content={"ok": False, "error": str(e)[:300]})

    return {
        "ok": True,
        "publication_id":     int(row[0]),
        "published_at":       row[1].isoformat() if row[1] else None,
        "content_sha256":     content_sha,
        "profile_fingerprint": profile_fp,
        "producer_node_id":   producer,
    }


# ─── Phase 9 — hub-side envelope bytes mirror ───────────────────────────────
# Producers behind firewalls (Cloudflare Access, NAT, etc.) can't be reached
# directly by subscribers. The hub optionally stores envelope BYTES so
# subscribers can pull from the hub instead. Content integrity is still
# verified end-to-end via envelope_sha256 — the hub is a relay, not a
# trusted intermediary.

def _hub_extraction_store_root():
    """Where the hub keeps mirrored envelope bytes. Falls back to /tmp
    in dev environments where /var/lib/enki/hub-storage isn't writable."""
    from pathlib import Path
    explicit = os.environ.get("ENKI_HUB_EXTRACTION_STORE")
    if explicit:
        return Path(explicit)
    default = Path("/var/lib/enki/hub-storage/extractions")
    try:
        default.mkdir(parents=True, exist_ok=True)
        return default
    except (PermissionError, OSError):
        fallback = Path("/tmp/enki_hub_extractions")
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def _hub_extraction_path(content_sha256: str, profile_fingerprint: str,
                          producer_node_id: str):
    """Content-addressed path on the hub. Mirrors the producer's
    /var/lib/enki/storage/extractions layout for symmetry."""
    root = _hub_extraction_store_root()
    return (root / content_sha256[:2] / content_sha256
              / f"{profile_fingerprint[:16]}-{producer_node_id[:12]}.enki")


@router.post("/extractions/store/{content_sha256}/{profile_fingerprint}/{producer_node_id}")
async def hub_store_extraction(
    content_sha256: str,
    profile_fingerprint: str,
    producer_node_id: str,
    request: Request,
):
    """Producer uploads envelope bytes to the hub mirror. The producer
    must have already called /fleet/extractions/publish for the same
    (content, profile, producer) tuple; otherwise we 404 — we don't
    accept bytes for unannounced envelopes (defends against pure-blob
    storage abuse).

    The bytes are written content-addressed under the hub's extraction
    store; extraction_publications.hub_has_bytes is flipped to TRUE so
    subscribers know to look here.

    Idempotent: re-uploading the same envelope overwrites the file +
    updates hub_bytes_size + hub_bytes_stored_at."""
    if (len(content_sha256) != 64 or
            not profile_fingerprint or not producer_node_id):
        raise HTTPException(400, "invalid path params")

    body = await request.body()
    if not body:
        raise HTTPException(400, "empty body")
    if len(body) > 50 * 1024 * 1024:   # 50MB hard cap, way over MAX_ENVELOPE_BYTES (10MB)
        raise HTTPException(413, "envelope too large for hub mirror")

    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(503, content={"ok": False, "error": "db_unavailable"})

    # Confirm the publication exists
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT publication_id, result_envelope_sha
                     FROM extraction_publications
                    WHERE content_sha256 = %s AND profile_fingerprint = %s
                      AND producer_node_id = %s""",
                (content_sha256, profile_fingerprint, producer_node_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(
                    404, "no extraction_publication for this (content, profile, producer) — "
                         "call /fleet/extractions/publish first",
                )
            pub_id, expected_envelope_sha = row

            # The hub is a relay, not a validator. Subscribers verify
            # envelope_sha256 end-to-end via gate G3 (sha of
            # canonical_json(content) must match what the publication
            # recorded). If a hostile producer uploads tampered bytes
            # here, subscribers reject them on pull — never written to
            # any local DB. So we deliberately DON'T parse CBOR on the
            # hub (avoiding a cbor2 dependency, keeping the hub lean).
            #
            # Cheap byte-level sanity only: first byte must be in the
            # CBOR map range (0xa0..0xbb covers maps of length 0..2^64).
            if body[0] < 0xa0 or body[0] > 0xbb:
                raise HTTPException(
                    400, f"body doesn't start with a CBOR map byte "
                         f"(got 0x{body[0]:02x}); not a valid envelope",
                )

            # Persist
            target = _hub_extraction_path(content_sha256, profile_fingerprint, producer_node_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)

            cur.execute(
                """UPDATE extraction_publications
                      SET hub_has_bytes       = TRUE,
                          hub_bytes_size      = %s,
                          hub_bytes_stored_at = NOW()
                    WHERE publication_id = %s""",
                (len(body), pub_id),
            )
        conn.commit()

    return {
        "ok":              True,
        "publication_id":  int(pub_id),
        "bytes_stored":    len(body),
        "path":            str(target),
    }


@router.get("/extractions/serve/{content_sha256}/{profile_fingerprint}/{producer_node_id}")
def hub_serve_extraction(
    content_sha256: str,
    profile_fingerprint: str,
    producer_node_id: str,
):
    """Hub-mediated envelope download. Used when the producer is behind
    a firewall or unreachable; subscribers concat hub_url + this path
    instead of producer_url + /p2p/extraction.

    Returns the raw CBOR bytes. Subscriber verifies envelope_sha256 on
    receipt the same way it would for a producer-served envelope — the
    hub is just a relay."""
    target = _hub_extraction_path(content_sha256, profile_fingerprint, producer_node_id)
    if not target.exists():
        raise HTTPException(404, "envelope not mirrored on this hub")
    body = target.read_bytes()
    from fastapi.responses import Response
    return Response(
        content=body,
        media_type="application/enki+cbor",
        headers={
            "X-ENKI-Envelope-Type":     "extraction",
            "X-ENKI-Content-Sha256":    content_sha256,
            "X-ENKI-Profile-Fingerprint": profile_fingerprint,
            "X-ENKI-Producer-Node-Id":  producer_node_id,
            "X-ENKI-Hub-Mirror":        "true",
            "Cache-Control":            "public, max-age=86400, immutable",
        },
    )


@router.get("/extractions/find")
async def find_extractions(content: str, profile: Optional[str] = None,
                            min_trust: float = 0.0, limit: int = 20):
    """Subscriber-side: find producer nodes that have published an
    extraction envelope for (content_sha256, profile_fingerprint).
    Returns producers sorted by federation trust score so the subscriber
    can pull from the most-trusted one first.

    Query params:
        content    : 64-char content_sha256 (required, no prefix)
        profile    : profile_fingerprint (optional; omitting returns all
                     profiles that have published extractions for this content)
        min_trust  : skip producers with trust_score below this (default 0)
        limit      : max producers to return (default 20)

    Response:
        {
          "producers": [
            {
              "producer_node_id": "node:abc123...",
              "envelope_sha":     "<64hex>",
              "row_counts":       {...},
              "produced_at":      "2026-...",
              "trust_score":      0.85,
              "pull_url":         "/p2p/extraction/{content}/{profile}/{producer}"
            }, ...
          ],
          "consensus": {
            "unique_envelopes": 1,    # 1 = all producers agree; >1 = conflict
            "majority_envelope_sha": "..."
          }
        }
    """
    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(status_code=503,
                            content={"ok": False, "error": "db_unavailable"})

    content = (content or "").strip()
    if len(content) != 64:
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": "content must be 64-char sha256"})
    profile = (profile or "").strip() or None

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Trust source: peer_memberships.trust_score (Phase 0).
                # The older federation_node_trust table only existed on peer
                # nodes; not present on the cloud hub. peer_memberships is
                # canonical post-Phase 0.
                # LEFT JOIN peer_memberships → pull_base_url for absolute
                # subscriber URLs. NULL when the producer hasn't onboarded
                # under peer auth yet (dual-mode legacy publishers).
                # Phase 9: also return hub_has_bytes so subscribers know
                # whether they can fall back to /fleet/extractions/serve
                # when the producer is unreachable.
                if profile:
                    cur.execute(
                        """
                        SELECT ep.producer_node_id, ep.result_envelope_sha,
                               ep.result_row_counts, ep.produced_at,
                               ep.profile_fingerprint,
                               COALESCE(pm.trust_score, 0.5) AS trust,
                               pm.metadata->>'pull_base_url' AS pull_base_url,
                               COALESCE(ep.hub_has_bytes, FALSE) AS hub_has_bytes
                          FROM extraction_publications ep
                          LEFT JOIN peer_memberships pm
                            ON pm.peer_node_id = ep.producer_node_id
                         WHERE ep.content_sha256 = %s
                           AND ep.profile_fingerprint = %s
                           AND COALESCE(pm.trust_score, 0.5) >= %s
                         ORDER BY trust DESC, ep.published_at DESC
                         LIMIT %s
                        """,
                        (content, profile, min_trust, limit),
                    )
                else:
                    cur.execute(
                        """
                        SELECT ep.producer_node_id, ep.result_envelope_sha,
                               ep.result_row_counts, ep.produced_at,
                               ep.profile_fingerprint,
                               COALESCE(pm.trust_score, 0.5) AS trust,
                               pm.metadata->>'pull_base_url' AS pull_base_url,
                               COALESCE(ep.hub_has_bytes, FALSE) AS hub_has_bytes
                          FROM extraction_publications ep
                          LEFT JOIN peer_memberships pm
                            ON pm.peer_node_id = ep.producer_node_id
                         WHERE ep.content_sha256 = %s
                           AND COALESCE(pm.trust_score, 0.5) >= %s
                         ORDER BY trust DESC, ep.published_at DESC
                         LIMIT %s
                        """,
                        (content, min_trust, limit),
                    )
                rows = cur.fetchall()
    except Exception as e:
        return JSONResponse(status_code=500,
                            content={"ok": False, "error": str(e)[:300]})

    producers = []
    envelope_counts = {}
    for r in rows:
        prod, env_sha, counts, prod_at, prof, trust, pull_base, hub_has_bytes = r
        producers.append({
            "producer_node_id":   prod,
            "envelope_sha":       env_sha,
            "row_counts":         counts,
            "produced_at":        prod_at.isoformat() if prod_at else None,
            "profile_fingerprint": prof,
            "trust_score":        float(trust),
            "pull_base_url":      pull_base,
            "pull_url":           f"/p2p/extraction/{content}/{prof}/{prod}",
            "hub_has_bytes":      bool(hub_has_bytes),
            "hub_pull_url":       (f"/fleet/extractions/serve/{content}/{prof}/{prod}"
                                   if hub_has_bytes else None),
        })
        envelope_counts[env_sha] = envelope_counts.get(env_sha, 0) + 1

    # Consensus: how many distinct envelope_shas did we see, which one
    # has the most producers behind it? Subscribers can use this to
    # decide whether to trust a single producer or wait for corroboration.
    if envelope_counts:
        majority_sha = max(envelope_counts, key=envelope_counts.get)
    else:
        majority_sha = None

    return {
        "content_sha256":      content,
        "profile_fingerprint": profile,
        "producers":           producers,
        "count":               len(producers),
        "consensus": {
            "unique_envelopes":      len(envelope_counts),
            "majority_envelope_sha": majority_sha,
            "majority_count":        envelope_counts.get(majority_sha, 0),
        },
    }


# ─── Work-claim protocol (phase 4) ─────────────────────────────────────
# Before a node spends 30+ minutes running LLM extraction on a doc, it:
#   1. GET /extractions/check  — is anyone else already on this?
#   2. POST /extractions/claim — atomically reserve the work
#   3. (run extraction locally)
#   4. POST /extractions/release — mark done (or abandon)
# The UNIQUE partial index on (content_sha256, profile_fingerprint)
# WHERE status='active' makes the claim race-condition-safe.

@router.get("/extractions/check")
async def check_extraction(content: str, profile: str):
    """Before claiming, peers ask: "is this work already done OR
    actively claimed by someone else?" Tells the caller whether to
    skip, wait, or proceed.

    Query params:
        content : 64-char content_sha256 (required)
        profile : profile_fingerprint     (required)

    Response:
        {
          "already_published": bool,
          "publication_count": int,
          "active_claims": [
            {"claimant_node_id": "...", "claimed_at": "...", "expires_at": "..."}
          ],
          "advice": "skip" | "wait" | "claim_it"
        }

    Semantics:
      - already_published=True → "skip" (subscriber will pull the envelope)
      - active_claims non-empty → "wait" (peer is working on it)
      - both empty                → "claim_it" (you're first)
    """
    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(status_code=503,
                            content={"ok": False, "error": "db_unavailable"})

    content = (content or "").strip()
    if len(content) != 64:
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": "content must be 64-char sha256"})
    profile = (profile or "").strip()
    if not profile:
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": "profile required"})

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Expire any stale claims first (cheap idempotent sweep).
            cur.execute(
                """UPDATE extraction_claims
                      SET status = 'expired'
                    WHERE status = 'active' AND expires_at < NOW()""",
            )

            cur.execute(
                "SELECT COUNT(*) FROM extraction_publications "
                "WHERE content_sha256 = %s AND profile_fingerprint = %s",
                (content, profile),
            )
            pub_count = cur.fetchone()[0]

            cur.execute(
                """SELECT claimant_node_id, claimed_at, expires_at
                     FROM extraction_claims
                    WHERE content_sha256 = %s AND profile_fingerprint = %s
                      AND status = 'active'""",
                (content, profile),
            )
            claims = [
                {"claimant_node_id": r[0], "claimed_at": r[1].isoformat(),
                 "expires_at": r[2].isoformat()}
                for r in cur.fetchall()
            ]
            conn.commit()

    if pub_count > 0:
        advice = "skip"
    elif claims:
        advice = "wait"
    else:
        advice = "claim_it"

    return {
        "content_sha256":     content,
        "profile_fingerprint": profile,
        "already_published":  pub_count > 0,
        "publication_count":  pub_count,
        "active_claims":      claims,
        "advice":             advice,
    }


@router.post("/extractions/claim")
async def claim_extraction(content: str, profile: str,
                           claimant_node_id: str, ttl_seconds: int = 3600):
    """Atomically reserve a (content, profile) extraction. Race-safe
    via the UNIQUE partial index on (content, profile) WHERE status='active'.

    Query params:
        content          : 64-char content_sha256 (required)
        profile          : profile_fingerprint    (required)
        claimant_node_id : the claiming node's ID (required)
        ttl_seconds      : how long the claim is valid (default 1h)

    Response on success:
        {"ok": true, "claim_id": <int>, "expires_at": "<iso>"}

    Response on conflict (someone else holds the active claim):
        {"ok": false, "error": "already_claimed",
         "active_claim": {"claimant_node_id": "...", "expires_at": "..."}}
    """
    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(status_code=503,
                            content={"ok": False, "error": "db_unavailable"})

    content = (content or "").strip()
    if len(content) != 64:
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": "content must be 64-char sha256"})
    profile = (profile or "").strip()
    claimant_node_id = (claimant_node_id or "").strip()
    if not profile or not claimant_node_id:
        return JSONResponse(status_code=400,
                            content={"ok": False,
                                     "error": "profile and claimant_node_id required"})
    ttl_seconds = max(60, min(ttl_seconds, 86400))  # 1 min .. 1 day

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Sweep expired claims so the unique partial index doesn't
            # block legitimate fresh claims.
            cur.execute(
                """UPDATE extraction_claims
                      SET status = 'expired'
                    WHERE status = 'active' AND expires_at < NOW()""",
            )
            try:
                cur.execute(
                    """INSERT INTO extraction_claims
                           (content_sha256, profile_fingerprint, claimant_node_id,
                            expires_at, status)
                       VALUES (%s, %s, %s, NOW() + (%s || ' seconds')::interval, 'active')
                       RETURNING claim_id, expires_at""",
                    (content, profile, claimant_node_id, ttl_seconds),
                )
                claim_id, expires_at = cur.fetchone()
                conn.commit()
                return {
                    "ok":         True,
                    "claim_id":   claim_id,
                    "expires_at": expires_at.isoformat(),
                }
            except Exception as e:
                conn.rollback()
                # Likely UNIQUE constraint violation — fetch the holder
                with conn.cursor() as c2:
                    c2.execute(
                        """SELECT claimant_node_id, expires_at
                             FROM extraction_claims
                            WHERE content_sha256 = %s AND profile_fingerprint = %s
                              AND status = 'active'
                            LIMIT 1""",
                        (content, profile),
                    )
                    row = c2.fetchone()
                if row:
                    return JSONResponse(status_code=409, content={
                        "ok":    False,
                        "error": "already_claimed",
                        "active_claim": {
                            "claimant_node_id": row[0],
                            "expires_at":       row[1].isoformat(),
                        },
                    })
                return JSONResponse(status_code=500,
                                    content={"ok": False, "error": str(e)})


@router.post("/extractions/release")
async def release_extraction(claim_id: int, claimant_node_id: str,
                             outcome: str = "completed"):
    """Release a held claim. Outcomes:
      'completed' → extraction succeeded, envelope was published
      'abandoned' → extraction failed or was cancelled; other nodes may try

    Only the original claimant_node_id can release its own claim
    (defense against malicious peers stomping each others' claims).

    Query params:
        claim_id         : the claim_id returned by /claim
        claimant_node_id : must match the original claimant
        outcome          : 'completed' (default) or 'abandoned'
    """
    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(status_code=503,
                            content={"ok": False, "error": "db_unavailable"})

    if outcome not in ("completed", "abandoned"):
        return JSONResponse(status_code=400,
                            content={"ok": False,
                                     "error": "outcome must be 'completed' or 'abandoned'"})

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE extraction_claims
                      SET status = %s, completed_at = NOW()
                    WHERE claim_id = %s
                      AND claimant_node_id = %s
                      AND status = 'active'
                    RETURNING claim_id""",
                (outcome, claim_id, claimant_node_id),
            )
            row = cur.fetchone()
            conn.commit()
            if not row:
                return JSONResponse(status_code=404, content={
                    "ok":    False,
                    "error": "claim not found, not yours, or already released",
                })
            return {"ok": True, "claim_id": row[0], "outcome": outcome}


@router.get("/libraries/network")
async def discover_network_libraries(limit_per_node: int = 50, timeout: float = 5.0):
    """Cross-node discovery: aggregate public libraries across every node
    in the federation. Returns a deduplicated catalog so operators can
    pick which library to mirror.

    Pattern mirrors /fleet/entities/network and /fleet/topics/network —
    polls each registered node's /fleet/libraries/public and merges.

    Returns empty if this node has no node_registry entries (i.e., it's
    not a hub and hasn't seen any peers yet). The hub at example.com
    is the typical caller of this endpoint, but any node with peer
    visibility can serve it.
    """
    import asyncio
    import httpx

    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(status_code=503,
                            content={"ok": False, "error": "db_unavailable"})

    # 1. List candidate nodes from local node_registry. We want anything
    #    with a known hostname/URL that we can reach via HTTP.
    candidates: list[dict] = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT node_id, hostname, last_seen_at
                      FROM node_registry
                     WHERE hostname IS NOT NULL
                       AND hostname <> ''
                       AND (last_seen_at IS NULL OR
                            last_seen_at > NOW() - INTERVAL '7 days')
                     ORDER BY last_seen_at DESC NULLS LAST
                    """
                )
                for r in cur.fetchall():
                    node_id, hostname, last_seen = r
                    # Hostname is a bare hostname or a URL. Normalize.
                    url = hostname if hostname.startswith("http") else f"https://{hostname}"
                    candidates.append({
                        "node_id": node_id,
                        "url": url.rstrip("/"),
                        "last_seen_at": last_seen.isoformat() if last_seen else None,
                    })
    except Exception as e:
        # node_registry may not exist on this node (only hub has it
        # populated); not an error, just means we have nothing to aggregate.
        logger.debug("node_registry not queryable: %s", e)
        candidates = []

    # 2. Always include self — the local node's own public libraries.
    #    This way even a single-node setup gets the discovery experience.
    candidates.insert(0, {"node_id": "self", "url": "http://localhost:8000", "last_seen_at": None})

    # 3. Concurrently poll every candidate's /fleet/libraries/public.
    #    Bounded timeout so a slow node doesn't stall the whole discovery.
    async def _poll(client: httpx.AsyncClient, c: dict) -> list:
        try:
            r = await client.get(
                f"{c['url']}/fleet/libraries/public?limit={limit_per_node}",
                timeout=timeout,
            )
            if r.status_code != 200:
                return []
            payload = r.json()
            libs = payload.get("libraries") or []
            # Stamp each library with its host node + URL so the
            # subscriber knows where to call.
            for lib in libs:
                lib["host_node_id"] = c["node_id"]
                lib["host_url"] = c["url"]
                lib["host_last_seen_at"] = c["last_seen_at"]
            return libs
        except Exception as e:
            logger.debug("poll failed for %s: %s", c["url"], e)
            return []

    aggregated: list = []
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*(_poll(client, c) for c in candidates))
        for libs in results:
            aggregated.extend(libs)

    # 4. Dedup by (host_node_id, library_id) — same host shouldn't appear
    #    twice. (Different hosts publishing the same canonical_name is
    #    FINE — that's the federation working as designed; both copies
    #    are real and an operator might pick either.)
    seen = set()
    deduped = []
    for lib in aggregated:
        key = (lib.get("host_node_id"), lib.get("library_id"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(lib)

    return {
        "libraries": deduped,
        "count": len(deduped),
        "polled_nodes": len(candidates),
    }


@router.post("/libraries/{library_id}/subscribe")
async def subscribe_to_library(library_id: int, body: LibrarySubscribeRequest):
    """A node opts into processing this library's documents.

    Enforces subscription_policy:
      - invite_only: subscription always rejected
      - open:        subscription accepted, yield ignored
      - yield_gated: subscription requires yield >= min_yield_required
    """
    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(status_code=503,
                            content={"ok": False, "error": "db_unavailable"})

    node_id = (body.node_id or "").strip()
    if not node_id:
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": "node_id required"})

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT is_public, subscription_policy, min_yield_required
                         FROM libraries WHERE library_id = %s""",
                    (library_id,),
                )
                row = cur.fetchone()
                if not row:
                    return JSONResponse(status_code=404,
                                        content={"ok": False,
                                                 "error": "library_not_found"})
                is_public, policy, min_yield = row
                if not is_public:
                    return JSONResponse(status_code=403,
                                        content={"ok": False,
                                                 "error": "library_not_public"})
                if policy == "invite_only":
                    return JSONResponse(status_code=403,
                                        content={"ok": False,
                                                 "error": "invite_only"})

                # Look up the subscriber's current yield for gating + audit
                cur.execute(
                    """SELECT yield_score FROM federation_node_trust
                        WHERE node_id = %s""",
                    (node_id,),
                )
                yrow = cur.fetchone()
                # federation_node_trust may not have yield_score yet
                # (added in migration 091). Tolerate missing column.
                node_yield = 0.0
                if yrow:
                    try:
                        node_yield = float(yrow[0] or 0.0)
                    except Exception:
                        node_yield = 0.0

                if policy == "yield_gated" and node_yield < float(min_yield or 0.0):
                    return JSONResponse(
                        status_code=403,
                        content={
                            "ok": False,
                            "error": "below_min_yield",
                            "required": float(min_yield or 0.0),
                            "have": node_yield,
                        },
                    )

                cur.execute(
                    """
                    INSERT INTO library_subscriptions
                      (library_id, subscriber_node_id, status,
                       max_docs_per_day, yield_at_subscribe, notes)
                    VALUES (%s, %s, 'active', %s, %s, %s)
                    ON CONFLICT (library_id, subscriber_node_id)
                    DO UPDATE SET
                      status = 'active',
                      last_seen_at = NOW(),
                      max_docs_per_day = COALESCE(EXCLUDED.max_docs_per_day,
                                                  library_subscriptions.max_docs_per_day),
                      notes = COALESCE(EXCLUDED.notes,
                                       library_subscriptions.notes)
                    RETURNING subscription_id, subscribed_at
                    """,
                    (library_id, node_id, body.max_docs_per_day,
                     node_yield, body.notes),
                )
                sub_row = cur.fetchone()
            conn.commit()
    except Exception as e:
        return JSONResponse(status_code=500,
                            content={"ok": False, "error": str(e)[:200]})

    return {
        "ok": True,
        "subscription_id": int(sub_row[0]),
        "library_id": library_id,
        "node_id": node_id,
        "subscribed_at": sub_row[1].isoformat() if sub_row[1] else None,
        "policy": policy,
        "node_yield": node_yield,
    }


@router.get("/libraries/{library_id}/work")
async def get_library_work(library_id: int, node_id: str, batch: int = 5):
    """Return up to `batch` unprocessed documents from this library
    assigned to `node_id`. Phase 10 of the plan adds the dedicated
    library_work_assignments table; until that migration lands, this
    endpoint returns documents that the subscriber hasn't yet seen by
    joining library_members → documents.
    """
    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(status_code=503,
                            content={"ok": False, "error": "db_unavailable"})

    # Guard: subscriber must be active
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT status FROM library_subscriptions
                        WHERE library_id = %s AND subscriber_node_id = %s""",
                    (library_id, node_id),
                )
                row = cur.fetchone()
                if not row:
                    return JSONResponse(status_code=403,
                                        content={"ok": False,
                                                 "error": "not_subscribed"})
                if row[0] != "active":
                    return JSONResponse(status_code=403,
                                        content={"ok": False,
                                                 "error": f"subscription_{row[0]}"})

                # Hand out a batch of documents the subscriber hasn't
                # logged completion for. Prefer the dedicated work-
                # assignments table when present (Phase 10); fall back
                # to a simple library_members → documents join.
                cur.execute(
                    """
                    SELECT to_regclass('library_work_assignments') IS NOT NULL
                    """,
                )
                has_assignments = bool(cur.fetchone()[0])

                if has_assignments:
                    cur.execute(
                        """
                        SELECT a.assignment_id, a.document_id,
                               d.title, d.url
                        FROM library_work_assignments a
                        JOIN documents d ON d.document_id = a.document_id
                        WHERE a.library_id = %s
                          AND a.subscriber_node_id = %s
                          AND a.status = 'assigned'
                        ORDER BY a.assigned_at ASC
                        LIMIT %s
                        """,
                        (library_id, node_id, max(1, min(batch, 50))),
                    )
                    items = [
                        {
                            "assignment_id": int(r[0]),
                            "document_id": int(r[1]),
                            "title": r[2],
                            "url": r[3],
                        }
                        for r in cur.fetchall()
                    ]
                else:
                    cur.execute(
                        """
                        SELECT d.document_id, d.title, d.url
                        FROM library_members lm
                        JOIN documents d ON d.document_id = lm.document_id
                        WHERE lm.library_id = %s
                          AND lm.document_id IS NOT NULL
                        ORDER BY lm.added_at DESC NULLS LAST
                        LIMIT %s
                        """,
                        (library_id, max(1, min(batch, 50))),
                    )
                    items = [
                        {
                            "assignment_id": None,
                            "document_id": int(r[0]),
                            "title": r[1],
                            "url": r[2],
                        }
                        for r in cur.fetchall()
                    ]

                # Bump last_seen_at on the subscription for liveness
                cur.execute(
                    """UPDATE library_subscriptions
                          SET last_seen_at = NOW()
                        WHERE library_id = %s AND subscriber_node_id = %s""",
                    (library_id, node_id),
                )
            conn.commit()
    except Exception as e:
        return JSONResponse(status_code=500,
                            content={"ok": False, "error": str(e)[:200]})

    return {"library_id": library_id, "node_id": node_id,
            "work": items, "count": len(items)}


class WorkCompleteRequest(BaseModel):
    node_id: str
    library_id: int
    document_id: int
    result_hash: str           # SHA-256 of the canonical result envelope
    result_signature: str      # hardware-signed Ed25519 over result_hash
    metadata: Optional[dict] = None


@router.post("/libraries/work/complete")
async def submit_work_complete(body: WorkCompleteRequest):
    """A subscriber reports the result of a work assignment.

    Stores result_hash + signature for later cross-verification. The
    yield-accounting worker (Phase 11) compares hashes across the >=2
    assignments per doc and updates yield_score accordingly.
    """
    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(status_code=503,
                            content={"ok": False, "error": "db_unavailable"})

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE library_work_assignments SET
                      status           = 'completed',
                      completed_at     = NOW(),
                      result_hash      = %s,
                      result_signature = %s,
                      result_metadata  = %s::jsonb,
                      attempts         = attempts + 1
                    WHERE library_id = %s
                      AND document_id = %s
                      AND subscriber_node_id = %s
                      AND status IN ('assigned', 'in_progress')
                    RETURNING assignment_id, completed_at
                    """,
                    (
                        body.result_hash,
                        body.result_signature,
                        json.dumps(body.metadata or {}),
                        body.library_id,
                        body.document_id,
                        body.node_id,
                    ),
                )
                row = cur.fetchone()
            conn.commit()
    except Exception as e:
        return JSONResponse(status_code=500,
                            content={"ok": False, "error": str(e)[:200]})

    if not row:
        return JSONResponse(status_code=404,
                            content={"ok": False,
                                     "error": "assignment_not_found_or_already_settled"})

    return {
        "ok": True,
        "assignment_id": int(row[0]),
        "completed_at": row[1].isoformat() if row[1] else None,
    }


@router.get("/nodes/{node_id}/yield")
async def get_node_yield(node_id: str, recent: int = 25):
    """Return a node's yield_score, last update time, and a breakdown
    of recent contributions (consensus, disputes, decay)."""
    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(status_code=503,
                            content={"ok": False, "error": "db_unavailable"})

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT yield_score, yield_history,
                              last_yield_update_at
                         FROM federation_node_trust
                        WHERE node_id = %s""",
                    (node_id,),
                )
                row = cur.fetchone()

                cur.execute(
                    """SELECT contribution_type, count(*),
                              COALESCE(sum(weight), 0)
                         FROM node_contributions
                        WHERE node_id = %s
                        GROUP BY contribution_type
                        ORDER BY sum(weight) DESC""",
                    (node_id,),
                )
                breakdown = [
                    {
                        "contribution_type": r[0],
                        "count": int(r[1] or 0),
                        "total_weight": float(r[2] or 0.0),
                    }
                    for r in cur.fetchall()
                ]

                cur.execute(
                    """SELECT contribution_type, weight, target_id,
                              verified_at, metadata
                         FROM node_contributions
                        WHERE node_id = %s
                        ORDER BY verified_at DESC
                        LIMIT %s""",
                    (node_id, max(1, min(recent, 200))),
                )
                recent_rows = [
                    {
                        "contribution_type": r[0],
                        "weight": float(r[1] or 0.0),
                        "target_id": int(r[2]) if r[2] is not None else None,
                        "verified_at": r[3].isoformat() if r[3] else None,
                        "metadata": r[4],
                    }
                    for r in cur.fetchall()
                ]
    except Exception as e:
        return JSONResponse(status_code=500,
                            content={"ok": False, "error": str(e)[:200]})

    if not row:
        return {
            "node_id": node_id,
            "yield_score": 0.0,
            "innocent_prior": True,
            "last_yield_update_at": None,
            "breakdown": breakdown,
            "recent": recent_rows,
        }

    return {
        "node_id": node_id,
        "yield_score": float(row[0] or 0.0),
        "yield_history": row[1],
        "last_yield_update_at": row[2].isoformat() if row[2] else None,
        "breakdown": breakdown,
        "recent": recent_rows,
    }


# ─── Governance (Phase 12) ─────────────────────────────────────────────────

# Floor weight so a freshly-joined node with yield=0 still has some voice
# on small proposals. Tuneable.
GOVERNANCE_MIN_WEIGHT = 0.1


class GovernanceVoteRequest(BaseModel):
    proposal_id: int
    node_id: str
    ballot: str   # 'yes' | 'no' | 'abstain'
    signature: Optional[str] = None
    notes: Optional[str] = None


@router.post("/governance/vote")
async def cast_governance_vote(body: GovernanceVoteRequest):
    """Cast (or re-cast) a node's vote on an open proposal.

    Effective weight = max(node.yield_score, GOVERNANCE_MIN_WEIGHT).
    Stamped at cast time so post-vote yield changes don't shift tallies.
    """
    if body.ballot not in ("yes", "no", "abstain"):
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": "invalid_ballot"})

    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(status_code=503,
                            content={"ok": False, "error": "db_unavailable"})

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT status, closes_at FROM governance_proposals
                        WHERE proposal_id = %s""",
                    (body.proposal_id,),
                )
                p_row = cur.fetchone()
                if not p_row:
                    return JSONResponse(status_code=404,
                                        content={"ok": False,
                                                 "error": "proposal_not_found"})
                status, closes_at = p_row
                if status != "open":
                    return JSONResponse(status_code=403,
                                        content={"ok": False,
                                                 "error": f"proposal_{status}"})

                # Pull yield_score at cast time. May be 0 / null.
                cur.execute(
                    """SELECT yield_score FROM federation_node_trust
                        WHERE node_id = %s""",
                    (body.node_id,),
                )
                yrow = cur.fetchone()
                node_yield = float(yrow[0] or 0.0) if yrow else 0.0
                weight = max(node_yield, GOVERNANCE_MIN_WEIGHT)

                cur.execute(
                    """
                    INSERT INTO governance_votes
                      (proposal_id, node_id, ballot, effective_weight,
                       signature, notes)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (proposal_id, node_id) DO UPDATE SET
                      ballot           = EXCLUDED.ballot,
                      effective_weight = EXCLUDED.effective_weight,
                      signature        = EXCLUDED.signature,
                      notes            = EXCLUDED.notes,
                      cast_at          = NOW()
                    RETURNING vote_id, cast_at
                    """,
                    (body.proposal_id, body.node_id, body.ballot, weight,
                     body.signature, body.notes),
                )
                row = cur.fetchone()
            conn.commit()
    except Exception as e:
        return JSONResponse(status_code=500,
                            content={"ok": False, "error": str(e)[:200]})

    return {
        "ok": True,
        "vote_id": int(row[0]),
        "cast_at": row[1].isoformat() if row[1] else None,
        "effective_weight": weight,
        "node_yield": node_yield,
    }


@router.get("/governance/proposals/{proposal_id}/tally")
async def proposal_tally(proposal_id: int):
    """Compute the running tally for an open proposal — yield-weighted
    fraction yes / no / abstain. Marks the proposal passed/failed if
    the close time has elapsed AND quorum-equivalent (any votes) cast."""
    try:
        from storage.postgres.db import get_connection
    except Exception:
        return JSONResponse(status_code=503,
                            content={"ok": False, "error": "db_unavailable"})

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT title, status, closes_at, pass_threshold
                         FROM governance_proposals
                        WHERE proposal_id = %s""",
                    (proposal_id,),
                )
                prow = cur.fetchone()
                if not prow:
                    return JSONResponse(status_code=404,
                                        content={"ok": False,
                                                 "error": "proposal_not_found"})
                title, p_status, closes_at, pass_threshold = prow

                cur.execute(
                    """SELECT ballot, COALESCE(sum(effective_weight), 0),
                              count(*)
                         FROM governance_votes
                        WHERE proposal_id = %s
                        GROUP BY ballot""",
                    (proposal_id,),
                )
                tally = {b: {"weight": 0.0, "count": 0}
                         for b in ("yes", "no", "abstain")}
                for ballot, w, c in cur.fetchall():
                    tally[ballot] = {"weight": float(w or 0.0),
                                     "count": int(c or 0)}
    except Exception as e:
        return JSONResponse(status_code=500,
                            content={"ok": False, "error": str(e)[:200]})

    total_decisive = tally["yes"]["weight"] + tally["no"]["weight"]
    yes_fraction = (tally["yes"]["weight"] / total_decisive) if total_decisive else 0.0

    return {
        "proposal_id": proposal_id,
        "title": title,
        "status": p_status,
        "closes_at": closes_at.isoformat() if closes_at else None,
        "pass_threshold": float(pass_threshold or 0.5),
        "tally": tally,
        "yes_fraction": yes_fraction,
        "would_pass_now": yes_fraction >= float(pass_threshold or 0.5),
    }
