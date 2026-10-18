"""Catalog + on-demand processing routes.

  GET  /catalog/libraries              list the documented, ready+hashed catalog
  POST /libraries/{id}/process         click -> acquire+ingest+collapse manifest
  GET  /libraries/{id}/process/status  progress of a running Process
  GET  /system/compute                 live GPU + compute stats (for the panel)
"""
from __future__ import annotations

import html
import json
import re

from fastapi import APIRouter, Query

from storage.postgres.db import get_connection
from core.catalog import process as cproc

router = APIRouter(tags=["catalog"])


@router.get("/intelligence")
def mined_intelligence():
    """What you've actually MINED — every library with collapsed content,
    regardless of how it got there (catalog Process button OR a script run).
    Counts come from the data itself (documents + resolved mentions +
    statements), not from a process marker, so nothing that was truly mined
    is invisible. Ordered by richness (entities) so the deepest topics lead."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH lib_docs AS (
                    SELECT lm.library_id, lm.member_id AS document_id
                      FROM library_members lm
                     WHERE lm.member_kind = 'document'
                )
                SELECT ld.library_id,
                       COALESCE(l.notes::jsonb->>'display_title', l.focal_term) AS title,
                       COUNT(DISTINCT ld.document_id)                           AS docs,
                       COUNT(DISTINCT dm.entity_id)                             AS entities,
                       COUNT(DISTINCT st.statement_id)                          AS facts
                  FROM lib_docs ld
                  JOIN libraries l ON l.library_id = ld.library_id
                  LEFT JOIN document_mentions dm
                         ON dm.document_id = ld.document_id AND dm.entity_id IS NOT NULL
                  LEFT JOIN statements st ON st.document_id = ld.document_id
                 GROUP BY ld.library_id, title
                HAVING COUNT(DISTINCT dm.entity_id) > 0
                 ORDER BY entities DESC
                 LIMIT 100
                """)
            rows = cur.fetchall()
    return {"libraries": [
        {"library_id": r[0], "title": r[1], "docs": r[2],
         "entities": r[3], "facts": r[4]}
        for r in rows
    ]}


@router.get("/catalog/libraries")
def list_catalog(agency: str | None = None, state: str | None = None):
    """The catalog: readable title + about + agency + state + hash + doc count."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT library_id, focal_term, description, notes
                     FROM libraries
                    WHERE notes::jsonb->>'catalog' = 'archive.org'
                    ORDER BY (coalesce(notes::jsonb->>'manifest_documents',
                                       notes::jsonb->>'item_count'))::int DESC NULLS LAST""")
            rows = cur.fetchall()
    out = []
    for lib_id, focal, desc, notes in rows:
        n = notes if isinstance(notes, dict) else json.loads(notes or "{}")
        if agency and n.get("agency") != agency:
            continue
        if state and n.get("state") != state:
            continue
        out.append({
            "library_id": lib_id,
            "title": n.get("display_title") or focal,
            "about": desc or n.get("about") or "",
            "agency": n.get("agency"),
            "state": n.get("state"),
            "documents": int(n.get("manifest_documents") or n.get("item_count") or 0),
            "manifest_sha256": n.get("manifest_sha256"),
            "archive_collection": n.get("archive_collection"),
            "process": n.get("process"),
            "followed": bool(n.get("followed")),
        })
    return {"count": len(out), "libraries": out}


@router.post("/libraries/{library_id}/follow")
def follow_library(library_id: int, on: bool = Query(True)):
    """Follow a topic -> the router pulls its news (from every source/category)
    into your private feed. Local only; your interests never leave the node."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT notes FROM libraries WHERE library_id=%s", (library_id,))
            row = cur.fetchone()
            if not row:
                return {"error": "not found"}
            n = row[0] if isinstance(row[0], dict) else json.loads(row[0] or "{}")
            n["followed"] = on
            cur.execute("UPDATE libraries SET notes=%s WHERE library_id=%s",
                        (json.dumps(n), library_id))
        conn.commit()
    return {"library_id": library_id, "followed": on}


def _clean_html(s: str) -> str:
    s = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()
    # Drop WordPress/ProPublica "The post … appeared first on …" boilerplate.
    s = re.sub(r"^The post .*?appeared first on .*$", "", s).strip()
    s = re.sub(r"\s*(Continue reading|Read more|The post)\b.*$", "", s).strip()
    return s


def _first_img(s: str) -> str | None:
    m = re.search(r'<img[^>]+src=["\']?([^"\'\s>]+)', s or "")
    return m.group(1) if m else None


def _sig_item(r, topic_id, topic):
    raw = r[6] or ""
    return {"signal_rid": r[0],
            "timestamp": r[1].isoformat() if r[1] else None,
            "headline": _clean_html(r[2]), "source": r[3], "link": r[4],
            "category": r[5], "summary": _clean_html(raw)[:280],
            "thumbnail": (r[7] if len(r) > 7 else None) or _first_img(raw),
            "topic_id": topic_id, "topic": topic}


@router.get("/feed/following")
def feed_following(limit: int = Query(80, ge=1, le=300)):
    """Merged live news across all FOLLOWED topics — each topic pulled from every
    source into one private stream. A topic matches news two ways: by feed
    category (broad topics like world/tech/cars) and by the router keyword match
    (specific topics via library_signals)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT library_id, coalesce(notes::jsonb->>'display_title', focal_term), "
                        "notes::jsonb->>'feed_category' FROM libraries "
                        "WHERE (notes::jsonb->>'followed') = 'true'")
            followed = cur.fetchall()
            if not followed:
                return {"count": 0, "items": []}
            cat_topic = {c: (i, n) for i, n, c in followed if c}
            lib_ids = [i for i, n, c in followed]

            items, seen = [], set()
            SIG = ("SELECT s.rid, s.timestamp, s.payload->>'title', s.payload->>'source', "
                   "s.payload->>'link', s.payload->>'category', s.payload->>'summary', "
                   "s.payload->>'thumbnail_url'")
            # Per-topic so every followed topic is represented (not just the
            # freshest-fetched one under a single global LIMIT).
            per = max(20, limit // max(len(cat_topic), 1)) if cat_topic else 0
            for cat, (tid, tname) in cat_topic.items():
                cur.execute(f"{SIG} FROM signals s WHERE s.payload->>'category' = %s "
                            "ORDER BY s.timestamp DESC NULLS LAST LIMIT %s", (cat, per))
                for r in cur.fetchall():
                    items.append(_sig_item(r, tid, tname))
                    seen.add(r[0])
            cur.execute(
                f"{SIG}, l.library_id, coalesce(l.notes::jsonb->>'display_title', l.focal_term) "
                "FROM library_signals ls JOIN signals s ON s.rid = ls.signal_rid "
                "JOIN libraries l ON l.library_id = ls.library_id "
                "WHERE ls.library_id = ANY(%s) ORDER BY s.timestamp DESC NULLS LAST LIMIT %s",
                (lib_ids, limit))
            for r in cur.fetchall():
                if r[0] in seen:
                    continue
                items.append(_sig_item(r, r[8], r[9]))
                seen.add(r[0])
    items.sort(key=lambda x: x["timestamp"] or "", reverse=True)
    return {"count": len(items), "items": items[:limit]}


@router.post("/libraries/{library_id}/process")
def process_library(library_id: int,
                    limit: int = Query(10, ge=1, le=1000,
                                       description="how many manifest docs to grab")):
    """Click -> Process: acquire + ingest + collapse (a slice of) the manifest."""
    return cproc.start_process(library_id, limit)


@router.get("/libraries/{library_id}/process/status")
def process_status(library_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT notes::jsonb->'process' FROM libraries WHERE library_id=%s",
                        (library_id,))
            row = cur.fetchone()
    return {"library_id": library_id, "process": (row[0] if row else None)}


@router.get("/system/compute")
def system_compute():
    return cproc.compute_stats()


@router.get("/system/pipeline-health")
def pipeline_health():
    """Live pipeline activity — what's actually mining, and whether it's moving.
    Replaces the 'workers' count (background daemons are off; the cron pipeline
    does the work). A topic is 'stalled' if its last snapshot is >90 min old."""
    topics = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT ON (p.library_id) p.library_id,
                           COALESCE(l.notes::jsonb->>'display_title', l.focal_term),
                           p.docs, p.entities, p.ts,
                           EXTRACT(EPOCH FROM (now() - p.ts))/60.0 AS mins
                      FROM corpus_progress p
                      LEFT JOIN libraries l ON l.library_id = p.library_id
                     ORDER BY p.library_id, p.ts DESC
                """)
                for lib, title, docs, ents, ts, mins in cur.fetchall():
                    topics.append({
                        "library_id": lib, "title": title, "docs": docs,
                        "entities": ents,
                        "minutes_since_tick": round(mins or 0, 1),
                        "stalled": (mins or 0) > 90,
                    })
    except Exception:
        pass
    active = [t for t in topics if not t["stalled"]]
    return {
        "topics": topics,
        "active_count": len(active),
        "stalled_count": len(topics) - len(active),
        "any_active": len(active) > 0,
    }
