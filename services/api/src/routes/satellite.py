"""
Satellite imagery connection — ESA / Copernicus Sentinel-2 via Sentinel Hub WMS.

The connection is credential-driven: the operator pastes a Sentinel Hub WMS
*instance ID* (and, optionally, OAuth client credentials for future monitoring)
into Settings → Satellite Imagery. Those land in the tenant-scoped `secrets`
table under fixed names and are read here server-side. The instance ID never
reaches the browser — live map tiles are proxied through GET /satellite/wms.

Endpoints
    GET  /satellite/status   — is the connection configured? (no secret values)
    POST /satellite/test     — WMS GetCapabilities against the stored instance
    GET  /satellite/wms      — tile proxy (WMS GetMap) for the map overlay
    POST /satellite/capture  — pull overhead of an entity's coords, store as
                               signed .enki evidence, attach to the dossier
    GET  /satellite/dates    — (stub) available clear passes; Phase 2 Catalog API

Phase 1 needs no OAuth — the WMS instance drives both tiles and capture. The
stored client_id/secret are reserved for Phase 3 (Statistical-API monitoring).
"""

from __future__ import annotations

import logging
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from storage.postgres.db import get_connection
from storage.secrets import get_secret_value

log = logging.getLogger("satellite")

router = APIRouter(prefix="/satellite", tags=["satellite"])

# ── Connection config (fixed secret names, set via the Settings UI) ──────────
SECRET_INSTANCE = "copernicus-wms-instance"
SECRET_CLIENT_ID = "copernicus-client-id"
SECRET_CLIENT_SECRET = "copernicus-client-secret"

WMS_BASE = "https://sh.dataspace.copernicus.eu/ogc/wms/{instance}"

# Layers we expose through the proxy / capture (matches the map + Full WMS
# template). Whitelisted so the proxy can't be turned into an open relay.
ALLOWED_LAYERS = {
    "TRUE_COLOR", "FALSE_COLOR", "FALSE_COLOR_URBAN", "NDVI", "SWIR",
    "MOISTURE_INDEX", "AGRICULTURE", "GEOLOGY", "NDWI", "EVI",
}

_ENTITY_COL = {"contact": "contact_id", "asset": "asset_id",
               "case": "case_id", "node": "node_id"}

# Sentinel Hub S2 collections cap at 200 m/pixel. Below that resolution (i.e.
# zoomed too far out) SH returns a tiled ERROR IMAGE ("resolution exceeds the
# limit"). We detect that case and serve a transparent tile instead, so the map
# stays clean and the UI can prompt "zoom in" rather than showing error text.
MAX_METERS_PER_PIXEL = 200.0
import base64 as _b64
_TRANSPARENT_PNG = _b64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA"
    "60e6kgAAAABJRU5ErkJggg=="
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _instance(tenant_id: str) -> Optional[str]:
    return get_secret_value(tenant_id, SECRET_INSTANCE)


def _default_time_range() -> str:
    """Rolling last-90-day window so imagery is always fresh."""
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=90)
    return f"{start.isoformat()}/{today.isoformat()}"


def _mercator(lat: float, lon: float) -> tuple[float, float]:
    """WGS84 lat/lon → EPSG:3857 meters."""
    r = 6378137.0
    x = r * math.radians(lon)
    y = r * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
    return x, y


def _bbox_around(lat: float, lon: float, box_km: float) -> str:
    """EPSG:3857 bbox string (minx,miny,maxx,maxy) for a box_km square."""
    x, y = _mercator(lat, lon)
    half = (box_km * 1000.0) / 2.0
    return f"{x - half},{y - half},{x + half},{y + half}"


def _resolve_coords(tenant_id: str, entity_id: str) -> Optional[tuple[float, float]]:
    """Best-effort coordinate lookup, mirroring GET /entities/{id}/geo.
    Tries knowledge_nodes (by node_id or master_entity_id) then entity_addresses."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT latitude, longitude FROM knowledge_nodes
                    WHERE (node_id = %s OR node_id = %s)
                      AND latitude IS NOT NULL AND longitude IS NOT NULL
                    ORDER BY source_count DESC NULLS LAST LIMIT 1""",
                (str(entity_id), f"entity:{entity_id}"),
            )
            row = cur.fetchone()
            if row:
                return float(row[0]), float(row[1])
            if str(entity_id).isdigit():
                cur.execute(
                    """SELECT latitude, longitude FROM knowledge_nodes
                        WHERE master_entity_id = %s
                          AND latitude IS NOT NULL AND longitude IS NOT NULL
                        ORDER BY source_count DESC NULLS LAST LIMIT 1""",
                    (int(entity_id),),
                )
                row = cur.fetchone()
                if row:
                    return float(row[0]), float(row[1])
                cur.execute(
                    """SELECT lat, lon FROM entity_addresses
                        WHERE entity_id = %s AND lat IS NOT NULL AND lon IS NOT NULL
                        ORDER BY (valid_to IS NULL) DESC, confidence DESC NULLS LAST
                        LIMIT 1""",
                    (int(entity_id),),
                )
                row = cur.fetchone()
                if row:
                    return float(row[0]), float(row[1])
    return None


def _wms_getmap(instance: str, *, layer: str, bbox: str, width: int,
                height: int, time_range: str, maxcc: int, fmt: str) -> requests.Response:
    params = {
        "SERVICE": "WMS", "REQUEST": "GetMap", "VERSION": "1.3.0",
        "LAYERS": layer, "FORMAT": fmt, "CRS": "EPSG:3857",
        "BBOX": bbox, "WIDTH": str(width), "HEIGHT": str(height),
        "TIME": time_range, "MAXCC": str(maxcc),
    }
    return requests.get(WMS_BASE.format(instance=instance), params=params, timeout=40)


# ── Status / test ────────────────────────────────────────────────────────────

@router.get("/status")
def status(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Whether the connection is configured. Never returns secret values."""
    inst = _instance(auth.tenant_id)
    cid = get_secret_value(auth.tenant_id, SECRET_CLIENT_ID)
    csec = get_secret_value(auth.tenant_id, SECRET_CLIENT_SECRET)
    return {
        "configured": bool(inst),
        "has_instance": bool(inst),
        "has_oauth": bool(cid and csec),
    }


@router.post("/test")
def test_connection(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Validate the stored instance via a WMS GetCapabilities and list layers."""
    inst = _instance(auth.tenant_id)
    if not inst:
        return {"success": False, "error": "No WMS instance ID configured."}
    try:
        url = WMS_BASE.format(instance=inst)
        r = requests.get(url, params={
            "SERVICE": "WMS", "REQUEST": "GetCapabilities", "VERSION": "1.3.0",
        }, timeout=25)
    except requests.RequestException as e:
        return {"success": False, "error": f"Request failed: {e}"}
    if r.status_code != 200 or "xml" not in r.headers.get("content-type", ""):
        return {"success": False,
                "error": f"Unexpected response ({r.status_code}). Check the instance ID."}
    # Layer <Name> elements (skip the root container names heuristically).
    names = re.findall(r"<Name>([^<]+)</Name>", r.text)
    layers = [n for n in names if n and not n.lower().startswith(("wms", "http"))]
    return {"success": True, "layers": layers[:60]}


# ── Tile proxy for the map overlay ───────────────────────────────────────────

@router.get("/wms")
def wms_proxy(
    layer: str = Query(...),
    bbox: str = Query(..., description="EPSG:3857 minx,miny,maxx,maxy"),
    width: int = Query(512, ge=32, le=2048),
    height: int = Query(512, ge=32, le=2048),
    time: Optional[str] = Query(None),
    maxcc: int = Query(20, ge=0, le=100),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Proxy a single WMS GetMap tile so the instance ID stays server-side."""
    inst = _instance(auth.tenant_id)
    if not inst:
        return JSONResponse({"error": "SATELLITE.NOT_CONFIGURED"}, status_code=404)
    if layer not in ALLOWED_LAYERS:
        return JSONResponse({"error": "SATELLITE.LAYER_NOT_ALLOWED"}, status_code=400)
    # Resolution guard: if the tile is coarser than Sentinel Hub allows, don't
    # forward (SH would return an error image) — return a transparent tile.
    try:
        minx, _miny, maxx, _maxy = (float(v) for v in bbox.split(","))
        if (maxx - minx) / max(width, 1) > MAX_METERS_PER_PIXEL:
            return Response(content=_TRANSPARENT_PNG, media_type="image/png",
                            headers={"Cache-Control": "public, max-age=3600"})
    except (ValueError, TypeError):
        pass
    try:
        r = _wms_getmap(inst, layer=layer, bbox=bbox, width=width, height=height,
                        time_range=time or _default_time_range(), maxcc=maxcc,
                        fmt="image/jpeg")
    except requests.RequestException as e:
        return JSONResponse({"error": f"UPSTREAM: {e}"}, status_code=502)
    if r.status_code != 200:
        return JSONResponse({"error": "UPSTREAM", "status": r.status_code},
                            status_code=502)
    return Response(
        content=r.content,
        media_type=r.headers.get("content-type", "image/jpeg"),
        headers={"Cache-Control": "public, max-age=3600"},
    )


# ── On-demand capture → signed evidence on a dossier ─────────────────────────

class CaptureRequest(BaseModel):
    entity_id: Optional[str] = None
    entity_type: str = "node"
    lat: Optional[float] = None
    lon: Optional[float] = None
    layer: str = "TRUE_COLOR"
    time_range: Optional[str] = None
    box_km: float = 3.0


@router.post("/capture")
def capture(body: CaptureRequest, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Pull overhead imagery for an entity's coordinates (or explicit lat/lon),
    store it as a content-addressed, Ed25519-signed .enki evidence document, and
    attach it to the entity's dossier. The document's acquisition window lands on
    the entity timeline via its issued_date."""
    inst = _instance(auth.tenant_id)
    if not inst:
        return JSONResponse({"error": "SATELLITE.NOT_CONFIGURED",
                             "detail": "Add a WMS instance in Settings → Satellite Imagery."},
                            status_code=400)
    if body.layer not in ALLOWED_LAYERS:
        return JSONResponse({"error": "SATELLITE.LAYER_NOT_ALLOWED"}, status_code=400)

    lat, lon = body.lat, body.lon
    if lat is None or lon is None:
        if not body.entity_id:
            return JSONResponse({"error": "SATELLITE.NO_TARGET",
                                 "detail": "Provide entity_id or lat/lon."}, status_code=400)
        coords = _resolve_coords(auth.tenant_id, body.entity_id)
        if not coords:
            return JSONResponse({"error": "SATELLITE.NO_COORDS",
                                 "detail": "No known coordinates for this entity."},
                                status_code=422)
        lat, lon = coords

    time_range = body.time_range or _default_time_range()
    bbox = _bbox_around(lat, lon, body.box_km)

    try:
        r = _wms_getmap(inst, layer=body.layer, bbox=bbox, width=1024, height=1024,
                        time_range=time_range, maxcc=20, fmt="image/png")
    except requests.RequestException as e:
        return JSONResponse({"error": f"UPSTREAM: {e}"}, status_code=502)
    if r.status_code != 200 or not r.content:
        return JSONResponse({"error": "UPSTREAM", "status": r.status_code}, status_code=502)
    png = r.content

    # Persist into the durable, content-addressed EVIDENCE store — the SAME store
    # every ingested original uses (core.evidence.store → /app/storage/evidence,
    # on_disk_path), so satellite captures are first-class evidence served by the
    # existing GET /documents/{id}/source. store_original also writes a best-effort
    # signed .enki sidecar (chain of custody). Idempotent: same bytes dedup by SHA.
    window_end = time_range.split("/")[-1]
    title = f"Sentinel-2 {body.layer} @ {lat:.4f},{lon:.4f} ({window_end})"
    source_url = f"sat://sentinel2/{body.layer}/{lat:.5f},{lon:.5f}/{time_range}"
    try:
        from core.evidence import store as evidence
        ev = evidence.store_original(png, ext="png", source_url=source_url)
        sha = ev["sha256"]
        enki_side = str(evidence.path_for(sha, "enki")) if ev.get("signed") else None
        signed_at = datetime.now(timezone.utc) if ev.get("signed") else None
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT document_id FROM documents WHERE content_sha256 = %s LIMIT 1", (sha,))
                row = cur.fetchone()
                if row:
                    document_id = int(row[0])
                else:
                    cur.execute(
                        """INSERT INTO documents
                             (tenant_id, title, source_url, source_type, mime_type,
                              content_sha256, hash, on_disk_path, enki_path,
                              enki_signed_at, created_at)
                           VALUES (%s, %s, %s, 'satellite', 'image/png',
                              %s, %s, %s, %s, %s, NOW())
                           RETURNING document_id""",
                        (auth.tenant_id, title, source_url, sha, sha,
                         ev["path"], enki_side, signed_at),
                    )
                    document_id = int(cur.fetchone()[0])
            conn.commit()
    except Exception as e:
        log.exception("satellite evidence store failed: %s", e)
        return JSONResponse({"error": "EVIDENCE_STORE_FAILED", "detail": str(e)},
                            status_code=500)

    content_sha256 = sha
    document_url = f"/api/documents/{document_id}/source" if document_id else None

    # Attach to the dossier (entity_documents), if targeting an entity.
    doc_id = f"doc-{uuid.uuid4().hex[:12]}"
    if body.entity_id:
        col = _ENTITY_COL.get(body.entity_type, "node_id")
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""INSERT INTO entity_documents
                              (doc_id, tenant_id, {col}, title, doc_type, doc_category,
                               classification, description, file_url, file_name,
                               mime_type, source, issued_date, created_by)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (
                            doc_id, auth.tenant_id, str(body.entity_id), title,
                            "satellite_image", "imagery", "unclassified",
                            f"Overhead {body.layer} imagery, most recent clear pass within "
                            f"{time_range} (≤20% cloud, ~10 m/px).",
                            document_url, f"{body.layer.lower()}_{document_id}.png",
                            "image/png", "satellite", window_end, auth.subject_id,
                        ),
                    )
                conn.commit()
        except Exception as e:
            # Evidence is safely stored even if the dossier link fails.
            log.exception("dossier attach failed: %s", e)
            doc_id = None

    return {
        "doc_id": doc_id,
        "document_id": document_id,
        "document_url": document_url,
        "content_sha256": content_sha256,
        "layer": body.layer,
        "lat": lat, "lon": lon,
        "time_range": time_range,
    }


# ── Dates (Phase 2 Catalog API) ──────────────────────────────────────────────

@router.get("/dates")
def available_dates(
    lat: float = Query(...),
    lon: float = Query(...),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Available clear passes for a point. Stub in Phase 1 — Phase 2 fills this
    from the Copernicus Catalog (STAC) API to power before/after comparison."""
    return {"dates": [], "note": "Catalog API integration lands in Phase 2."}
