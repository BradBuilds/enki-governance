"""
Card template API endpoints.

GET  /card-templates           — list all templates for this tenant
GET  /card-templates/{type}    — get template for a signal type
PATCH /card-templates/{type}   — update column visibility / media config
"""

from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from pydantic import BaseModel

from storage.signals.card_templates import (
    get_template,
    list_templates,
    update_template_columns,
    _ai_suggest_layout,
    _rule_based_template,
)

router = APIRouter(prefix="/card-templates", tags=["card templates"])


class UpdateColumnsRequest(BaseModel):
    columns: list[dict]
    media_config: Optional[list[dict]] = None


@router.get("")
async def list_all(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """List all card templates for this tenant."""
    templates = list_templates(auth.tenant_id)
    return {"templates": templates, "total": len(templates)}


@router.get("/{signal_type:path}")
async def get_one(signal_type: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Get the card template for a specific signal type."""
    template = get_template(signal_type, auth.tenant_id)
    if not template:
        return JSONResponse(status_code=404, content={"error": "TEMPLATE.NOT_FOUND"})
    return template


@router.patch("/{signal_type:path}")
async def update_columns(
    signal_type: str,
    body: UpdateColumnsRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Update which columns are visible/hidden and media configuration."""
    ok = update_template_columns(signal_type, body.columns, body.media_config, auth.tenant_id)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "TEMPLATE.NOT_FOUND"})
    return {"updated": True, "signal_type": signal_type}


@router.delete("/{signal_type:path}")
async def delete_template(
    signal_type: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Delete a card template so it regenerates on next ingestion."""
    from storage.postgres.db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM card_templates WHERE signal_type = %s AND tenant_id = %s",
                (signal_type, auth.tenant_id),
            )
            deleted = cur.rowcount > 0
            conn.commit()
    if not deleted:
        return JSONResponse(status_code=404, content={"error": "TEMPLATE.NOT_FOUND"})
    return {"deleted": True, "signal_type": signal_type}


@router.post("/seed")
async def seed_outlines(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Seed card templates for all signal types from pre-defined outlines.

    Only inserts where no template exists yet. Safe to call multiple times.
    """
    from storage.signals.card_outlines import seed_card_outlines
    result = seed_card_outlines(tenant_id=auth.tenant_id)
    return result


@router.post("/{signal_type:path}/regenerate")
async def regenerate_with_ai(
    signal_type: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Regenerate a card template using AI layout suggestion.

    Uses the sample_payload from the existing template and asks the AI
    to suggest an optimal layout. Falls back to rule-based if AI unavailable.
    """
    import json

    existing = get_template(signal_type, auth.tenant_id)
    if not existing:
        return JSONResponse(status_code=404, content={"error": "TEMPLATE.NOT_FOUND"})

    sample = existing.get("sample_payload")
    if not sample:
        return JSONResponse(status_code=400, content={
            "error": "TEMPLATE.NO_SAMPLE",
            "detail": "No sample payload stored — cannot regenerate"
        })

    if isinstance(sample, str):
        sample = json.loads(sample)

    # Try AI
    ai_template = None
    try:
        ai_template = _ai_suggest_layout(signal_type, sample, auth.tenant_id)
    except Exception:
        pass

    if not ai_template:
        ai_template = _rule_based_template(signal_type, sample)

    # Update the template
    ok = update_template_columns(
        signal_type,
        ai_template["columns"],
        ai_template.get("media_config"),
        auth.tenant_id,
    )

    return {
        "regenerated": True,
        "signal_type": signal_type,
        "ai_generated": ai_template.get("ai_generated", False),
        "display_name": ai_template.get("display_name"),
        "media_count": len(ai_template.get("media_config", [])),
        "field_count": len(ai_template.get("columns", [])),
    }
