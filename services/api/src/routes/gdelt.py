"""
GDELT API surface — global news mentions on the map.

Backed by the gdelt_mentions table populated by core.connectors.gdelt_gkg
(see migration 100). The worker pulls a fresh 15-min GKG slice on a
schedule; this endpoint just reads the table.

GET /gdelt/mentions  — GeoJSON FeatureCollection (bbox + time + theme filters)
GET /gdelt/themes    — top themes in the current window
GET /gdelt/state     — sync state (last processed file, error count, etc.)
POST /gdelt/sync     — kick a sync immediately (admin / manual)
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

from storage.postgres.db import get_connection

log = logging.getLogger("enki.gdelt")
router = APIRouter(prefix="/gdelt", tags=["map", "gdelt"])


@router.get("/mentions")
async def list_mentions(
    west: Optional[float] = Query(None),
    south: Optional[float] = Query(None),
    east: Optional[float] = Query(None),
    north: Optional[float] = Query(None),
    hours: int = Query(24, ge=1, le=48),
    theme: Optional[str] = Query(None, description="Substring match against any of the row's themes"),
    country: Optional[str] = Query(None, max_length=8),
    limit: int = Query(1000, ge=1, le=5000),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Return geo-coded GDELT mentions as a GeoJSON FeatureCollection.

    Filters compose with AND. bbox/west/south/east/north are optional —
    omit them for a global view. Time window is bound by the retention
    sweep in the worker (default 48h).
    """
    clauses = ["published_at >= NOW() - %s::interval"]
    params: list = [f"{hours} hours"]

    if west is not None and south is not None and east is not None and north is not None:
        clauses.append("longitude BETWEEN %s AND %s")
        params.extend([west, east])
        clauses.append("latitude BETWEEN %s AND %s")
        params.extend([south, north])

    if theme:
        clauses.append("EXISTS (SELECT 1 FROM UNNEST(themes) t WHERE t ILIKE %s)")
        params.append(f"%{theme}%")

    if country:
        clauses.append("country_code = %s")
        params.append(country.upper())

    params.append(limit)
    sql = f"""
        SELECT mention_id, gkg_record_id, published_at, latitude, longitude,
               location_name, country_code, themes, tone, source_domain,
               document_url
          FROM gdelt_mentions
         WHERE {' AND '.join(clauses)}
         ORDER BY published_at DESC
         LIMIT %s
    """

    features = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for r in cur.fetchall():
                features.append({
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [float(r[4]), float(r[3])]},
                    "properties": {
                        "mention_id": r[0],
                        "gkg_record_id": r[1],
                        "published_at": r[2].isoformat() if r[2] else None,
                        "name": r[5],
                        "country_code": r[6],
                        "themes": r[7] or [],
                        "tone": float(r[8]) if r[8] is not None else None,
                        "source": r[9],
                        "url": r[10],
                    },
                })

    return {"type": "FeatureCollection", "features": features, "count": len(features)}


@router.get("/themes")
async def top_themes(
    hours: int = Query(24, ge=1, le=48),
    limit: int = Query(40, ge=1, le=200),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Top themes in the last N hours, by mention count. Useful as
    filter chips on the map UI."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT theme, COUNT(*) AS n
                  FROM gdelt_mentions, UNNEST(themes) t (theme)
                 WHERE published_at >= NOW() - %s::interval
                 GROUP BY theme
                 ORDER BY n DESC
                 LIMIT %s
                """,
                (f"{hours} hours", limit),
            )
            rows = cur.fetchall()
    return {"themes": [{"theme": r[0], "count": int(r[1])} for r in rows]}


@router.get("/state")
async def sync_state(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Last processed file, time, count, error. For ops dashboards."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT last_processed_url, last_processed_at,
                       last_record_count, last_error, consecutive_errors
                  FROM gdelt_sync_state WHERE id = 1
            """)
            r = cur.fetchone()
            cur.execute("SELECT COUNT(*) FROM gdelt_mentions")
            total = cur.fetchone()[0]
    if not r:
        return {"initialized": False, "total_mentions": total}
    return {
        "initialized": True,
        "last_processed_url": r[0],
        "last_processed_at": r[1].isoformat() if r[1] else None,
        "last_record_count": r[2],
        "last_error": r[3],
        "consecutive_errors": r[4],
        "total_mentions": total,
    }


@router.post("/sync")
async def manual_sync(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Trigger a GKG sync now. Useful after install or to recover from a
    long backend pause. Returns the same dict shape as the worker."""
    from core.connectors.gdelt_gkg import sync_latest_gkg
    return await sync_latest_gkg()
