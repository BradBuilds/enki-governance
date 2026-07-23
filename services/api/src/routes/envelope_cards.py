"""
Envelope cards — read signal_extractions rows produced by the new
extraction pipeline (Phases 2-5, 8) and return them in a card-shaped
JSON the new EnvelopeCard.svelte component renders.

Generic over node_type/subtype. The card renderer is content-agnostic:
it picks fields from the envelope by canonical path and lets the
frontend decide how to display them based on node_type. There are no
per-signal-type branches in this file.

GET /envelope-cards            — list cards (paginated, filterable)
GET /envelope-cards/{node_id}  — one full card with envelope JSON
GET /envelope-cards/stats      — counts by node_type/subtype/tier
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

log = logging.getLogger("enki.envelope_cards")
router = APIRouter(prefix="/envelope-cards", tags=["envelope-cards"])


def _project_card(row: dict) -> dict:
    """Project a signal_extractions row into a frontend-safe card dict.

    THE CARD IS THE INVARIANT. Every field is guaranteed present with a
    sane default. The frontend never crashes on a missing field; if the
    envelope schema changes upstream, the LDU layer handles the
    transformation and this projection still produces a renderable card.

    A row only reaches this function if it already passed the auto_node
    upsert (which means it has a node_id, an envelope JSONB, and the
    LDU lineage stamp). If any individual field is missing or oddly
    typed we fall back to safe defaults — never raise.
    """
    env = row.get("envelope") or {}
    if isinstance(env, str):
        try:
            env = json.loads(env)
        except Exception:
            env = {}
    if not isinstance(env, dict):
        env = {}

    def _safe_dict(obj, key):
        v = obj.get(key)
        return v if isinstance(v, dict) else {}

    def _safe_list(obj, key):
        v = obj.get(key)
        return v if isinstance(v, list) else []

    def _safe_str(value, default=""):
        if value is None:
            return default
        try:
            return str(value)
        except Exception:
            return default

    identity     = _safe_dict(env, "identity")
    summary      = _safe_dict(env, "summary")
    spatial      = _safe_dict(env, "spatial")
    media        = _safe_dict(env, "media")
    source       = _safe_dict(env, "source")
    completeness = _safe_dict(env, "completeness")
    temporal     = _safe_dict(env, "temporal")

    # Title — fall back through display → raw → row title → "Untitled"
    title = (
        _safe_str(identity.get("title_display"))
        or _safe_str(identity.get("title_raw"))
        or _safe_str(row.get("title"))
        or "Untitled"
    )

    # Outlet — display name with multiple fallbacks
    outlet = (
        _safe_str(source.get("outlet_name"))
        or _safe_str(row.get("source_authority"))
        or "Unknown source"
    )

    # Coordinates — must be a dict with both lat and lon, or None
    coords_raw = spatial.get("coordinates")
    coordinates = None
    if isinstance(coords_raw, dict):
        lat = coords_raw.get("lat")
        lon = coords_raw.get("lon")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            coordinates = {"lat": lat, "lon": lon}

    # Topics — guarantee a list of strings (never None values)
    topics_list: list[str] = []
    for t in _safe_list(env, "topics"):
        if isinstance(t, dict):
            label = t.get("label")
            if label:
                topics_list.append(_safe_str(label))

    # Place text — always a list of strings
    place_text = [
        _safe_str(p) for p in _safe_list(spatial, "place_text") if p
    ]

    # Score — must be a number 0..1
    raw_score = completeness.get("score")
    if isinstance(raw_score, (int, float)):
        score_val = float(raw_score)
    else:
        score_val = 0.0

    return {
        "node_id":      _safe_str(row.get("node_id"), default="unknown"),
        "node_type":    _safe_str(row.get("node_type"), default="source"),
        "subtype":      _safe_str(row.get("subtype")) or None,
        "title":        title,
        "headline":     _safe_str(summary.get("headline"), default=title),
        "narrative":    _safe_str(summary.get("narrative")),
        "byline":       _safe_str(identity.get("byline_raw")) or None,
        "outlet":       outlet,
        "url":          _safe_str(source.get("url")) or None,
        "published":    _safe_str(temporal.get("event_time")) or None,
        "thumbnail":    _safe_str(media.get("thumbnail")) or None,
        "domain":       _safe_str(summary.get("domain_classification"), default="other"),
        "coordinates":  coordinates,
        "place_text":   place_text,
        "topics":       topics_list,
        "entity_count": len(_safe_list(env, "entities")),
        "completeness": {
            "score":        score_val,
            "tier":         _safe_str(completeness.get("tier"), default="stub"),
            "filled_slots": int(completeness.get("filled_slots") or 0),
            "total_slots":  int(completeness.get("total_slots") or 0),
            "gaps":         [_safe_str(g) for g in _safe_list(completeness, "gaps")],
        },
        "master_entity_id": row.get("master_entity_id"),
        "master_topic_id":  row.get("master_topic_id"),
        "active_set":       _safe_str(row.get("admitted_under_active_set")) or None,
        "updated_at":       _safe_str(row.get("updated_at")) or None,
    }


@router.get("")
async def list_envelope_cards(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    node_type: Optional[str] = Query(None),
    subtype: Optional[str] = Query(None),
    tier: Optional[str] = Query(None, description="stub|partial|solid|verified"),
    min_score: Optional[float] = Query(None, ge=0.0, le=1.0),
    topic_id: Optional[int] = Query(None),
    place_id: Optional[int] = Query(None),
    exclude_watched: bool = Query(True),
):
    """List envelope-shaped cards from signal_extractions."""
    from storage.postgres.db import get_connection

    clauses = ["tenant_id = %s"]
    params: list = [auth.tenant_id]

    if node_type:
        clauses.append("node_type = %s")
        params.append(node_type)
    if subtype:
        clauses.append("subtype = %s")
        params.append(subtype)
    if tier:
        clauses.append("completeness_tier = %s")
        params.append(tier)
    if min_score is not None:
        clauses.append("completeness_score >= %s")
        params.append(min_score)
    if topic_id is not None:
        clauses.append("master_topic_id = %s")
        params.append(topic_id)
    if place_id is not None:
        # Post-unification (migration 118): places are entities. Filter
        # by master_entity_id where the entity is place-typed.
        clauses.append("master_entity_id = %s")
        params.append(place_id)

    # Only return feed-worthy extractions that haven't been superseded by
    # a higher-level summary (e.g. a track that bundled its constituent
    # position reports). signal_extractions.envelope is NOT NULL by schema
    # default so no explicit check needed here.
    clauses.append("feed_worthy = TRUE")
    clauses.append("superseded_by IS NULL")

    # Articles that came in through a watch-topic connector belong under
    # /watch/{id}, not the discovery feed. Filter them out by EXISTS on
    # signals.metadata._topic_id unless caller asks for everything.
    if exclude_watched:
        clauses.append(
            "NOT EXISTS ("
            "  SELECT 1 FROM signals s "
            "   WHERE s.signal_rid = signal_extractions.signal_rid "
            "     AND s.metadata->>'_topic_id' IS NOT NULL "
            "     AND s.metadata->>'_topic_id' != ''"
            ")"
        )

    where = " AND ".join(clauses)
    cards: list[dict] = []

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT node_id, node_type, subtype,
                           envelope->'identity'->>'title_display' AS title,
                           envelope->'source'->>'outlet_name'     AS source_authority,
                           envelope, master_entity_id, master_topic_id,
                           admitted_under_active_set, updated_at
                      FROM signal_extractions
                     WHERE {where}
                     ORDER BY updated_at DESC
                     LIMIT %s OFFSET %s
                    """,
                    params + [limit, offset],
                )
                cols = [d[0] for d in cur.description]
                for row in cur.fetchall():
                    cards.append(_project_card(dict(zip(cols, row))))
    except Exception as e:
        log.exception("envelope_cards list failed")
        raise HTTPException(status_code=500, detail=str(e))

    return {"cards": cards, "count": len(cards), "offset": offset, "limit": limit}


@router.get("/stats")
async def envelope_card_stats(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Counts by node_type/subtype and by tier."""
    from storage.postgres.db import get_connection

    out = {"by_type": [], "by_tier": [], "total": 0}
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT node_type, subtype, COUNT(*),
                           ROUND(AVG(completeness_score)::numeric, 3)
                      FROM signal_extractions
                     WHERE tenant_id = %s AND feed_worthy = TRUE
                       AND superseded_by IS NULL
                     GROUP BY node_type, subtype
                     ORDER BY 3 DESC
                    """,
                    (auth.tenant_id,),
                )
                for r in cur.fetchall():
                    out["by_type"].append({
                        "node_type": r[0], "subtype": r[1],
                        "count": r[2], "avg_score": float(r[3] or 0.0),
                    })

                cur.execute(
                    """
                    SELECT completeness_tier, COUNT(*)
                      FROM signal_extractions
                     WHERE tenant_id = %s AND feed_worthy = TRUE
                       AND superseded_by IS NULL
                     GROUP BY completeness_tier
                     ORDER BY 2 DESC
                    """,
                    (auth.tenant_id,),
                )
                for r in cur.fetchall():
                    out["by_tier"].append({"tier": r[0], "count": r[1]})

                cur.execute(
                    "SELECT COUNT(*) FROM signal_extractions WHERE tenant_id=%s AND feed_worthy=TRUE AND superseded_by IS NULL",
                    (auth.tenant_id,),
                )
                out["total"] = cur.fetchone()[0]
    except Exception as e:
        log.exception("envelope_card_stats failed")
        raise HTTPException(status_code=500, detail=str(e))
    return out


@router.get("/{node_id}")
async def get_envelope_card(
    node_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Return one card with the full envelope attached. Used for the
    detail / dossier view."""
    from storage.postgres.db import get_connection
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT node_id, node_type, subtype,
                           envelope->'identity'->>'title_display' AS title,
                           envelope->'source'->>'outlet_name'     AS source_authority,
                           envelope, master_entity_id, master_topic_id,
                           admitted_under_active_set, updated_at
                      FROM signal_extractions
                     WHERE tenant_id = %s AND node_id = %s
                    """,
                    (auth.tenant_id, node_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="not found")
                cols = [d[0] for d in cur.description]
                row_dict = dict(zip(cols, row))
                card = _project_card(row_dict)
                env = row_dict.get("envelope") or {}
                if isinstance(env, str):
                    try:
                        env = json.loads(env)
                    except Exception:
                        env = {}
                card["envelope"] = env
                return card
    except HTTPException:
        raise
    except Exception as e:
        log.exception("envelope_card detail failed")
        raise HTTPException(status_code=500, detail=str(e))
