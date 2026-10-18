"""
Content serving, view tracking, and collections.

Serves raw content from LDU-T, tracks who viewed what,
and manages user-created collections (boards/pins).
"""

import json
import uuid
from typing import Optional

from storage.postgres.db import get_connection


# ─── Content Serving ───────────────────────────────────────────────────────────

def get_signal_content(
    signal_id: str,
    tenant_id: str,
    dsn: Optional[str] = None,
) -> Optional[dict]:
    """Get raw content bytes for a signal (from LDU-T)."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT lt.raw_bytes, lt.content_type, lt.raw_sha256,
                       lt.raw_byte_length, lt.format_detected,
                       s.signal_type, s.signal_id
                FROM signals s
                JOIN ldu_t_signals lt ON lt.ldu_t_id = s.ldu_t_id
                WHERE s.signal_id = %s AND s.tenant_id = %s
            """, (signal_id, tenant_id))
            row = cur.fetchone()
            if not row:
                return None
            return {
                "raw_bytes": bytes(row[0]),
                "content_type": row[1] or "application/octet-stream",
                "sha256": row[2],
                "byte_length": row[3],
                "format_detected": row[4],
                "signal_type": row[5],
                "signal_id": row[6],
            }


# ─── Gallery Query ─────────────────────────────────────────────────────────────

def get_gallery(
    tenant_id: str,
    identity_id: Optional[str] = None,
    content_type: Optional[str] = None,
    unseen_only: bool = False,
    limit: int = 50,
    offset: int = 0,
    dsn: Optional[str] = None,
) -> dict:
    """
    Get content signals with view status for gallery display.

    Filters to content types (images, documents, articles, media).
    Includes seen/unseen status per user.
    """
    # Content domains: images, documents, articles, interest feeds
    content_types = [
        "signal.content.image", "signal.content.document",
        "signal.content.video", "signal.content.news_article",
        "signal.content.rss_entry", "signal.content.web_page",
        "signal.content.social_post",
        "signal.interest.image", "signal.interest.article",
        "signal.infrastructure.camera_capture",
        "signal.infrastructure.alpr_reading",
    ]

    if content_type:
        content_types = [content_type]

    placeholders = ",".join(["%s"] * len(content_types))
    params: list = [tenant_id] + content_types

    # Base query
    query = f"""
        SELECT s.rid, s.signal_id, s.signal_type, s.source_domain,
               s.timestamp, s.device_id, s.payload, s.metadata,
               s.latitude, s.longitude
        FROM signals s
        WHERE s.tenant_id = %s
          AND s.signal_type IN ({placeholders})
    """

    # Unseen filter
    if unseen_only and identity_id:
        query += """
          AND NOT EXISTS (
              SELECT 1 FROM signal_views sv
              WHERE sv.signal_rid = s.rid AND sv.identity_id = %s
          )
        """
        params.append(identity_id)

    query += " ORDER BY s.timestamp DESC LIMIT %s OFFSET %s"
    params.extend([min(limit, 200), offset])

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            items = []
            for r in cur.fetchall():
                payload = r[6] if isinstance(r[6], dict) else {}
                metadata = r[7] if isinstance(r[7], dict) else {}
                auto_tags = metadata.get("auto_tags", [])

                items.append({
                    "rid": r[0],
                    "signal_id": r[1],
                    "signal_type": r[2],
                    "source": r[3],
                    "timestamp": r[4].isoformat(),
                    "device_id": r[5],
                    "title": _extract_title(r[2], payload),
                    "format": payload.get("format", metadata.get("format_detected", "")),
                    "size_bytes": payload.get("size_bytes", payload.get("raw_byte_length", 0)),
                    "latitude": r[8],
                    "longitude": r[9],
                    "tags": auto_tags,
                    "has_thumbnail": r[2] in ("signal.content.image", "signal.infrastructure.alpr_reading",
                                               "signal.infrastructure.camera_capture", "signal.interest.image"),
                })

            # Get total count
            count_query = f"""
                SELECT COUNT(*) FROM signals
                WHERE tenant_id = %s AND signal_type IN ({placeholders})
            """
            cur.execute(count_query, [tenant_id] + content_types)
            total = cur.fetchone()[0]

    # Add view status if identity provided
    if identity_id and items:
        _add_view_status(items, identity_id, dsn)

    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _extract_title(signal_type: str, payload: dict) -> str:
    """Extract a display title from the payload."""
    if payload.get("title"):
        return payload["title"]
    if payload.get("subject"):
        return payload["subject"]
    if payload.get("filename"):
        return payload["filename"]
    if payload.get("file_sha256"):
        return payload["file_sha256"][:12] + "..."
    parts = signal_type.split(".")
    return parts[-1].replace("_", " ").title() if len(parts) >= 3 else signal_type


def _add_view_status(items: list[dict], identity_id: str, dsn: Optional[str] = None):
    """Add seen/unseen status to gallery items."""
    rids = [item["rid"] for item in items]
    if not rids:
        return

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(rids))
            cur.execute(f"""
                SELECT signal_rid, viewed_at, downloaded
                FROM signal_views
                WHERE identity_id = %s AND signal_rid IN ({placeholders})
            """, [identity_id] + rids)

            viewed = {}
            for r in cur.fetchall():
                viewed[r[0]] = {"viewed_at": r[1].isoformat() if r[1] else None, "downloaded": r[2]}

    for item in items:
        view = viewed.get(item["rid"])
        item["seen"] = view is not None
        item["viewed_at"] = view["viewed_at"] if view else None
        item["downloaded"] = view["downloaded"] if view else False


# ─── View Tracking ─────────────────────────────────────────────────────────────

def mark_signal_viewed(
    tenant_id: str,
    identity_id: str,
    signal_rid: int,
    context: str = "gallery",
    dsn: Optional[str] = None,
) -> bool:
    """Mark a signal as viewed by a user."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO signal_views (tenant_id, identity_id, signal_rid, view_context)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (identity_id, signal_rid)
                DO UPDATE SET view_context = EXCLUDED.view_context,
                              viewed_at = NOW()
            """, (tenant_id, identity_id, signal_rid, context))
            conn.commit()
            return True


def mark_signal_downloaded(
    tenant_id: str,
    identity_id: str,
    signal_rid: int,
    dsn: Optional[str] = None,
) -> bool:
    """Record that a user downloaded the raw content."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO signal_views (tenant_id, identity_id, signal_rid, view_context, downloaded, downloaded_at)
                VALUES (%s, %s, %s, 'download', TRUE, NOW())
                ON CONFLICT (identity_id, signal_rid)
                DO UPDATE SET downloaded = TRUE, downloaded_at = NOW()
            """, (tenant_id, identity_id, signal_rid))
            conn.commit()
            return True


def get_unseen_count(
    tenant_id: str,
    identity_id: str,
    dsn: Optional[str] = None,
) -> int:
    """Count signals this user hasn't viewed yet."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM signals s
                WHERE s.tenant_id = %s
                  AND NOT EXISTS (
                      SELECT 1 FROM signal_views sv
                      WHERE sv.signal_rid = s.rid AND sv.identity_id = %s
                  )
            """, (tenant_id, identity_id))
            return cur.fetchone()[0]


# ─── Collections (Boards/Pins) ─────────────────────────────────────────────────

def create_collection(
    tenant_id: str, identity_id: str, name: str,
    description: Optional[str] = None, is_public: bool = False,
    dsn: Optional[str] = None,
) -> dict:
    collection_id = f"coll_{uuid.uuid4().hex[:12]}"
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO collections (collection_id, tenant_id, identity_id, name, description, is_public)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (collection_id, tenant_id, identity_id, name, description, is_public))
            conn.commit()
    return {"collection_id": collection_id, "name": name}


def list_collections(
    tenant_id: str, identity_id: str, dsn: Optional[str] = None,
) -> list[dict]:
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.collection_id, c.name, c.description, c.is_public, c.created_at,
                       (SELECT COUNT(*) FROM collection_items ci WHERE ci.collection_id = c.collection_id)
                FROM collections c
                WHERE c.tenant_id = %s AND (c.identity_id = %s OR c.is_public = TRUE)
                ORDER BY c.updated_at DESC
            """, (tenant_id, identity_id))
            return [
                {"collection_id": r[0], "name": r[1], "description": r[2],
                 "is_public": r[3], "created_at": r[4].isoformat(), "item_count": r[5]}
                for r in cur.fetchall()
            ]


def pin_to_collection(
    collection_id: str, signal_rid: int, pinned_by: str,
    notes: Optional[str] = None, dsn: Optional[str] = None,
) -> bool:
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO collection_items (collection_id, signal_rid, pinned_by, notes)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (collection_id, signal_rid, pinned_by, notes))
            conn.commit()
            return cur.rowcount > 0


def get_collection_items(
    collection_id: str, tenant_id: str, limit: int = 100, dsn: Optional[str] = None,
) -> list[dict]:
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.signal_id, s.signal_type, s.timestamp, s.payload, s.metadata,
                       ci.pinned_by, ci.pinned_at, ci.notes
                FROM collection_items ci
                JOIN signals s ON s.rid = ci.signal_rid
                WHERE ci.collection_id = %s AND s.tenant_id = %s
                ORDER BY ci.pinned_at DESC LIMIT %s
            """, (collection_id, tenant_id, min(limit, 1000)))
            return [
                {
                    "signal_id": r[0], "signal_type": r[1],
                    "timestamp": r[2].isoformat(),
                    "title": _extract_title(r[1], r[3] if isinstance(r[3], dict) else {}),
                    "pinned_by": r[5], "pinned_at": r[6].isoformat(), "notes": r[7],
                }
                for r in cur.fetchall()
            ]
