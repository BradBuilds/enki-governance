"""
Contact management endpoints.
POST   /contacts                          — create contact
GET    /contacts                          — list contacts
GET    /contacts/{contact_id}             — get contact
PATCH  /contacts/{contact_id}             — update contact
DELETE /contacts/{contact_id}             — delete contact
GET    /contacts/{contact_id}/documents   — list documents
POST   /contacts/{contact_id}/documents   — add document
DELETE /contacts/{contact_id}/documents/{doc_id} — delete document
GET    /contacts/{contact_id}/cases       — linked cases
POST   /contacts/{contact_id}/cases       — link case
GET    /contacts/{contact_id}/signals     — matching signals
GET    /contacts/{contact_id}/relationships — relationships
POST   /contacts/{contact_id}/relationships — add relationship
GET    /contacts/{contact_id}/assets      — linked assets
"""

from __future__ import annotations
from typing import Optional
from datetime import datetime, timezone
import uuid

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from pydantic import BaseModel

router = APIRouter(tags=["contacts"])


class CreateContactRequest(BaseModel):
    contact_type: str = "person"
    primary_name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    status: str = "active"
    threat_level: str = "none"
    tags: Optional[list[str]] = None
    notes: Optional[str] = None
    role_title: Optional[str] = None
    company_name: Optional[str] = None
    address: Optional[str] = None
    social_handles: Optional[dict] = None


class UpdateContactRequest(BaseModel):
    primary_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    status: Optional[str] = None
    threat_level: Optional[str] = None
    tags: Optional[list[str]] = None
    notes: Optional[str] = None
    role_title: Optional[str] = None
    company_name: Optional[str] = None
    address: Optional[str] = None


class AddDocumentRequest(BaseModel):
    title: str
    doc_type: str = "note"
    content: Optional[str] = None


class LinkCaseRequest(BaseModel):
    case_id: str
    relationship: str = "subject"


class AddRelationshipRequest(BaseModel):
    target_id: str
    relationship: str = "associate"
    description: Optional[str] = None


def _get_conn():
    from storage.postgres.db import get_connection
    return get_connection()


@router.post("/contacts")
async def create_contact(body: CreateContactRequest, auth: UnifiedAuthInfo = Depends(unified_auth)):
    contact_id = f"contact_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO contacts (contact_id, tenant_id, contact_type, primary_name, email, phone,
                    status, threat_level, tags, notes, role_title, address, social_handles,
                    created_by, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (contact_id, auth.tenant_id, body.contact_type, body.primary_name, body.email, body.phone,
                  body.status, body.threat_level, body.tags or [], body.notes, body.role_title,
                  body.address, str(body.social_handles) if body.social_handles else None,
                  "api", now, now))
            conn.commit()
    return JSONResponse(status_code=201, content={"contact_id": contact_id, "primary_name": body.primary_name})


@router.get("/contacts")
async def list_contacts(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    contact_type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(100, le=1000),
):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            conditions = ["tenant_id = %s"]
            params: list = [auth.tenant_id]
            if contact_type:
                conditions.append("contact_type = %s")
                params.append(contact_type)
            if status:
                conditions.append("status = %s")
                params.append(status)
            where = " AND ".join(conditions)
            cur.execute(f"""
                SELECT contact_id, contact_type, primary_name, email, phone, status, threat_level,
                    tags, notes, role_title, address, created_at, updated_at
                FROM contacts WHERE {where} ORDER BY updated_at DESC LIMIT %s
            """, params + [limit])
            rows = cur.fetchall()
            contacts = [{"contact_id": r[0], "contact_type": r[1], "primary_name": r[2], "email": r[3],
                         "phone": r[4], "status": r[5], "threat_level": r[6], "tags": r[7] or [],
                         "notes": r[8], "role_title": r[9], "address": r[10],
                         "created_at": r[11].isoformat() if r[11] else None,
                         "updated_at": r[12].isoformat() if r[12] else None} for r in rows]
    return {"contacts": contacts, "count": len(contacts)}


@router.get("/contacts/{contact_id}")
async def get_contact(contact_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT contact_id, contact_type, primary_name, email, phone, status, threat_level,
                    tags, notes, role_title, company_id, address, social_handles, aliases,
                    metadata, enrichment, created_at, updated_at
                FROM contacts WHERE contact_id = %s AND tenant_id = %s
            """, (contact_id, auth.tenant_id))
            r = cur.fetchone()
            if not r:
                return JSONResponse(status_code=404, content={"error": "Contact not found"})
            return {"contact_id": r[0], "contact_type": r[1], "primary_name": r[2], "email": r[3],
                    "phone": r[4], "status": r[5], "threat_level": r[6], "tags": r[7] or [],
                    "notes": r[8], "role_title": r[9], "company_id": r[10],
                    "address": r[11], "social_handles": r[12], "aliases": r[13] or [],
                    "metadata": r[14], "enrichment": r[15],
                    "created_at": r[16].isoformat() if r[16] else None,
                    "updated_at": r[17].isoformat() if r[17] else None}


@router.patch("/contacts/{contact_id}")
async def update_contact(contact_id: str, body: UpdateContactRequest, auth: UnifiedAuthInfo = Depends(unified_auth)):
    updates = {k: v for k, v in body.dict(exclude_none=True).items()}
    if not updates:
        return {"updated": False}
    updates["updated_at"] = datetime.now(timezone.utc)
    set_clause = ", ".join(f"{k} = %s" for k in updates)
    values = list(updates.values()) + [contact_id, auth.tenant_id]
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE contacts SET {set_clause} WHERE contact_id = %s AND tenant_id = %s", values)
            conn.commit()
    return {"updated": True}


@router.delete("/contacts/{contact_id}")
async def delete_contact(contact_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM contacts WHERE contact_id = %s AND tenant_id = %s", (contact_id, auth.tenant_id))
            conn.commit()
    return {"deleted": True}


@router.get("/contacts/{contact_id}/documents")
async def list_contact_documents(contact_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT document_id, title, doc_type, content, file_url, file_size, created_by, created_at
                FROM contact_documents WHERE contact_id = %s AND tenant_id = %s ORDER BY created_at DESC
            """, (contact_id, auth.tenant_id))
            docs = [{"document_id": r[0], "title": r[1], "doc_type": r[2], "content": r[3],
                     "file_url": r[4], "file_size": r[5], "created_by": r[6],
                     "created_at": r[7].isoformat() if r[7] else None} for r in cur.fetchall()]
    return {"documents": docs}


@router.post("/contacts/{contact_id}/documents")
async def add_contact_document(contact_id: str, body: AddDocumentRequest, auth: UnifiedAuthInfo = Depends(unified_auth)):
    doc_id = f"doc_{uuid.uuid4().hex[:12]}"
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO contact_documents (document_id, contact_id, tenant_id, title, doc_type, content, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            """, (doc_id, contact_id, auth.tenant_id, body.title, body.doc_type, body.content, datetime.now(timezone.utc)))
            conn.commit()
    return JSONResponse(status_code=201, content={"document_id": doc_id})


@router.delete("/contacts/{contact_id}/documents/{doc_id}")
async def delete_contact_document(contact_id: str, doc_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM contact_documents WHERE document_id = %s AND contact_id = %s AND tenant_id = %s",
                       (doc_id, contact_id, auth.tenant_id))
            conn.commit()
    return {"deleted": True}


@router.get("/contacts/{contact_id}/cases")
async def list_contact_cases(contact_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT cc.case_id, cc.relationship, c.title, c.status, c.priority
                FROM contact_cases cc JOIN cases c ON c.case_id = cc.case_id AND c.tenant_id = cc.tenant_id
                WHERE cc.contact_id = %s AND cc.tenant_id = %s
            """, (contact_id, auth.tenant_id))
            cases = [{"case_id": r[0], "relationship": r[1], "title": r[2], "status": r[3], "priority": r[4]} for r in cur.fetchall()]
    return {"cases": cases}


@router.post("/contacts/{contact_id}/cases")
async def link_contact_case(contact_id: str, body: LinkCaseRequest, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO contact_cases (contact_id, case_id, tenant_id, relationship, linked_at)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
            """, (contact_id, body.case_id, auth.tenant_id, body.relationship, datetime.now(timezone.utc)))
            conn.commit()
    return JSONResponse(status_code=201, content={"linked": True})


@router.get("/contacts/{contact_id}/signals")
async def list_contact_signals(contact_id: str, auth: UnifiedAuthInfo = Depends(unified_auth), limit: int = Query(50)):
    # Match signals via observable index (email, phone, name)
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT email, phone, primary_name FROM contacts WHERE contact_id = %s AND tenant_id = %s",
                       (contact_id, auth.tenant_id))
            contact = cur.fetchone()
            if not contact:
                return {"signals": []}
            email, phone, name = contact
            conditions = []
            params: list = [auth.tenant_id]
            if email:
                conditions.append("(oi.observable_type = 'email_address' AND oi.normalized_value = %s)")
                params.append(email.lower())
            if phone:
                conditions.append("(oi.observable_type = 'phone_number' AND oi.normalized_value = %s)")
                params.append(phone)
            if name:
                conditions.append("(oi.observable_type = 'person_name' AND oi.normalized_value = %s)")
                params.append(name.lower())
            if not conditions:
                return {"signals": []}
            or_clause = " OR ".join(conditions)
            cur.execute(f"""
                SELECT DISTINCT s.signal_id, s.signal_type, s.severity, s.timestamp
                FROM observable_index oi JOIN signals s ON s.rid = oi.signal_rid AND s.tenant_id = oi.tenant_id
                WHERE oi.tenant_id = %s AND ({or_clause})
                ORDER BY s.timestamp DESC LIMIT %s
            """, params + [limit])
            signals = [{"signal_id": r[0], "signal_type": r[1], "severity": r[2],
                        "timestamp": r[3].isoformat() if r[3] else None} for r in cur.fetchall()]
    return {"signals": signals}


@router.get("/contacts/{contact_id}/relationships")
async def list_contact_relationships(contact_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT cr.target_id, cr.relationship, cr.description, c.primary_name, c.contact_type
                FROM contact_relationships cr
                LEFT JOIN contacts c ON c.contact_id = cr.target_id AND c.tenant_id = cr.tenant_id
                WHERE cr.contact_id = %s AND cr.tenant_id = %s
            """, (contact_id, auth.tenant_id))
            rels = [{"target_id": r[0], "relationship": r[1], "description": r[2],
                     "target_name": r[3], "target_type": r[4]} for r in cur.fetchall()]
    return {"relationships": rels}


@router.post("/contacts/{contact_id}/relationships")
async def add_contact_relationship(contact_id: str, body: AddRelationshipRequest, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO contact_relationships (contact_id, target_id, tenant_id, relationship, description, created_at)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
            """, (contact_id, body.target_id, auth.tenant_id, body.relationship, body.description, datetime.now(timezone.utc)))
            conn.commit()
    return JSONResponse(status_code=201, content={"added": True})


@router.get("/contacts/{contact_id}/assets")
async def list_contact_assets(contact_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            # Assets linked via asset_contacts junction or entity_documents
            cur.execute("""
                SELECT a.asset_id, a.display_name, a.asset_category, a.status, a.estimated_value
                FROM assets a
                WHERE a.tenant_id = %s AND a.asset_id IN (
                    SELECT asset_id FROM entity_documents WHERE contact_id = %s AND tenant_id = %s
                    UNION
                    SELECT asset_id FROM entity_images WHERE contact_id = %s AND tenant_id = %s
                )
            """, (auth.tenant_id, contact_id, auth.tenant_id, contact_id, auth.tenant_id))
            assets = [{"asset_id": r[0], "display_name": r[1], "asset_category": r[2],
                       "status": r[3], "estimated_value": float(r[4]) if r[4] else None} for r in cur.fetchall()]
    return {"assets": assets}
