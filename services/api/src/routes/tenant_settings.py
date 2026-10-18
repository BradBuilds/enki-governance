"""
Tenant Settings — runtime configuration knobs surfaced to the operator.

Keys are flat in the `settings` JSONB so the API + UI stay simple.

Current keys:
  news_llm_max_per_min  (int)  Soft cap on news LLM extraction throughput.
                              feed_enrichment workers self-throttle to stay
                              under this. Default 30. 0 = pause news LLM.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

router = APIRouter(tags=["tenant-settings"])


# Defaults — match the migration seed. Callers can rely on these even
# if the row doesn't exist yet (e.g., new tenants).
DEFAULTS: dict[str, Any] = {
    "news_llm_max_per_min": 30,
}

# Bounds for operator-set values. Out-of-range writes get clamped.
BOUNDS: dict[str, tuple[int, int]] = {
    "news_llm_max_per_min": (0, 600),  # 0 = paused, 600 = ~10/sec
}


def _get_conn():
    from storage.postgres.db import get_connection
    return get_connection()


def read_tenant_settings(tenant_id: str) -> dict[str, Any]:
    """Read tenant settings with DEFAULTS applied for missing keys.

    Used by workers (feed_enrichment) and the API. Workers should cache
    this for a few seconds — it's cheap but not free.
    """
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT settings FROM tenant_settings WHERE tenant_id = %s",
                            (tenant_id,))
                row = cur.fetchone()
    except Exception:
        return dict(DEFAULTS)

    stored = (row[0] if row else None) or {}
    if isinstance(stored, str):
        try:
            stored = json.loads(stored)
        except Exception:
            stored = {}
    out = dict(DEFAULTS)
    out.update(stored)
    return out


class UpdateSettingsRequest(BaseModel):
    settings: dict[str, Any]


@router.get("/tenant/settings")
async def get_settings(auth: UnifiedAuthInfo = Depends(unified_auth)):
    return {
        "tenant_id": auth.tenant_id,
        "settings": read_tenant_settings(auth.tenant_id),
        "defaults": dict(DEFAULTS),
        "bounds": {k: list(v) for k, v in BOUNDS.items()},
    }


@router.put("/tenant/settings")
async def put_settings(body: UpdateSettingsRequest,
                       auth: UnifiedAuthInfo = Depends(unified_auth)):
    # Clamp + type-check known keys; unknown keys are accepted as-is so
    # new feature flags don't require code change.
    cleaned: dict[str, Any] = {}
    for k, v in (body.settings or {}).items():
        if k in BOUNDS:
            lo, hi = BOUNDS[k]
            try:
                iv = int(v)
            except (TypeError, ValueError):
                return JSONResponse(status_code=400, content={
                    "error": f"{k} must be an integer",
                })
            cleaned[k] = max(lo, min(hi, iv))
        else:
            cleaned[k] = v

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO tenant_settings (tenant_id, settings, updated_at)
                VALUES (%s, %s::jsonb, NOW())
                ON CONFLICT (tenant_id) DO UPDATE SET
                    settings   = tenant_settings.settings || EXCLUDED.settings,
                    updated_at = NOW()
                RETURNING settings, updated_at
            """, (auth.tenant_id, json.dumps(cleaned)))
            res = cur.fetchone()
            conn.commit()

    final = dict(DEFAULTS)
    final.update(res[0] if res else {})
    return {
        "tenant_id": auth.tenant_id,
        "settings": final,
        "updated_at": res[1].isoformat() if res and res[1] else None,
    }
