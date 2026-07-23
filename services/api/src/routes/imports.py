"""
Bulk import endpoints for contacts, assets, and observables.
POST /imports/contacts   — bulk insert contacts
POST /imports/assets     — bulk insert assets
POST /imports/observables — bulk insert observables for a case
"""

from fastapi import APIRouter, Depends, Header
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from pydantic import BaseModel
from typing import Optional
import logging
import uuid
from datetime import datetime, timezone

log = logging.getLogger("enki.imports")
router = APIRouter(prefix="/imports", tags=["imports"])


class BulkImportRequest(BaseModel):
    records: list[dict]
    case_id: Optional[str] = None  # for observables


@router.post("/contacts")
async def import_contacts(body: BulkImportRequest, auth: UnifiedAuthInfo = Depends(unified_auth)):
    from storage.postgres.db import get_connection
    imported = 0
    errors = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            for i, rec in enumerate(body.records):
                try:
                    contact_id = f"contact_{uuid.uuid4().hex[:12]}"
                    cur.execute("""
                        INSERT INTO contacts (contact_id, tenant_id, contact_type, primary_name, email, phone,
                            status, threat_level, tags, notes, role_title, company_name, address, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        contact_id, auth.tenant_id,
                        rec.get("contact_type", "person"),
                        rec.get("primary_name", "Unknown"),
                        rec.get("email"), rec.get("phone"),
                        rec.get("status", "active"),
                        rec.get("threat_level", "none"),
                        rec.get("tags", []),
                        rec.get("notes"),
                        rec.get("role_title"),
                        rec.get("company_name"),
                        rec.get("address"),
                        datetime.now(timezone.utc), datetime.now(timezone.utc),
                    ))
                    imported += 1
                except Exception as e:
                    errors.append({"row": i, "reason": str(e)})
            conn.commit()
    return {"imported": imported, "errors": errors}


@router.post("/assets")
async def import_assets(body: BulkImportRequest, auth: UnifiedAuthInfo = Depends(unified_auth)):
    from storage.postgres.db import get_connection
    imported = 0
    errors = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            for i, rec in enumerate(body.records):
                try:
                    asset_id = f"asset_{uuid.uuid4().hex[:12]}"
                    cur.execute("""
                        INSERT INTO assets (asset_id, tenant_id, asset_category, display_name, serial_number,
                            estimated_value, status, description, tags, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        asset_id, auth.tenant_id,
                        rec.get("asset_category", "other"),
                        rec.get("display_name", "Unknown"),
                        rec.get("serial_number"),
                        rec.get("estimated_value"),
                        rec.get("status", "active"),
                        rec.get("description"),
                        rec.get("tags", []),
                        datetime.now(timezone.utc), datetime.now(timezone.utc),
                    ))
                    imported += 1
                except Exception as e:
                    errors.append({"row": i, "reason": str(e)})
            conn.commit()
    return {"imported": imported, "errors": errors}


@router.post("/observables")
async def import_observables(body: BulkImportRequest, auth: UnifiedAuthInfo = Depends(unified_auth)):
    from storage.postgres.db import get_connection
    imported = 0
    errors = []
    case_id = body.case_id
    with get_connection() as conn:
        with conn.cursor() as cur:
            for i, rec in enumerate(body.records):
                try:
                    if case_id:
                        cur.execute("""
                            INSERT INTO case_observables (case_id, tenant_id, observable_type, observable_value,
                                notes, source, created_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """, (
                            case_id, auth.tenant_id,
                            rec.get("observable_type", "custom"),
                            rec.get("observable_value", ""),
                            rec.get("notes"),
                            "bulk_import",
                            datetime.now(timezone.utc),
                        ))
                    imported += 1
                except Exception as e:
                    errors.append({"row": i, "reason": str(e)})
            conn.commit()
    return {"imported": imported, "errors": errors}
