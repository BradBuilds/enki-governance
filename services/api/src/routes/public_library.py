"""
Public Library — anonymous-readable surface for federation-published corpora.

When a library is marked is_public=TRUE + is_published_to_network=TRUE,
its catalog metadata + references list + ingested-doc index become
fetchable by anyone, no auth, no peer JWT. This is the wire used by:

  - example.com/library page (hub's catalog browser)
  - Any future independent catalog mirror that wants to render a
    file-browser-style view of a federation_id's contents

Federation manifest serving stays auth-gated under /p2p — that path
is for full bulk pulls that include the signed bundle. This public
surface is for *human-readable browsing* and returns lightweight JSON.

Auth: NONE. Filters to is_public=TRUE only. Any non-public library
returns 404 here regardless of caller identity.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from storage.postgres.db import get_connection

log = logging.getLogger("enki.routes.public_library")

router = APIRouter(prefix="/public/library", tags=["public-library"])


def _library_from_fid(federation_id: str) -> dict:
    """Resolve a federation_id to (library_id, focal_term, subtype, ...).
    404 if not present OR not public."""
    try:
        from uuid import UUID
        UUID(federation_id)
    except ValueError:
        raise HTTPException(400, "invalid federation_id (must be UUID)")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT library_id, library_subtype, canonical_name,
                       focal_term, description, base60_address,
                       reference_count, document_count, entity_count,
                       fact_count, first_published_at,
                       discovery_status, last_refreshed_at,
                       linking_rule
                  FROM libraries
                 WHERE federation_id = %s::uuid
                   AND is_public = TRUE
                """,
                (federation_id,),
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(
            404,
            "federation_id not found OR library is not marked is_public on this node",
        )
    return {
        "library_id":         row[0],
        "library_subtype":    row[1],
        "canonical_name":     row[2],
        "focal_term":         row[3],
        "description":        row[4],
        "base60_address":     row[5],
        "reference_count":    row[6] or 0,
        "document_count":     row[7] or 0,
        "entity_count":       row[8] or 0,
        "fact_count":         row[9] or 0,
        "first_published_at": row[10].isoformat() if row[10] else None,
        "discovery_status":   row[11],
        "last_refreshed_at":  row[12].isoformat() if row[12] else None,
        "linking_rule":       row[13],
        "federation_id":      federation_id,
    }


@router.get("/{federation_id}")
def public_library_info(federation_id: str):
    """Catalog detail page for a single corpus. Returns library
    metadata + lightweight counts. Reference/document lists live at
    /{federation_id}/references and /{federation_id}/documents."""
    return _library_from_fid(federation_id)


@router.get("/{federation_id}/references")
def public_library_references(
    federation_id: str,
    status: Optional[str] = Query(
        None,
        description="Filter by acquisition_status: pending | acquired | ingested | unavailable | paywall",
    ),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """List bibliographic references in this corpus — what the worker
    has been told to acquire. Returns title + source + acquisition
    status + (when ingested) the resulting document_id so callers can
    deep-link to the rendered document."""
    info = _library_from_fid(federation_id)
    sql = """
        SELECT reference_id, title, citation_text, authors,
               publication_year, publisher, reference_type,
               identifiers, acquisition_status, acquisition_attempts,
               last_attempted_at, document_id, discovered_via,
               file_size_bytes
          FROM bibliographic_references
         WHERE library_id = %s
    """
    params: list = [info["library_id"]]
    if status:
        sql += " AND acquisition_status = %s"
        params.append(status)
    sql += " ORDER BY reference_id LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            cur.execute(
                "SELECT acquisition_status, COUNT(*) FROM bibliographic_references "
                " WHERE library_id = %s GROUP BY acquisition_status",
                (info["library_id"],),
            )
            by_status = {r[0]: r[1] for r in cur.fetchall()}

    references = []
    for r in rows:
        ident = r[7] or {}
        archive_id = ident.get("archive_id") if isinstance(ident, dict) else None
        source_url = ident.get("source_url") if isinstance(ident, dict) else None
        references.append({
            "reference_id":        r[0],
            "title":               r[1],
            "citation_text":       r[2],
            "authors":             r[3],
            "publication_year":    r[4],
            "publisher":           r[5],
            "reference_type":      r[6],
            "archive_id":          archive_id,
            "source_url":          source_url,
            "acquisition_status":  r[8],
            "acquisition_attempts": r[9],
            "last_attempted_at":   r[10].isoformat() if r[10] else None,
            "document_id":         r[11],
            "discovered_via":      r[12],
            "file_size_bytes":     r[13],
        })

    return {
        "federation_id":      federation_id,
        "focal_term":         info["focal_term"],
        "references":         references,
        "returned_count":     len(references),
        "total_references":   info["reference_count"],
        "status_breakdown":   by_status,
        "offset":             offset,
        "limit":              limit,
    }


@router.get("/{federation_id}/documents")
def public_library_documents(
    federation_id: str,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """List documents successfully ingested into this corpus.
    These are the actual .enki containers serving as the corpus."""
    info = _library_from_fid(federation_id)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.document_id, d.title, d.source_url,
                       d.mime_type, d.source_filename,
                       d.created_at, d.content_sha256,
                       d.document_class, d.page_count, d.word_count,
                       LEFT(d.content_sha256, 16) AS short_hash,
                       d.relevance_status,
                       COALESCE(lm.contributor_id, d.contributor_id) AS contributor_id,
                       lm.contributor_weight, lm.submission_status,
                       ct.kind AS contributor_kind, ct.display_name, ct.trust_score
                  FROM library_members lm
                  JOIN documents d ON d.document_id = lm.member_id
                  LEFT JOIN contributors ct
                         ON ct.contributor_id = COALESCE(lm.contributor_id, d.contributor_id)
                 WHERE lm.library_id = %s
                   AND lm.member_kind = 'document'
                 ORDER BY d.created_at DESC NULLS LAST
                 LIMIT %s OFFSET %s
                """,
                (info["library_id"], limit, offset),
            )
            rows = cur.fetchall()

    documents = [
        {
            "document_id":     r[0],
            "title":           r[1],
            "source_url":      r[2],          # permanent source link — always shown
            "mime_type":       r[3],
            "source_filename": r[4],
            "ingested_at":     r[5].isoformat() if r[5] else None,
            "content_sha256":  r[6],
            "short_hash":      r[10],
            "document_class":  r[7],
            "page_count":      r[8],
            "word_count":      r[9],
            # the permanent mark: who contributed it + the relevance verdict
            "relevance_status":  r[11],        # relevant | flagged | None
            "contributor_id":    r[12],
            "contributor_weight": r[13],
            "submission_status": r[14],        # accepted | flagged | pending
            "contributor_kind":  r[15],        # identified | anonymous | system
            "contributor_name":  r[16],
            "contributor_trust": r[17],
        }
        for r in rows
    ]
    return {
        "federation_id":   federation_id,
        "focal_term":      info["focal_term"],
        "documents":       documents,
        "returned_count":  len(documents),
        "total_documents": info["document_count"],
        "offset":          offset,
        "limit":           limit,
    }
