"""
Access log endpoint — receives batched access events from the frontend.
Every data retrieval (chat query, story view, entity click) is logged
as a signal.access.* event in the audit_events table.
"""

from fastapi import APIRouter, Request
from pydantic import BaseModel
from typing import Optional
import json
import os

router = APIRouter()

DSN = os.getenv("ENKI_PG_DSN", "")


class AccessEvent(BaseModel):
    action: str
    resource_type: str
    resource_id: Optional[str] = None
    context: str
    space: str
    timestamp: str
    metadata: Optional[dict] = None


class AccessLogBatch(BaseModel):
    events: list[AccessEvent]


@router.post("/access-log")
async def log_access(batch: AccessLogBatch, request: Request):
    """Persist access events to audit_events table."""
    if not DSN or not batch.events:
        return {"status": "ok", "stored": 0}

    identity_id = request.headers.get("x-identity-id", "anonymous")
    tenant_id = request.headers.get("x-tenant-id", "default")

    try:
        import asyncpg

        conn = await asyncpg.connect(DSN)
        try:
            for event in batch.events:
                payload = {
                    "action": event.action,
                    "resource_type": event.resource_type,
                    "resource_id": event.resource_id,
                    "context": event.context,
                    "space": event.space,
                    "identity_id": identity_id,
                    "metadata": event.metadata,
                }
                await conn.execute(
                    """
                    INSERT INTO audit_events (tenant_id, event_type, payload_json, cid)
                    VALUES ($1, $2, $3, $4)
                    """,
                    tenant_id,
                    f"signal.access.{event.action}",
                    json.dumps(payload),
                    f"access:{event.timestamp}:{identity_id}:{event.action}",
                )
        finally:
            await conn.close()

        return {"status": "ok", "stored": len(batch.events)}
    except Exception:
        # Don't break the frontend if DB is unavailable
        return {"status": "ok", "stored": 0}
