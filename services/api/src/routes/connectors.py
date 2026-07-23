"""
Connector API endpoints.

POST   /connectors           Create connector
GET    /connectors           List connectors
GET    /connectors/{id}      Connector detail
POST   /connectors/{id}/fetch  Manually trigger fetch
"""

from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from pydantic import BaseModel

from storage.connectors.manager import (
    create_connector, get_connector, list_connectors, trigger_connector,
)
from storage.ingest.signal_ingestion import ingest_signal
from storage.secrets import resolve_secrets

router = APIRouter(prefix="/connectors", tags=["connectors"])


class CreateConnectorRequest(BaseModel):
    device_id: str
    connector_type: str = "pull"
    source_type: str
    source_config: dict
    poll_interval: Optional[str] = None


@router.post("")
async def create_connector_endpoint(
    body: CreateConnectorRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    result = create_connector(
        tenant_id=auth.tenant_id, device_id=body.device_id,
        connector_type=body.connector_type, source_type=body.source_type,
        source_config=body.source_config, poll_interval=body.poll_interval,
    )
    return JSONResponse(status_code=201, content=result)


@router.get("")
async def list_connectors_endpoint(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    connectors = list_connectors(auth.tenant_id)
    return {"connectors": connectors, "count": len(connectors)}


@router.get("/{connector_id}")
async def get_connector_endpoint(
    connector_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    conn = get_connector(connector_id, auth.tenant_id)
    if not conn:
        return JSONResponse(status_code=404, content={"error": "CONNECTOR.NOT_FOUND"})
    return conn


@router.post("/{connector_id}/fetch")
async def trigger_fetch_endpoint(
    connector_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Manually trigger a connector fetch cycle."""
    result = trigger_connector(connector_id, auth.tenant_id)
    if result is None:
        return JSONResponse(status_code=404, content={"error": "CONNECTOR.NOT_FOUND"})
    return {
        "connector_id": connector_id,
        "items_fetched": result.items_fetched,
        "items_ingested": result.items_ingested,
        "errors": result.errors,
    }


@router.post("/test-connection")
async def test_connection_endpoint(
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Test a data source connection without creating a connector.

    Resolves $secret:name references in source_config before testing.
    """
    body = await request.json()
    source_type = body.get("source_type", "")

    # Resolve secret references in the config
    if "source_config" in body:
        body["source_config"] = resolve_secrets(body["source_config"], auth.tenant_id)

    if source_type == "database_query":
        from storage.connectors.database import test_database_connection
        result = test_database_connection(body.get("source_config", {}))
        status = 200 if result.get("success") else 400
        return JSONResponse(status_code=status, content=result)

    if source_type in ("http_api", "rss_feed"):
        # Quick test: try to fetch one page
        try:
            from urllib.request import urlopen, Request as URLRequest
            from services.api.src.routes.sources import _is_safe_url
            url = body.get("source_config", {}).get("url", "")
            if not url:
                return JSONResponse(status_code=400, content={"success": False, "error": "No URL provided"})
            if not _is_safe_url(url):
                return JSONResponse(status_code=400, content={"success": False, "error": "URL targets a private or internal network"})
            req = URLRequest(url, headers={"User-Agent": "Enki/1.0"})
            with urlopen(req, timeout=10) as resp:
                preview = resp.read(4000).decode("utf-8", errors="replace")
            return {"success": True, "preview": preview[:500], "status_code": resp.status}
        except Exception as e:
            return JSONResponse(status_code=400, content={"success": False, "error": str(e)})

    return JSONResponse(status_code=400, content={"success": False, "error": f"Unknown source_type: {source_type}"})


@router.post("/upload")
async def upload_file(
    request: Request,
    x_device_id: str = Header("upload", alias="X-Device-ID"),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """
    Upload a file for ingestion (drag-and-drop).
    Accepts any file type — the pipeline auto-detects format.
    """
    body = await request.body()
    content_type = request.headers.get("content-type", "application/octet-stream")

    if len(body) > 50_000_000:  # 50MB limit for file upload
        return JSONResponse(status_code=413, content={
            "error": "FILE.TOO_LARGE", "detail": "File exceeds 50MB limit",
        })

    if not body:
        return JSONResponse(status_code=400, content={
            "error": "FILE.EMPTY", "detail": "No file content",
        })

    result = ingest_signal(
        raw_bytes=body,
        device_id=x_device_id,
        tenant_id=auth.tenant_id,
        content_type=content_type,
        source_type="file_drop",
    )

    return JSONResponse(status_code=201, content={
        "status": "accepted" if result.path == "ldu_w" else "provisioned",
        "signal_id": result.signal_id,
        "ldu_t_id": result.ldu_t_id,
        "signal_type": result.signal_type,
        "format_detected": result.format_detected,
    })
