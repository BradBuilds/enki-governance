"""
Entity Profile API — dossier management for contacts, assets, and nodes.

GET    /entities/:id/identifiers       List all IDs for an entity
POST   /entities/:id/identifiers       Add an identifier (passport, DL, VIN, etc.)
PUT    /entities/identifiers/:id       Update identifier
DELETE /entities/identifiers/:id       Remove identifier

GET    /entities/:id/documents         List all filed documents
POST   /entities/:id/documents         File a document
GET    /entities/:id/images            List all photos/scans
POST   /entities/:id/images            Upload an image

POST   /entities/sync                  Sync all entities to knowledge nodes
POST   /entities/:id/sync              Sync single entity to knowledge node
GET    /entities/:id/dossier           Full dossier: profile + IDs + docs + images + node
"""

from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from pydantic import BaseModel

import json
import uuid

from storage.postgres.db import get_connection

router = APIRouter(prefix="/entities", tags=["entity-profiles"])


# ─── Models ──────────────────────────────────────────────────────────────────

class AddIdentifierRequest(BaseModel):
    entity_type: str            # contact, asset, node
    entity_id: str              # contact_id, asset_id, or node_id
    id_type: str                # passport, drivers_license, ssn, vin, mmsi, etc.
    id_number: str
    id_country: Optional[str] = None
    id_state: Optional[str] = None
    id_issuer: Optional[str] = None
    issue_date: Optional[str] = None
    expiry_date: Optional[str] = None
    scan_url: Optional[str] = None
    notes: Optional[str] = None


class FileDocumentRequest(BaseModel):
    entity_type: str
    entity_id: str
    title: str
    doc_type: str
    doc_category: str = "general"
    classification: str = "unclassified"
    description: Optional[str] = None
    content: Optional[str] = None
    file_url: Optional[str] = None
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    file_size: Optional[int] = None
    issued_by: Optional[str] = None
    issued_date: Optional[str] = None
    expiry_date: Optional[str] = None


class AddImageRequest(BaseModel):
    entity_type: str
    entity_id: str
    image_type: str             # profile, mugshot, passport_photo, vehicle_photo, etc.
    url: str
    thumbnail_url: Optional[str] = None
    caption: Optional[str] = None
    is_primary: bool = False


# ─── Identifiers ─────────────────────────────────────────────────────────────

@router.get("/{entity_id}/identifiers")
async def list_identifiers(
    entity_id: str,
    entity_type: str = Query("contact"),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    col = {"contact": "contact_id", "asset": "asset_id", "node": "node_id"}.get(entity_type, "contact_id")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT identifier_id, id_type, id_number, id_country, id_state, id_issuer,
                       issue_date, expiry_date, status, verified, scan_url, notes, created_at
                FROM entity_identifiers
                WHERE {col} = %s AND tenant_id = %s
                ORDER BY id_type, created_at
            """, (entity_id, auth.tenant_id))
            ids = [{
                "identifier_id": r[0], "id_type": r[1], "id_number": r[2],
                "id_country": r[3], "id_state": r[4], "id_issuer": r[5],
                "issue_date": r[6].isoformat() if r[6] else None,
                "expiry_date": r[7].isoformat() if r[7] else None,
                "status": r[8], "verified": r[9], "scan_url": r[10],
                "notes": r[11], "created_at": r[12].isoformat() if r[12] else None,
            } for r in cur.fetchall()]
    return {"identifiers": ids, "count": len(ids)}


@router.post("/{entity_id}/identifiers")
async def add_identifier(
    entity_id: str,
    body: AddIdentifierRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    identifier_id = f"eid-{uuid.uuid4().hex[:12]}"
    col_map = {"contact": "contact_id", "asset": "asset_id", "node": "node_id"}
    col = col_map.get(body.entity_type, "contact_id")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO entity_identifiers
                    (identifier_id, tenant_id, {col}, id_type, id_number,
                     id_country, id_state, id_issuer, issue_date, expiry_date,
                     scan_url, notes, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                identifier_id, auth.tenant_id, entity_id,
                body.id_type, body.id_number, body.id_country, body.id_state,
                body.id_issuer, body.issue_date, body.expiry_date,
                body.scan_url, body.notes, auth.subject_id,
            ))
        conn.commit()

    return {"identifier_id": identifier_id, "id_type": body.id_type}


@router.delete("/identifiers/{identifier_id}")
async def remove_identifier(
    identifier_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM entity_identifiers WHERE identifier_id = %s AND tenant_id = %s",
                       (identifier_id, auth.tenant_id))
        conn.commit()
    return {"deleted": True}


# ─── Documents ───────────────────────────────────────────────────────────────

@router.get("/{entity_id}/documents")
async def list_documents(
    entity_id: str,
    entity_type: str = Query("contact"),
    doc_category: Optional[str] = Query(None),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    col = {"contact": "contact_id", "asset": "asset_id", "case": "case_id", "node": "node_id"}.get(entity_type, "contact_id")
    conditions = [f"{col} = %s", "tenant_id = %s"]
    params = [entity_id, auth.tenant_id]
    if doc_category:
        conditions.append("doc_category = %s")
        params.append(doc_category)

    where = " AND ".join(conditions)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT doc_id, title, doc_type, doc_category, classification,
                       description, file_url, file_name, file_size, mime_type,
                       issued_by, issued_date, expiry_date, reviewed, tags, created_at
                FROM entity_documents WHERE {where}
                ORDER BY created_at DESC
            """, params)
            docs = [{
                "doc_id": r[0], "title": r[1], "doc_type": r[2], "doc_category": r[3],
                "classification": r[4], "description": r[5], "file_url": r[6],
                "file_name": r[7], "file_size": r[8], "mime_type": r[9],
                "issued_by": r[10],
                "issued_date": r[11].isoformat() if r[11] else None,
                "expiry_date": r[12].isoformat() if r[12] else None,
                "reviewed": r[13], "tags": r[14] or [],
                "created_at": r[15].isoformat() if r[15] else None,
            } for r in cur.fetchall()]
    return {"documents": docs, "count": len(docs)}


@router.post("/{entity_id}/documents")
async def file_document(
    entity_id: str,
    body: FileDocumentRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    doc_id = f"doc-{uuid.uuid4().hex[:12]}"
    col_map = {"contact": "contact_id", "asset": "asset_id", "case": "case_id", "node": "node_id"}
    col = col_map.get(body.entity_type, "contact_id")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO entity_documents
                    (doc_id, tenant_id, {col}, title, doc_type, doc_category,
                     classification, description, content, file_url, file_name,
                     mime_type, file_size, issued_by, issued_date, expiry_date, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                doc_id, auth.tenant_id, entity_id,
                body.title, body.doc_type, body.doc_category,
                body.classification, body.description, body.content,
                body.file_url, body.file_name, body.mime_type, body.file_size,
                body.issued_by, body.issued_date, body.expiry_date, auth.subject_id,
            ))
        conn.commit()

    return {"doc_id": doc_id, "title": body.title, "doc_type": body.doc_type}


# ─── Images ──────────────────────────────────────────────────────────────────

@router.get("/{entity_id}/images")
async def list_images(
    entity_id: str,
    entity_type: str = Query("contact"),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    col = {"contact": "contact_id", "asset": "asset_id", "node": "node_id"}.get(entity_type, "contact_id")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT image_id, image_type, url, thumbnail_url, caption,
                       taken_at, is_primary, verified, tags, created_at
                FROM entity_images WHERE {col} = %s AND tenant_id = %s
                ORDER BY is_primary DESC, created_at DESC
            """, (entity_id, auth.tenant_id))
            images = [{
                "image_id": r[0], "image_type": r[1], "url": r[2],
                "thumbnail_url": r[3], "caption": r[4],
                "taken_at": r[5].isoformat() if r[5] else None,
                "is_primary": r[6], "verified": r[7], "tags": r[8] or [],
                "created_at": r[9].isoformat() if r[9] else None,
            } for r in cur.fetchall()]
    return {"images": images, "count": len(images)}


@router.post("/{entity_id}/images")
async def add_image(
    entity_id: str,
    body: AddImageRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    image_id = f"img-{uuid.uuid4().hex[:12]}"
    col_map = {"contact": "contact_id", "asset": "asset_id", "node": "node_id"}
    col = col_map.get(body.entity_type, "contact_id")

    with get_connection() as conn:
        with conn.cursor() as cur:
            # If primary, unset other primaries for this entity
            if body.is_primary:
                cur.execute(f"""
                    UPDATE entity_images SET is_primary = FALSE
                    WHERE {col} = %s AND tenant_id = %s AND is_primary = TRUE
                """, (entity_id, auth.tenant_id))

            cur.execute(f"""
                INSERT INTO entity_images
                    (image_id, tenant_id, {col}, image_type, url, thumbnail_url,
                     caption, is_primary, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                image_id, auth.tenant_id, entity_id,
                body.image_type, body.url, body.thumbnail_url,
                body.caption, body.is_primary, auth.subject_id,
            ))
        conn.commit()

    return {"image_id": image_id, "image_type": body.image_type}


# ─── Full Dossier ────────────────────────────────────────────────────────────

@router.get("/{entity_id}/dossier")
async def get_dossier(
    entity_id: str,
    entity_type: str = Query("contact"),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Full intelligence dossier: profile + identifiers + documents + images + knowledge node."""
    col = {"contact": "contact_id", "asset": "asset_id", "node": "node_id"}.get(entity_type, "contact_id")

    profile = None
    with get_connection() as conn:
        with conn.cursor() as cur:
            if entity_type == "contact":
                cur.execute("""
                    SELECT contact_id, contact_type, primary_name, aliases, email, phone,
                           address, role_title, notes, threat_level, tags, status,
                           date_of_birth, place_of_birth, nationality, gender,
                           height_cm, weight_kg, eye_color, hair_color, distinguishing_marks,
                           occupation, employer, languages, last_known_location,
                           registration_number, registration_country, industry, website,
                           node_id, social_handles, avatar_url, created_at
                    FROM contacts WHERE contact_id = %s AND tenant_id = %s
                """, (entity_id, auth.tenant_id))
                r = cur.fetchone()
                if r:
                    profile = {
                        "contact_id": r[0], "contact_type": r[1], "primary_name": r[2],
                        "aliases": r[3] or [], "email": r[4], "phone": r[5],
                        "address": r[6], "role_title": r[7], "notes": r[8],
                        "threat_level": r[9], "tags": r[10] or [], "status": r[11],
                        "date_of_birth": r[12].isoformat() if r[12] else None,
                        "place_of_birth": r[13], "nationality": r[14], "gender": r[15],
                        "height_cm": r[16], "weight_kg": r[17], "eye_color": r[18],
                        "hair_color": r[19], "distinguishing_marks": r[20],
                        "occupation": r[21], "employer": r[22], "languages": r[23] or [],
                        "last_known_location": r[24],
                        "registration_number": r[25], "registration_country": r[26],
                        "industry": r[27], "website": r[28], "node_id": r[29],
                        "social_handles": r[30], "avatar_url": r[31],
                        "created_at": r[32].isoformat() if r[32] else None,
                    }
            elif entity_type == "asset":
                cur.execute("""
                    SELECT asset_id, display_name, description, asset_category, status,
                           estimated_value, serial_number, latitude, longitude,
                           vehicle_make, vehicle_model, vehicle_year, vehicle_vin, vehicle_plate, vehicle_color,
                           vessel_mmsi, vessel_imo, vessel_flag_state, vessel_type, vessel_owner,
                           aircraft_registration, aircraft_icao_hex, aircraft_model, aircraft_owner,
                           property_address, property_type, property_sqft,
                           node_id, tags, created_at
                    FROM assets WHERE asset_id = %s AND tenant_id = %s
                """, (entity_id, auth.tenant_id))
                r = cur.fetchone()
                if r:
                    profile = {
                        "asset_id": r[0], "display_name": r[1], "description": r[2],
                        "asset_category": r[3], "status": r[4], "estimated_value": float(r[5]) if r[5] else None,
                        "serial_number": r[6], "latitude": r[7], "longitude": r[8],
                        "vehicle": {"make": r[9], "model": r[10], "year": r[11], "vin": r[12], "plate": r[13], "color": r[14]} if r[9] else None,
                        "vessel": {"mmsi": r[15], "imo": r[16], "flag_state": r[17], "type": r[18], "owner": r[19]} if r[15] else None,
                        "aircraft": {"registration": r[20], "icao_hex": r[21], "model": r[22], "owner": r[23]} if r[20] else None,
                        "property": {"address": r[24], "type": r[25], "sqft": r[26]} if r[24] else None,
                        "node_id": r[27], "tags": r[28] or [],
                        "created_at": r[29].isoformat() if r[29] else None,
                    }

    if not profile:
        return JSONResponse(status_code=404, content={"error": "Entity not found"})

    # Get identifiers, documents, images in parallel queries
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM entity_identifiers WHERE {col} = %s AND tenant_id = %s", (entity_id, auth.tenant_id))
            id_count = cur.fetchone()[0]

            cur.execute(f"SELECT COUNT(*) FROM entity_documents WHERE {col} = %s AND tenant_id = %s", (entity_id, auth.tenant_id))
            doc_count = cur.fetchone()[0]

            cur.execute(f"SELECT COUNT(*) FROM entity_images WHERE {col} = %s AND tenant_id = %s", (entity_id, auth.tenant_id))
            img_count = cur.fetchone()[0]

            # Get linked cases
            if entity_type == "contact":
                cur.execute("""
                    SELECT cc.case_id, c.title, cc.relationship
                    FROM contact_cases cc JOIN cases c ON c.case_id = cc.case_id
                    WHERE cc.contact_id = %s AND cc.tenant_id = %s
                """, (entity_id, auth.tenant_id))
            elif entity_type == "asset":
                cur.execute("""
                    SELECT ac.case_id, c.title, 'asset' as relationship
                    FROM asset_cases ac JOIN cases c ON c.case_id = ac.case_id
                    WHERE ac.asset_id = %s AND ac.tenant_id = %s
                """, (entity_id, auth.tenant_id))
            else:
                cur.execute("SELECT NULL LIMIT 0")

            cases = [{"case_id": r[0], "title": r[1], "relationship": r[2]} for r in cur.fetchall()]

    # Get knowledge node if linked
    node_data = None
    node_id = profile.get("node_id")
    if node_id:
        from core.knowledge.graph import get_node
        node_data = get_node(node_id, auth.tenant_id)

    return {
        "entity_type": entity_type,
        "profile": profile,
        "identifiers_count": id_count,
        "documents_count": doc_count,
        "images_count": img_count,
        "cases": cases,
        "knowledge_node": node_data,
    }


# ─── Entity Sync ─────────────────────────────────────────────────────────────

@router.post("/sync")
async def sync_all(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Sync all contacts, assets, accounts to knowledge nodes."""
    from core.enrichment.entity_sync import sync_all_entities
    result = sync_all_entities(auth.tenant_id)
    return result


@router.post("/{entity_id}/sync")
async def sync_entity(
    entity_id: str,
    entity_type: str = Query("contact"),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Sync a single entity to its knowledge node."""
    from core.enrichment.entity_sync import sync_contact_to_node, sync_asset_to_node, sync_account_to_node

    if entity_type == "contact":
        node_id = sync_contact_to_node(entity_id, auth.tenant_id)
    elif entity_type == "asset":
        node_id = sync_asset_to_node(entity_id, auth.tenant_id)
    elif entity_type == "account":
        node_id = sync_account_to_node(entity_id, auth.tenant_id)
    else:
        return JSONResponse(status_code=400, content={"error": f"Unknown entity_type: {entity_type}"})

    return {"synced": bool(node_id), "node_id": node_id}


# ─── Real Research Endpoint ────────────────────────────────────────────────────

@router.post("/research/entity")
async def research_entity(body: dict, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Research an entity using real public data sources (Wikipedia, Wikidata, SEC, etc.)."""
    name = body.get("name", "").strip()
    entity_type = body.get("entity_type", "person")

    if not name:
        return JSONResponse(status_code=400, content={"error": "name is required"})

    # Resolve canonical name before research (demonym/abbreviation cleanup)
    from core.extraction.entity_cleanup import resolve_canonical_name
    canonical, resolution = resolve_canonical_name(name)
    if resolution == "source_filter":
        return {"status": "complete", "profile": {"full_name": name, "is_news_source": True,
                "biography": f"{name} is a news source, not an intelligence entity.", "data_sources": []}}
    if resolution in ("demonym", "abbreviation"):
        name = canonical  # Research the canonical form

    from core.research.deep_profile import (
        deep_research_person, deep_research_organization, deep_research_location,
    )
    from core.research.sources import research_address

    if entity_type in ("person",):
        raw = deep_research_person(name, tenant_id=auth.tenant_id)
    elif entity_type in ("organization",):
        raw = deep_research_organization(name, tenant_id=auth.tenant_id)
    elif entity_type in ("location", "country", "city", "region", "facility"):
        raw = deep_research_location(name, tenant_id=auth.tenant_id)
    elif entity_type == "address":
        raw = research_address(name)
    else:
        raw = deep_research_person(name, tenant_id=auth.tenant_id)

    # Helper: Wikidata properties can be strings or lists — normalize to string
    def _str(val):
        if isinstance(val, list):
            return val[0] if val else ""
        return str(val) if val else ""

    # Transform to the profile format the frontend expects
    profile = {
        "full_name": _str(raw.get("name", name)),
        "biography": _str(raw.get("biography", "")),
        "description": _str(raw.get("description", "")),
        "nationality": _str(raw.get("nationality")),
        "date_of_birth": _str(raw.get("date_of_birth")),
        "current_role": _str(raw.get("current_role") or raw.get("occupation")),
        "organization": _str(raw.get("organization") or raw.get("employer")),
        "education": [],
        "career_history": [],
        "social_handles": {},
        "notable_actions": [],
        "key_associations": [],
        "conflicts": [],
        "images": [],
        "links": [],
        "knowledge_nodes_created": [],
        "data_sources": raw.get("data_sources", []),
    }

    # Deep career data (from Wikidata positions + employers)
    if raw.get("career"):
        for job in raw["career"]:
            profile["career_history"].append({
                "role": job.get("role", ""),
                "organization": job.get("of", ""),
                "period": f"{job.get('start', '?')} – {job.get('end', 'present') if not job.get('current') else 'present'}",
                "current": job.get("current", False),
                "source": job.get("source", "Wikidata"),
            })

    # Deep education data
    if raw.get("education") and isinstance(raw["education"], list):
        for edu in raw["education"]:
            if isinstance(edu, dict):
                profile["education"].append({
                    "institution": edu.get("institution", ""),
                    "degree": edu.get("degree", ""),
                    "field": edu.get("field", ""),
                    "period": f"{edu.get('start', '')} – {edu.get('end', '')}".strip(" –"),
                    "source": edu.get("source", "Wikidata"),
                })
            else:
                profile["education"].append({"institution": str(edu), "source": "Wikidata"})
    elif raw.get("education") and isinstance(raw["education"], str):
        profile["education"].append({"institution": raw["education"], "source": "Wikidata"})

    # Social handles
    if raw.get("twitter"):
        profile["social_handles"]["twitter"] = raw["twitter"]
    if raw.get("website"):
        profile["social_handles"]["website"] = raw["website"]

    # Family
    if raw.get("family"):
        profile["family"] = raw["family"]

    # Awards
    if raw.get("awards"):
        profile["awards"] = raw["awards"]

    # All aliases (from Wikidata - every language variant)
    if raw.get("all_aliases"):
        profile["all_aliases"] = raw["all_aliases"]

    # Timeline (chronological events from all sources)
    if raw.get("timeline"):
        profile["timeline"] = raw["timeline"]

    # GDELT articles (news mentions from the global archive)
    if raw.get("gdelt_articles"):
        profile["gdelt_articles"] = raw["gdelt_articles"]

    # Mention volume over time (GDELT timeline)
    if raw.get("mention_volume"):
        profile["mention_volume"] = raw["mention_volume"]

    # Our own database mentions
    if raw.get("own_mentions"):
        profile["own_mentions"] = raw["own_mentions"]

    # Connections (people/orgs linked to this entity)
    if raw.get("connections"):
        profile["key_associations"] = raw["connections"]

    # Person extended fields
    if raw.get("place_of_birth"):
        profile["place_of_birth"] = raw["place_of_birth"]
    if raw.get("date_of_death"):
        profile["date_of_death"] = raw["date_of_death"]
    if raw.get("religion"):
        profile["religion"] = raw["religion"]
    if raw.get("sex_or_gender"):
        profile["sex_or_gender"] = raw["sex_or_gender"]

    # Organization-specific fields
    if raw.get("cik") or (raw.get("sec") and raw["sec"].get("cik")):
        profile["cik"] = raw.get("cik") or raw["sec"]["cik"]
    if raw.get("sec"):
        sec = raw["sec"]
        profile["sec"] = {
            "company_name": sec.get("company_name"),
            "sic": sec.get("sic"),
            "sic_description": sec.get("sic_description"),
            "state_of_incorporation": sec.get("state"),
            "tickers": sec.get("tickers", []),
            "exchanges": sec.get("exchanges", []),
            "business_address": sec.get("business_address"),
            "filings": sec.get("filings", [])[:20],
            "financials": sec.get("financials", {}),
        }
    if raw.get("inception"):
        profile["founded"] = raw["inception"]
    if raw.get("headquarters"):
        profile["headquarters"] = raw["headquarters"]
    if raw.get("ceo"):
        profile["ceo"] = raw["ceo"]
    if raw.get("parent_org"):
        profile["parent_org"] = raw["parent_org"]
    if raw.get("owned_by"):
        profile["owned_by"] = raw["owned_by"]
    if raw.get("subsidiaries"):
        profile["subsidiaries"] = raw["subsidiaries"]

    # Location-specific fields
    if raw.get("population"):
        profile["population"] = _str(raw["population"])
    if raw.get("area_km2"):
        profile["area_km2"] = _str(raw["area_km2"])
    if raw.get("head_of_state"):
        profile["head_of_state"] = _str(raw["head_of_state"])
    if raw.get("head_of_government"):
        profile["head_of_government"] = _str(raw["head_of_government"])
    if raw.get("capital"):
        profile["capital"] = _str(raw["capital"])
    if raw.get("continent"):
        profile["continent"] = _str(raw["continent"])
    if raw.get("currency"):
        profile["currency"] = _str(raw["currency"])
    if raw.get("official_language"):
        profile["official_language"] = _str(raw["official_language"])
    if raw.get("iso_3166"):
        profile["iso_3166"] = _str(raw["iso_3166"])
    if raw.get("latitude"):
        profile["latitude"] = raw["latitude"]
        profile["longitude"] = raw.get("longitude")
    if raw.get("timezone"):
        profile["timezone"] = raw["timezone"]
    if raw.get("structured_address"):
        profile["structured_address"] = raw["structured_address"]
    if raw.get("display_name"):
        profile["display_name"] = raw["display_name"]
    if raw.get("osm_type"):
        profile["osm_type"] = raw["osm_type"]

    # Images
    if raw.get("image_url"):
        profile["images"].append({"url": raw["image_url"], "caption": name, "source": "Wikimedia Commons"})
    if raw.get("thumbnail"):
        profile["images"].append({"url": raw["thumbnail"], "caption": f"{name} thumbnail", "source": "Wikipedia"})
    if raw.get("flag_url"):
        profile["images"].append({"url": raw["flag_url"], "caption": f"Flag of {name}", "source": "Wikimedia Commons"})

    # Links
    if raw.get("wikipedia_url"):
        profile["links"].append({"url": raw["wikipedia_url"], "label": "Wikipedia", "source": "wikipedia"})
    if raw.get("wikidata_id"):
        profile["links"].append({"url": f"https://www.wikidata.org/wiki/{raw['wikidata_id']}", "label": "Wikidata", "source": "wikidata"})
    if raw.get("website"):
        profile["links"].append({"url": raw["website"], "label": "Official Website", "source": "wikidata"})

    # Sanctions
    if raw.get("sanctions"):
        profile["sanctions"] = raw["sanctions"]
        if raw["sanctions"].get("sanctioned"):
            profile["conflicts"].append({
                "claim": "Entity appears on sanctions lists",
                "sources": [m.get("name", "") for m in raw["sanctions"].get("matches", [])],
                "severity": "high",
            })

    # Contested claims / controversies (from GDELT + Wikipedia controversy sections)
    if raw.get("contested_claims"):
        for claim in raw["contested_claims"]:
            profile["conflicts"].append({
                "claim": claim.get("claim", ""),
                "source": claim.get("source", ""),
                "date": claim.get("date", ""),
                "url": claim.get("url"),
                "section": claim.get("section"),
                "category": claim.get("category", "controversy"),
                "epistemic_type": "claim",
                "confidence": claim.get("confidence", 0.3),
            })

    # Write deep research findings as knowledge nodes (GDU-governed)
    if raw.get("contested_claims") or raw.get("timeline"):
        try:
            _persist_research_nodes(name, entity_type, raw, auth.tenant_id)
        except Exception as e:
            import logging
            logging.getLogger("enki.research").debug("Failed to persist research nodes: %s", e)

    return {"status": "complete", "profile": profile}


def _persist_research_nodes(name: str, entity_type: str, raw: dict, tenant_id: str):
    """Write deep research findings as knowledge_nodes with proper epistemic typing."""
    import uuid
    from storage.postgres.db import get_connection

    # Find canonical entity_id
    entity_id = None
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT entity_id FROM entities
                WHERE tenant_id = %s AND normalized_name = LOWER(%s) AND canonical_type = %s
                LIMIT 1
            """, (tenant_id, name, entity_type))
            row = cur.fetchone()
            if row:
                entity_id = row[0]

            if not entity_id:
                return

            # Write biography as an observation node (GDU governed)
            if raw.get("biography"):
                from core.gdu.enforce import admit_node as gdu_admit_node
                bio_node_id = f"node-bio-{uuid.uuid4().hex[:12]}"
                gdu_admit_node(
                    tenant_id=tenant_id, category="assessment", subtype="observation",
                    title=f"Profile: {name}"[:200], description=raw["biography"][:500],
                    signal_rid=0, collapse_key=bio_node_id,
                )
                cur.execute("""
                    INSERT INTO knowledge_nodes
                        (node_id, tenant_id, title, description, epistemic_type,
                         master_entity_id, entity_type, entity_name,
                         source_authority, confidence, enrichment_status, metadata,
                         gdu_canonical)
                    VALUES (%s, %s, %s, %s, 'observation', %s, %s, %s,
                            'trusted', 0.85, 'complete', %s, TRUE)
                    ON CONFLICT (node_id) DO UPDATE SET
                        description = EXCLUDED.description,
                        gdu_canonical = TRUE, updated_at = NOW()
                """, (
                    bio_node_id, tenant_id,
                    f"Profile: {name}", raw["biography"][:2000],
                    entity_id, entity_type, name,
                    json.dumps({"source": "deep_research", "data_sources": raw.get("data_sources", [])}),
                ))

            # Write contested claims as claim nodes (GDU governed)
            for claim in raw.get("contested_claims", []):
                from core.gdu.enforce import admit_node as gdu_admit_node
                claim_node_id = f"node-claim-{uuid.uuid4().hex[:12]}"
                gdu_admit_node(
                    tenant_id=tenant_id, category="assessment", subtype="claim",
                    title=f"Claim: {claim.get('claim', '')[:100]}"[:200],
                    description=claim.get("claim", "")[:500],
                    signal_rid=0, collapse_key=claim_node_id,
                )
                cur.execute("""
                    INSERT INTO knowledge_nodes
                        (node_id, tenant_id, title, description, epistemic_type,
                         master_entity_id, entity_type, entity_name,
                         source_authority, confidence, enrichment_status, metadata,
                         gdu_canonical)
                    VALUES (%s, %s, %s, %s, 'claim', %s, %s, %s,
                            'unverified', %s, 'complete', %s, TRUE)
                    ON CONFLICT (node_id) DO UPDATE SET
                        description = EXCLUDED.description,
                        gdu_canonical = TRUE, updated_at = NOW()
                """, (
                    claim_node_id, tenant_id,
                    f"Claim: {claim.get('claim', '')[:100]}", claim.get("claim", "")[:2000],
                    entity_id, entity_type, name,
                    claim.get("confidence", 0.3),
                    json.dumps({
                        "source": claim.get("source", ""),
                        "category": claim.get("category", "controversy"),
                        "url": claim.get("url"),
                        "date": claim.get("date"),
                        "epistemic_note": "contested claim from deep research — NOT verified fact",
                    }),
                ))

        conn.commit()
