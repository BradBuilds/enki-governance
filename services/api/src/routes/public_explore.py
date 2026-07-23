"""
Public Explore — the click-in demo surface. A no-auth, curated read of a
library's extracted intelligence, formatted for the public /explore pages
(how-it-works counts + library-wide timeline + profile grid + per-entity
dossier). Same data the internal Command Center dossier uses, projected
through the curation layer:

  - only RELEVANT (non-flagged) source documents,
  - drop [needs_review] / self-referential assessments,
  - corpus-scoped (never the polluted global entity graph).

Auth: NONE. Read-only. Scoped to one library_id.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Query

from storage.postgres.db import get_connection

log = logging.getLogger("enki.routes.public_explore")

router = APIRouter(prefix="/public/explore", tags=["public-explore"])


def _relevant_docs(cur, library_id: int) -> list[int]:
    cur.execute(
        """SELECT lm.member_id
             FROM library_members lm
             JOIN documents d ON d.document_id = lm.member_id
            WHERE lm.library_id = %s AND lm.member_kind = 'document'
              AND COALESCE(d.relevance_status, 'relevant') <> 'flagged'
              AND COALESCE(lm.submission_status, 'accepted') <> 'flagged'""",
        (library_id,))
    return [r[0] for r in cur.fetchall()]


@router.get("/{library_id}/overview")
def overview(library_id: int):
    """How-it-works header: library meta + pipeline counts (incl. flagged,
    to show the gate working)."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT canonical_name, focal_term, description,
                      document_count, entity_count, fact_count
                 FROM libraries WHERE library_id = %s""", (library_id,))
        r = cur.fetchone()
        if not r:
            return {"error": "library not found"}
        docs = _relevant_docs(cur, library_id)
        cur.execute(
            """SELECT count(*) FROM library_members lm JOIN documents d ON d.document_id=lm.member_id
                WHERE lm.library_id=%s AND lm.member_kind='document'
                  AND (d.relevance_status='flagged' OR lm.submission_status='flagged')""",
            (library_id,))
        flagged = cur.fetchone()[0]
        cur.execute(
            """SELECT count(DISTINCT m.entity_id)
                 FROM document_mentions m WHERE m.document_id = ANY(%s) AND m.entity_id IS NOT NULL""",
            (docs,))
        entities = cur.fetchone()[0]
        cur.execute(
            """SELECT count(*) FROM statements s
                WHERE s.document_id = ANY(%s) AND s.spawned_event_id IS NOT NULL""", (docs,))
        events = cur.fetchone()[0]
    return {
        "library_id": library_id, "name": r[0], "focal_term": r[1], "description": r[2],
        "relevant_documents": len(docs), "flagged_documents": flagged,
        "entities": entities, "timeline_events": events,
        "pipeline": ["capture", "extract", "collapse", "govern", "timeline"],
    }


@router.get("/{library_id}/timeline")
def timeline(library_id: int, limit: int = Query(80, ge=1, le=300)):
    """Library-wide dated events, drawn only from relevant docs."""
    with get_connection() as conn, conn.cursor() as cur:
        docs = _relevant_docs(cur, library_id)
        cur.execute(
            """SELECT DISTINCT ev.event_id, ev.event_time::date, ev.event_type,
                      ev.title, ev.temporal_precision,
                      array_to_string(ev.place_text, ', ')
                 FROM events ev
                 JOIN statements s ON s.spawned_event_id = ev.event_id
                WHERE s.document_id = ANY(%s) AND ev.event_time IS NOT NULL
                ORDER BY ev.event_time::date""",
            (docs,))
        rows = cur.fetchall()[:limit]
    return {"library_id": library_id, "events": [
        {"event_id": r[0], "date": r[1].isoformat() if r[1] else None,
         "type": r[2], "title": r[3], "precision": r[4], "place": r[5] or None}
        for r in rows]}


@router.get("/{library_id}/profiles")
def profiles(library_id: int, limit: int = Query(12, ge=1, le=50)):
    """Top people by presence in relevant docs — the profile grid."""
    with get_connection() as conn, conn.cursor() as cur:
        docs = _relevant_docs(cur, library_id)
        # Rank by DOCUMENT-SPREAD, not raw mentions: central figures appear
        # across many docs, while mis-resolutions / in-doc contamination
        # concentrate in a single doc. Require >=2 docs (cross-document
        # corroboration — the collapse principle applied to ranking).
        cur.execute(
            """SELECT e.entity_id, e.canonical_name,
                      count(*) AS mentions,
                      count(DISTINCT m.document_id) AS docs
                 FROM document_mentions m
                 JOIN entities e ON e.entity_id = m.entity_id
                WHERE m.document_id = ANY(%s)
                  AND e.canonical_type = 'person'
                  AND e.canonical_name NOT ILIKE '%%[needs_review]%%'
                  AND length(e.canonical_name) >= 4
                  AND e.canonical_name ~ '\\s'          -- multi-token names only (cuts OCR junk)
                GROUP BY e.entity_id, e.canonical_name
                HAVING count(DISTINCT m.document_id) >= 2
                ORDER BY count(DISTINCT m.document_id) DESC, mentions DESC
                LIMIT %s""",
            (docs, limit))
        rows = cur.fetchall()
        out = []
        for eid, name, mentions, ndocs in rows:
            cur.execute(
                """SELECT count(*) FROM events
                    WHERE %s = ANY(actor_entity_ids) OR %s = ANY(target_entity_ids)""", (eid, eid))
            ev = cur.fetchone()[0]
            out.append({"entity_id": eid, "name": name, "mentions": mentions,
                        "documents": ndocs, "events": ev})
    return {"library_id": library_id, "profiles": out}


@router.get("/entity/{entity_id}")
def entity_dossier(entity_id: int, library_id: int = Query(...)):
    """Full public dossier for one person: presence + timeline + connections
    + places + source documents. Curated."""
    with get_connection() as conn, conn.cursor() as cur:
        docs = _relevant_docs(cur, library_id)
        cur.execute("SELECT canonical_name, canonical_type FROM entities WHERE entity_id=%s", (entity_id,))
        e = cur.fetchone()
        if not e:
            return {"error": "entity not found"}

        cur.execute(
            """SELECT count(*) FROM document_mentions
                WHERE entity_id=%s AND document_id = ANY(%s)""", (entity_id, docs))
        mentions = cur.fetchone()[0]

        # timeline — CORPUS-SCOPED: only events spawned from statements in
        # this library's relevant docs (never the polluted global event graph,
        # e.g. Vale's world-news events).
        cur.execute(
            """SELECT DISTINCT ev.event_time::date, ev.event_type, ev.title,
                      array_to_string(ev.place_text, ', ')
                 FROM events ev
                 JOIN statements s ON s.spawned_event_id = ev.event_id
                WHERE s.document_id = ANY(%s)
                  AND (%s = ANY(ev.actor_entity_ids) OR %s = ANY(ev.target_entity_ids))
                  AND ev.event_time IS NOT NULL
                ORDER BY ev.event_time::date LIMIT 60""", (docs, entity_id, entity_id))
        timeline = [{"date": r[0].isoformat() if r[0] else None, "type": r[1],
                     "title": r[2], "place": r[3] or None} for r in cur.fetchall()]

        # connections — CORPUS-SCOPED (assessments sourced from relevant docs)
        # and limited to real PEOPLE (drops place/value/[needs_review] junk like
        # 'Bill.com' / 'New England').
        cur.execute(
            """SELECT DISTINCT a.predicate_edge_type, o.canonical_name AS connected_to,
                      a.corroborated_by_count
                 FROM assessments a
                 JOIN entities o ON o.entity_id = a.obj_entity_id
                 JOIN statements s ON s.statement_id = ANY(a.source_statement_ids)
                WHERE a.subj_entity_id = %s
                  AND s.document_id = ANY(%s)
                  AND o.entity_id <> a.subj_entity_id
                  AND o.canonical_type = 'person'
                  AND o.canonical_name NOT ILIKE '%%[needs_review]%%'
                ORDER BY a.corroborated_by_count DESC LIMIT 25""", (entity_id, docs))
        connections = [{"relationship": r[0], "to": r[1], "corroborations": r[2]}
                       for r in cur.fetchall()]

        # places (events with coords)
        cur.execute(
            """SELECT DISTINCT array_to_string(place_text, ', '), latitude, longitude
                 FROM events
                WHERE (%s = ANY(actor_entity_ids) OR %s = ANY(target_entity_ids))
                  AND (latitude IS NOT NULL OR place_text <> '{}')
                LIMIT 25""", (entity_id, entity_id))
        places = [{"place": r[0] or None, "lat": r[1], "lon": r[2]} for r in cur.fetchall() if r[0]]

        # source documents (relevant, that mention this entity) — the "marks"
        cur.execute(
            """SELECT DISTINCT d.document_id, d.title, d.source_url, d.page_count
                 FROM document_mentions m JOIN documents d ON d.document_id = m.document_id
                WHERE m.entity_id = %s AND m.document_id = ANY(%s)
                LIMIT 25""", (entity_id, docs))
        sources = [{"document_id": r[0], "title": r[1], "source_url": r[2], "pages": r[3]}
                   for r in cur.fetchall()]

    return {
        "entity_id": entity_id, "name": e[0], "type": e[1],
        "presence": {"mentions": mentions, "documents": len(sources)},
        "timeline": timeline, "connections": connections,
        "places": places, "sources": sources,
    }
