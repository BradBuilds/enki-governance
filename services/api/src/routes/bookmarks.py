"""
Article bookmarks — lightweight "Remember" flag per user per node.

No research triggered, no validation — just a personal pile the user
builds by clicking the star on any card. List + recall happens via
GET /bookmarks (filterable by category or by linked entity).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from pydantic import BaseModel

router = APIRouter(tags=["bookmarks"])


def _get_conn():
    from storage.postgres.db import get_connection
    return get_connection()


class BookmarkBody(BaseModel):
    node_id: str
    category: Optional[str] = "save"   # save / idea / interesting / follow-up
    note: Optional[str] = None


@router.post("/bookmarks")
async def create_bookmark(
    body: BookmarkBody,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Upsert a bookmark. Same (tenant, identity, node) re-stars just
    updates the category/note — no duplicates."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO article_bookmarks
                    (tenant_id, identity_id, node_id, category, note)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, identity_id, node_id) DO UPDATE SET
                    category = EXCLUDED.category,
                    note     = COALESCE(EXCLUDED.note, article_bookmarks.note)
                RETURNING bookmark_id, created_at
                """,
                (auth.tenant_id, auth.subject_id, body.node_id,
                 body.category or "save", body.note),
            )
            bookmark_id, created_at = cur.fetchone()
        conn.commit()
    return {
        "bookmark_id": bookmark_id,
        "node_id": body.node_id,
        "category": body.category or "save",
        "created_at": created_at.isoformat() if created_at else None,
    }


@router.delete("/bookmarks/{node_id}")
async def delete_bookmark(
    node_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Un-remember an article."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM article_bookmarks
                 WHERE tenant_id = %s AND identity_id = %s AND node_id = %s
                """,
                (auth.tenant_id, auth.subject_id, node_id),
            )
            deleted = cur.rowcount
        conn.commit()
    return {"deleted": deleted, "node_id": node_id}


@router.get("/bookmarks")
async def list_bookmarks(
    category: Optional[str] = None,
    entity_id: Optional[int] = None,
    limit: int = 200,
    offset: int = 0,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """List the user's bookmarks, newest first, with the enclosing
    knowledge_nodes row joined so the UI can render cards without a
    second round trip."""
    where = ["b.tenant_id = %s", "b.identity_id = %s"]
    params: list = [auth.tenant_id, auth.subject_id]
    if category:
        where.append("b.category = %s")
        params.append(category)
    if entity_id:
        where.append("kn.master_entity_id = %s")
        params.append(entity_id)

    sql = f"""
        SELECT b.bookmark_id, b.node_id, b.category, b.note, b.created_at,
               kn.title, kn.description, kn.node_type, kn.subtype,
               kn.source_authority, kn.concepts,
               kn.envelope -> 'source' ->> 'url'     AS url,
               kn.envelope -> 'identity' ->> 'thumbnail' AS thumbnail,
               kn.envelope -> 'temporal' ->> 'event_time' AS event_time,
               kn.master_entity_id, kn.master_topic_id
          FROM article_bookmarks b
          LEFT JOIN knowledge_nodes kn ON kn.node_id = b.node_id
         WHERE {' AND '.join(where)}
         ORDER BY b.created_at DESC
         LIMIT %s OFFSET %s
    """
    params.extend([limit, offset])

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

            cur.execute(
                """
                SELECT category, COUNT(*)
                  FROM article_bookmarks
                 WHERE tenant_id = %s AND identity_id = %s
                 GROUP BY category
                """,
                (auth.tenant_id, auth.subject_id),
            )
            counts = {r[0]: r[1] for r in cur.fetchall()}

    bookmarks = [{
        "bookmark_id": r[0],
        "node_id": r[1],
        "category": r[2],
        "note": r[3],
        "created_at": r[4].isoformat() if r[4] else None,
        "title": r[5],
        "description": r[6],
        "node_type": r[7],
        "subtype": r[8],
        "outlet": r[9],
        "concepts": r[10] or [],
        "url": r[11],
        "thumbnail": r[12],
        "event_time": r[13],
        "master_entity_id": r[14],
        "master_topic_id": r[15],
    } for r in rows]

    return {
        "bookmarks": bookmarks,
        "counts_by_category": counts,
        "total": sum(counts.values()),
    }


@router.get("/bookmarks/ids")
async def bookmark_ids(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Return just the set of node_ids the user has remembered. The feed
    page calls this once on load to decide which stars should render
    filled vs. empty — cheaper than a per-card check."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT node_id, category
                  FROM article_bookmarks
                 WHERE tenant_id = %s AND identity_id = %s
                """,
                (auth.tenant_id, auth.subject_id),
            )
            rows = cur.fetchall()
    return {"ids": {r[0]: r[1] for r in rows}}
