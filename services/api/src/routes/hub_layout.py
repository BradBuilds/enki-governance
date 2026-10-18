"""
Hub Layout — operator-configurable bucket layout for the Hub dashboard.

Each bucket maps one or more source_catalog categories to a named section
shown on /hub. Operators can add, remove, and reorder buckets and set
how many items show per section.

GET  /hub/layout  — current layout for the calling operator
PUT  /hub/layout  — replace the layout (full overwrite, not patch)
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

router = APIRouter(tags=["hub-layout"])


class Bucket(BaseModel):
    name: str
    categories: list[str]
    top_n: int = 2
    order: int = 0


class HubLayoutRequest(BaseModel):
    buckets: list[Bucket]


def _get_conn():
    from storage.postgres.db import get_connection
    return get_connection()


def _user_id(auth: UnifiedAuthInfo) -> Optional[str]:
    """Identifier used to scope per-user layout rows.

    Falls back to identity_id, then to the operator's bearer subject. If
    nothing is available we return None and write to the tenant-default row.
    """
    return getattr(auth, "identity_id", None) or getattr(auth, "subject", None)


@router.get("/hub/layout")
async def get_hub_layout(auth: UnifiedAuthInfo = Depends(unified_auth)):
    uid = _user_id(auth)
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT buckets, updated_at, user_id
                  FROM hub_layouts
                 WHERE tenant_id = %s
                   AND (user_id = %s OR (user_id IS NULL AND %s IS NULL))
                 ORDER BY user_id NULLS LAST
                 LIMIT 1
            """, (auth.tenant_id, uid, uid))
            row = cur.fetchone()
            if row is None:
                cur.execute("""
                    SELECT buckets, updated_at, user_id FROM hub_layouts
                     WHERE tenant_id = %s AND user_id IS NULL LIMIT 1
                """, (auth.tenant_id,))
                row = cur.fetchone()

    if row is None:
        return {
            "buckets": [],
            "is_default": True,
            "updated_at": None,
        }

    buckets = row[0] if isinstance(row[0], list) else json.loads(row[0] or "[]")
    return {
        "buckets": sorted(buckets, key=lambda b: b.get("order", 0)),
        "is_default": row[2] is None,
        "updated_at": row[1].isoformat() if row[1] else None,
    }


@router.put("/hub/layout")
async def put_hub_layout(body: HubLayoutRequest,
                         auth: UnifiedAuthInfo = Depends(unified_auth)):
    uid = _user_id(auth)
    buckets_json = json.dumps([b.dict() for b in body.buckets])

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO hub_layouts (tenant_id, user_id, buckets, updated_at)
                VALUES (%s, %s, %s::jsonb, NOW())
                ON CONFLICT (tenant_id, COALESCE(user_id, ''))
                DO UPDATE SET buckets = EXCLUDED.buckets,
                              updated_at = NOW()
                RETURNING layout_id, updated_at
            """, (auth.tenant_id, uid, buckets_json))
            res = cur.fetchone()
            conn.commit()

    return {
        "layout_id": res[0],
        "updated_at": res[1].isoformat() if res[1] else None,
        "buckets": [b.dict() for b in body.buckets],
    }
