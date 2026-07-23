"""
Watch dashboard — derivative cards backed by node_metrics.

Phase 4.8 of the collapse rebuild plan. Reads ``node_metrics`` (computed
by ``core.workers.watch_derivatives``) and returns four sorted lists:

  /watch-dashboard/trending     top spike_factor
  /watch-dashboard/hotspots     top hotspot_intensity (place nodes only)
  /watch-dashboard/hype         top hype_score weighted by recency
  /watch-dashboard/narrative    rows with dominant_source_share > threshold
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

log = logging.getLogger("enki.watch_dashboard")
router = APIRouter(prefix="/watch-dashboard", tags=["watch"])


def _row_to_dict(row) -> dict:
    return {
        "node_id": row[0],
        "title": row[1],
        "node_type": row[2],
        "subtype": row[3],
        "base60_address": row[4],
        "mention_count": row[5],
        "mentions_per_hour": float(row[6]),
        "spike_factor": float(row[7]),
        "source_entropy": float(row[8]),
        "dominant_source": row[9],
        "dominant_source_share": float(row[10]),
        "hype_score": float(row[11]),
        "hotspot_intensity": float(row[12]) if row[12] is not None else None,
        "computed_at": row[13].isoformat() if row[13] else None,
    }


def _query(tenant_id: str, *, where: list[str], params: list,
           order_by: str, limit: int) -> list[dict]:
    from storage.postgres.db import get_connection

    sql = f"""
        SELECT n.node_id, n.title, n.node_type, n.subtype, n.base60_address,
               m.mention_count, m.mentions_per_hour, m.spike_factor,
               m.source_entropy, m.dominant_source, m.dominant_source_share,
               m.hype_score, m.hotspot_intensity, m.computed_at
          FROM node_metrics m
          JOIN knowledge_nodes n ON n.node_id = m.node_id
         WHERE m.tenant_id = %s
           AND {' AND '.join(where)}
         ORDER BY {order_by}
         LIMIT %s
    """
    full_params: list = [tenant_id, *params, limit]
    out: list[dict] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, full_params)
            for r in cur.fetchall():
                out.append(_row_to_dict(r))
    return out


@router.get("/trending")
async def trending(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    window: str = Query(default="24h"),
    limit: int = Query(default=10, ge=1, le=50),
):
    return _query(
        auth.tenant_id,
        where=["m.window_label = %s", "m.spike_factor > 1"],
        params=[window],
        order_by="m.spike_factor DESC",
        limit=limit,
    )


@router.get("/hotspots")
async def hotspots(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    window: str = Query(default="24h"),
    limit: int = Query(default=10, ge=1, le=50),
):
    return _query(
        auth.tenant_id,
        where=[
            "m.window_label = %s",
            "m.hotspot_intensity IS NOT NULL",
            "n.node_type = 'place'",
        ],
        params=[window],
        order_by="m.hotspot_intensity DESC",
        limit=limit,
    )


@router.get("/hype")
async def hype(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    window: str = Query(default="24h"),
    limit: int = Query(default=10, ge=1, le=50),
):
    return _query(
        auth.tenant_id,
        where=["m.window_label = %s", "m.hype_score > 0.05"],
        params=[window],
        order_by="m.hype_score DESC, m.computed_at DESC",
        limit=limit,
    )


@router.get("/narrative")
async def narrative_push(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    window: str = Query(default="24h"),
    threshold: float = Query(default=0.6, ge=0, le=1),
    limit: int = Query(default=10, ge=1, le=50),
):
    """Nodes whose mentions are dominated by a single source over the
    threshold — likely narrative-push patterns."""
    return _query(
        auth.tenant_id,
        where=[
            "m.window_label = %s",
            "m.dominant_source_share >= %s",
            "m.mention_count >= 3",
        ],
        params=[window, threshold],
        order_by="m.dominant_source_share DESC, m.mention_count DESC",
        limit=limit,
    )
