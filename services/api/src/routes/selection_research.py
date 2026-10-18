"""
Research routes — operator-initiated entity collapse.

  POST /research/from-selection — highlight text in the UI, route
                                  through gateway-collapse

The legacy term: research pipeline (5-stage Gemma classification + a
separate term: knowledge_node) was retired June 4 2026 in favor of the
substrate-aware path. Selection-research now goes straight through
gateway.admit_entity → form_concept_bindings → entity dossier. See
core/research/selection_router.py.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from pydantic import BaseModel

log = logging.getLogger("enki.api.research")

router = APIRouter(tags=["research"])


class ResearchFromSelectionRequest(BaseModel):
    """Operator highlighted text in the UI and clicked Research.
    Routes through the substrate (gateway-collapse) rather than the
    legacy term: pipeline."""
    text: str
    context: Optional[str] = None
    parent_entity_id: Optional[int] = None


@router.post("/research/from-selection")
async def research_from_selection(
    body: ResearchFromSelectionRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Substrate-aware selection research.

    The operator highlighted text in a dossier and clicked Research.
    This route dispatches via core.research.selection_router which:

      - Classifies the selection (entity-shaped vs multi vs topic)
      - For entity-shaped: passes through gateway.admit_entity so
        Layer 0 binding consumption routes to an existing concept OR
        mints a new one with proper substrate writes.
      - For multi-token selections: runs NER via local Gemma, routes
        each extracted entity through the gateway, returns candidates.
      - Creates a parent → resolved 'mentions' edge so the
        Connections section on both dossiers surfaces the link.
      - Queues an investigation when the entity is new (operator-
        initiated, bypasses cooldown).

    Returns the resolved entity_id + node_id + is_new + canonical_name
    so the UI can render a chip / chooser and link to the dossier.
    """
    text = (body.text or "").strip()
    if not text or len(text) < 2:
        return JSONResponse(status_code=400,
                            content={"error": "text is required"})
    if len(text) > 300:
        return JSONResponse(status_code=400, content={"error": "text too long"})

    from core.research.selection_router import research_selection

    result = research_selection(
        tenant_id=auth.tenant_id,
        text=text,
        context=body.context,
        parent_entity_id=body.parent_entity_id,
    )
    return result.to_dict()
