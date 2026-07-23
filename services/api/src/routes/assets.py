"""
Asset management endpoints.
POST   /assets                         — create asset
GET    /assets                         — list assets
GET    /assets/{asset_id}              — get asset
PATCH  /assets/{asset_id}              — update asset
DELETE /assets/{asset_id}              — delete asset
POST   /assets/{asset_id}/properties   — add property
GET    /assets/{asset_id}/documents    — list documents
POST   /assets/{asset_id}/documents    — add document
DELETE /assets/{asset_id}/documents/{doc_id} — delete document
POST   /assets/{asset_id}/contacts     — link contact
GET    /assets/{asset_id}/contacts     — linked contacts
POST   /assets/{asset_id}/cases        — link case
GET    /assets/{asset_id}/cases        — linked cases
"""

from __future__ import annotations
from typing import Optional
from datetime import datetime, timezone
import uuid
import json

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from pydantic import BaseModel

router = APIRouter(tags=["assets"])


class CreateAssetRequest(BaseModel):
    asset_category: str = "other"
    display_name: str
    serial_number: Optional[str] = None
    estimated_value: Optional[float] = None
    status: str = "active"
    description: Optional[str] = None
    tags: Optional[list[str]] = None


class UpdateAssetRequest(BaseModel):
    display_name: Optional[str] = None
    asset_category: Optional[str] = None
    status: Optional[str] = None
    estimated_value: Optional[float] = None
    serial_number: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[list[str]] = None


class AddPropertyRequest(BaseModel):
    key: str
    value: str


class AddDocumentRequest(BaseModel):
    title: str
    doc_type: str = "photo"
    content: Optional[str] = None


class LinkContactRequest(BaseModel):
    contact_id: str
    relationship: str = "owner"


class LinkCaseRequest(BaseModel):
    case_id: str
    relationship: str = "evidence"


def _get_conn():
    from storage.postgres.db import get_connection
    return get_connection()


@router.post("/assets")
async def create_asset(body: CreateAssetRequest, auth: UnifiedAuthInfo = Depends(unified_auth)):
    asset_id = f"asset_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO assets (asset_id, tenant_id, asset_category, display_name, serial_number,
                    estimated_value, status, description, tags, properties, observables, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (asset_id, auth.tenant_id, body.asset_category, body.display_name, body.serial_number,
                  body.estimated_value, body.status, body.description, body.tags or [], '[]', '[]', now, now))
            conn.commit()
    return JSONResponse(status_code=201, content={"asset_id": asset_id, "display_name": body.display_name})


@router.get("/assets")
async def list_assets(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    category: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(100, le=1000),
):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            conditions = ["tenant_id = %s"]
            params: list = [auth.tenant_id]
            if category:
                conditions.append("asset_category = %s")
                params.append(category)
            if status:
                conditions.append("status = %s")
                params.append(status)
            where = " AND ".join(conditions)
            cur.execute(f"""
                SELECT asset_id, asset_category, display_name, serial_number, estimated_value,
                    status, description, tags, observables, properties, created_at, updated_at
                FROM assets WHERE {where} ORDER BY updated_at DESC LIMIT %s
            """, params + [limit])
            assets = []
            for r in cur.fetchall():
                props = r[9]
                if isinstance(props, str):
                    try:
                        props = json.loads(props)
                    except Exception:
                        props = []
                assets.append({
                    "asset_id": r[0], "asset_category": r[1], "display_name": r[2], "serial_number": r[3],
                    "estimated_value": float(r[4]) if r[4] else None, "status": r[5], "description": r[6],
                    "tags": r[7] or [], "observables": r[8] or [], "properties": props or [],
                    "created_at": r[9] if isinstance(r[9], str) else (r[10].isoformat() if r[10] else None),
                    "updated_at": r[11].isoformat() if r[11] else None,
                })
    return {"assets": assets, "count": len(assets)}


@router.get("/assets/{asset_id}")
async def get_asset(asset_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT asset_id, asset_category, display_name, serial_number, estimated_value,
                    status, description, tags, observables, properties, latitude, longitude,
                    created_at, updated_at
                FROM assets WHERE asset_id = %s AND tenant_id = %s
            """, (asset_id, auth.tenant_id))
            r = cur.fetchone()
            if not r:
                return JSONResponse(status_code=404, content={"error": "Asset not found"})
            props = r[9]
            if isinstance(props, str):
                try:
                    props = json.loads(props)
                except Exception:
                    props = []
            return {
                "asset_id": r[0], "asset_category": r[1], "display_name": r[2], "serial_number": r[3],
                "estimated_value": float(r[4]) if r[4] else None, "status": r[5], "description": r[6],
                "tags": r[7] or [], "observables": r[8] or [], "properties": props or [],
                "latitude": r[10], "longitude": r[11],
                "created_at": r[12].isoformat() if r[12] else None,
                "updated_at": r[13].isoformat() if r[13] else None,
            }


@router.patch("/assets/{asset_id}")
async def update_asset(asset_id: str, body: UpdateAssetRequest, auth: UnifiedAuthInfo = Depends(unified_auth)):
    updates = {k: v for k, v in body.dict(exclude_none=True).items()}
    if not updates:
        return {"updated": False}
    updates["updated_at"] = datetime.now(timezone.utc)
    set_clause = ", ".join(f"{k} = %s" for k in updates)
    values = list(updates.values()) + [asset_id, auth.tenant_id]
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE assets SET {set_clause} WHERE asset_id = %s AND tenant_id = %s", values)
            conn.commit()
    return {"updated": True}


@router.delete("/assets/{asset_id}")
async def delete_asset(asset_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM assets WHERE asset_id = %s AND tenant_id = %s", (asset_id, auth.tenant_id))
            conn.commit()
    return {"deleted": True}


@router.post("/assets/{asset_id}/properties")
async def add_asset_property(asset_id: str, body: AddPropertyRequest, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            # Append to JSONB properties array
            new_prop = json.dumps({"key": body.key, "value": body.value, "source": "manual",
                                   "confidence": 1.0, "discovered_at": datetime.now(timezone.utc).isoformat()})
            cur.execute("""
                UPDATE assets SET properties = COALESCE(properties, '[]'::jsonb) || %s::jsonb,
                    updated_at = NOW()
                WHERE asset_id = %s AND tenant_id = %s
            """, (f'[{new_prop}]', asset_id, auth.tenant_id))
            conn.commit()
    return {"added": True}


@router.get("/assets/{asset_id}/documents")
async def list_asset_documents(asset_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT document_id, title, doc_type, content, file_url, file_size, mime_type, created_by, created_at
                FROM entity_documents WHERE asset_id = %s AND tenant_id = %s ORDER BY created_at DESC
            """, (asset_id, auth.tenant_id))
            docs = [{"doc_id": r[0], "title": r[1], "doc_type": r[2], "content": r[3],
                     "file_url": r[4], "file_size": r[5], "mime_type": r[6], "created_by": r[7],
                     "created_at": r[8].isoformat() if r[8] else None} for r in cur.fetchall()]
    return {"documents": docs}


@router.post("/assets/{asset_id}/documents")
async def add_asset_document(asset_id: str, body: AddDocumentRequest, auth: UnifiedAuthInfo = Depends(unified_auth)):
    doc_id = f"doc_{uuid.uuid4().hex[:12]}"
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO entity_documents (document_id, asset_id, tenant_id, title, doc_type, content, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            """, (doc_id, asset_id, auth.tenant_id, body.title, body.doc_type, body.content, datetime.now(timezone.utc)))
            conn.commit()
    return JSONResponse(status_code=201, content={"doc_id": doc_id})


@router.delete("/assets/{asset_id}/documents/{doc_id}")
async def delete_asset_document(asset_id: str, doc_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM entity_documents WHERE document_id = %s AND asset_id = %s AND tenant_id = %s",
                       (doc_id, asset_id, auth.tenant_id))
            conn.commit()
    return {"deleted": True}


@router.post("/assets/{asset_id}/contacts")
async def link_asset_contact(asset_id: str, body: LinkContactRequest, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO entity_documents (document_id, asset_id, contact_id, tenant_id, title, doc_type, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
            """, (f"link_{uuid.uuid4().hex[:8]}", asset_id, body.contact_id, auth.tenant_id,
                  f"Link: {body.relationship}", "link", datetime.now(timezone.utc)))
            conn.commit()
    return JSONResponse(status_code=201, content={"linked": True})


@router.get("/assets/{asset_id}/contacts")
async def list_asset_contacts(asset_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT c.contact_id, c.primary_name, c.contact_type, ed.title as relationship
                FROM entity_documents ed
                JOIN contacts c ON c.contact_id = ed.contact_id AND c.tenant_id = ed.tenant_id
                WHERE ed.asset_id = %s AND ed.tenant_id = %s AND ed.contact_id IS NOT NULL
            """, (asset_id, auth.tenant_id))
            contacts = [{"contact_id": r[0], "primary_name": r[1], "contact_type": r[2],
                         "relationship": r[3]} for r in cur.fetchall()]
    return {"contacts": contacts}


@router.post("/assets/{asset_id}/cases")
async def link_asset_case(asset_id: str, body: LinkCaseRequest, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO case_documents (document_id, case_id, tenant_id, title, doc_type, content, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
            """, (f"alink_{uuid.uuid4().hex[:8]}", body.case_id, auth.tenant_id,
                  f"Asset: {asset_id} ({body.relationship})", "asset_link", asset_id, datetime.now(timezone.utc)))
            conn.commit()
    return JSONResponse(status_code=201, content={"linked": True})


@router.get("/assets/{asset_id}/cases")
async def list_asset_cases(asset_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.case_id, c.title, c.status, c.priority, cd.title as relationship
                FROM case_documents cd
                JOIN cases c ON c.case_id = cd.case_id AND c.tenant_id = cd.tenant_id
                WHERE cd.content = %s AND cd.tenant_id = %s AND cd.doc_type = 'asset_link'
            """, (asset_id, auth.tenant_id))
            cases = [{"case_id": r[0], "title": r[1], "status": r[2], "priority": r[3],
                      "relationship": r[4]} for r in cur.fetchall()]
    return {"cases": cases}
