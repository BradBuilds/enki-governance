"""
Mention extraction + persistence.

A mention is a per-occurrence reference to an entity at a specific span
inside a document. Mentions are stored separately from the canonical
`entities` table so spans survive entity merges and we can revisit ambiguous
mentions independently.

This module exposes:
  - persist_mention(...)            store one mention
  - bind_mention(mention_id, entity_id, confidence)   resolve unresolved
  - mark_disputed(mention_id, candidates)             multiple candidates
  - find_unresolved(dataset_id, surface_form)         for the review UI
  - cluster_unresolved_by_surface(...)               grouping for the graph

Mention extraction itself (running an LLM over paragraph text to get
spans) lives in `core/documents/extractor.py` — this module is the
persistence + lifecycle layer.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.documents.mentions")

_PUNCT_RE = re.compile(r"[^A-Za-z0-9 ]+")
_WS_RE = re.compile(r"\s+")


def normalize_surface(s: str) -> str:
    """Match-friendly normalization for surface_form_norm. Aligned with
    OFAC pack and GDU resolver."""
    if not s:
        return ""
    out = s.upper()
    out = _PUNCT_RE.sub(" ", out)
    out = _WS_RE.sub(" ", out).strip()
    return out


def persist_mention(
    *,
    tenant_id: str,
    document_id: int,
    page_id: Optional[int],
    paragraph_id: Optional[int],
    char_start: Optional[int],
    char_end: Optional[int],
    surface_form: str,
    suggested_type: Optional[str] = None,
    candidate_entity_id: Optional[int] = None,
    confidence: Optional[float] = None,
    resolution_status: str = "unresolved",
    extraction_method: Optional[str] = None,
    extraction_engines: Optional[dict] = None,
) -> int:
    """Insert a mention row. Returns mention_id.

    Noise filter: every incoming surface_form is screened against
    core.documents.noise_patterns. Matches (GDS office codes, FOIA
    exemption markers, airport-pair routings, PNR person fragments,
    etc.) are still INSERTED — preserving the audit trail of what the
    extractor saw — but are pre-marked `review_status='rejected'`
    with `reviewed_by='noise_filter_v1'`. Downstream views that filter
    on `review_status` hide them by default; the operator can override
    by inspecting the rejected sample if a real entity was misflagged.
    """
    review_status: Optional[str] = None
    review_reason: Optional[str] = None
    try:
        from core.documents.noise_patterns import classify_noise
        v = classify_noise(surface_form, suggested_type or "")
        if v.action in ("reject", "redaction_marker"):
            review_status = "rejected"
            review_reason = f"{v.kind}: {v.rationale}"
    except Exception:
        # Noise classifier is best-effort. If it fails for any reason
        # (import error, regex throw), we don't want to block the
        # insert — better to write the mention raw than lose it.
        pass

    with get_connection() as conn:
        with conn.cursor() as cur:
            if review_status:
                cur.execute(
                    """
                    INSERT INTO document_mentions
                        (tenant_id, document_id, page_id, paragraph_id,
                         char_start, char_end, surface_form, surface_form_norm,
                         suggested_type, candidate_entity_id, confidence,
                         resolution_status, extraction_method, extraction_engines,
                         review_status, reviewed_by, reviewed_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s::jsonb, %s, %s, NOW())
                    RETURNING mention_id
                    """,
                    (
                        tenant_id, document_id, page_id, paragraph_id,
                        char_start, char_end, surface_form,
                        normalize_surface(surface_form),
                        suggested_type, candidate_entity_id, confidence,
                        resolution_status, extraction_method,
                        json.dumps(extraction_engines or {}),
                        review_status, "noise_filter_v1",
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO document_mentions
                        (tenant_id, document_id, page_id, paragraph_id,
                         char_start, char_end, surface_form, surface_form_norm,
                         suggested_type, candidate_entity_id, confidence,
                         resolution_status, extraction_method, extraction_engines)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    RETURNING mention_id
                    """,
                    (
                        tenant_id, document_id, page_id, paragraph_id,
                        char_start, char_end, surface_form,
                        normalize_surface(surface_form),
                        suggested_type, candidate_entity_id, confidence,
                        resolution_status, extraction_method,
                        json.dumps(extraction_engines or {}),
                    ),
                )
            mention_id = cur.fetchone()[0]
        conn.commit()
    return mention_id


def bind_mention(
    mention_id: int, entity_id: int, confidence: float,
    reviewed_by: Optional[str] = None,
) -> None:
    """Bind an unresolved/disputed mention to a canonical entity. Updates
    entity_surface_forms occurrences in the same transaction."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE document_mentions
                SET candidate_entity_id = %s,
                    confidence = %s,
                    resolution_status = 'bound',
                    review_status = 'reviewed',
                    reviewed_by = %s,
                    reviewed_at = NOW()
                WHERE mention_id = %s
                RETURNING surface_form, surface_form_norm
                """,
                (entity_id, confidence, reviewed_by, mention_id),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                return
            surface, surface_norm = row
            # Bump surface-form occurrences
            cur.execute(
                """
                INSERT INTO entity_surface_forms
                    (entity_id, surface_form, surface_form_norm,
                     occurrences, first_seen_at, last_seen_at)
                VALUES (%s, %s, %s, 1, NOW(), NOW())
                ON CONFLICT (entity_id, surface_form_norm) DO UPDATE SET
                    occurrences = entity_surface_forms.occurrences + 1,
                    last_seen_at = NOW()
                """,
                (entity_id, surface, surface_norm),
            )
        conn.commit()


def mark_disputed(mention_id: int, candidates: list[dict]) -> None:
    """Record that multiple entity candidates exist for a mention.

    `candidates`: [{"entity_id": int, "score": float, "reason": str}, ...]
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE document_mentions
                SET resolution_status = 'disputed',
                    candidates = %s::jsonb,
                    review_status = 'pending'
                WHERE mention_id = %s
                """,
                (json.dumps(candidates), mention_id),
            )
        conn.commit()


def find_unresolved(
    dataset_id: Optional[str] = None,
    surface_form: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """Return unresolved + disputed mentions for the review queue. Filterable
    by dataset (via the document's dataset_id) and by surface form (so the
    UI can say 'show me all 412 unresolved John mentions')."""
    where = ["m.resolution_status IN ('unresolved', 'disputed')"]
    params: list = []
    if dataset_id:
        where.append("d.dataset_id = %s")
        params.append(dataset_id)
    if surface_form:
        where.append("m.surface_form_norm = %s")
        params.append(normalize_surface(surface_form))
    params.append(limit)

    sql = f"""
        SELECT m.mention_id, m.surface_form, m.surface_form_norm,
               m.suggested_type, m.confidence, m.resolution_status,
               m.candidates, m.char_start, m.char_end,
               d.document_id, d.title, d.dataset_id,
               p.paragraph_id, p.raw_text
        FROM document_mentions m
        JOIN documents d ON d.document_id = m.document_id
        LEFT JOIN document_paragraphs p ON p.paragraph_id = m.paragraph_id
        WHERE {' AND '.join(where)}
        ORDER BY m.created_at DESC
        LIMIT %s
    """

    out: list[dict] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for r in cur.fetchall():
                out.append({
                    "mention_id": r[0],
                    "surface_form": r[1],
                    "surface_form_norm": r[2],
                    "suggested_type": r[3],
                    "confidence": r[4],
                    "resolution_status": r[5],
                    "candidates": r[6],
                    "char_start": r[7],
                    "char_end": r[8],
                    "document_id": r[9],
                    "document_title": r[10],
                    "dataset_id": r[11],
                    "paragraph_id": r[12],
                    "paragraph_text": r[13],
                })
    return out


def cluster_unresolved_by_surface(dataset_id: str, limit: int = 50) -> list[dict]:
    """For the graph view: collapse N mentions of the same surface form into
    one cluster node so 1,200 'John' question-marks don't drown the canvas.
    Returns one row per surface form with the mention count."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.surface_form_norm, MIN(m.surface_form), COUNT(*),
                       MAX(m.suggested_type)
                FROM document_mentions m
                JOIN documents d ON d.document_id = m.document_id
                WHERE m.resolution_status IN ('unresolved', 'disputed')
                  AND d.dataset_id = %s
                GROUP BY m.surface_form_norm
                ORDER BY COUNT(*) DESC
                LIMIT %s
                """,
                (dataset_id, limit),
            )
            return [
                {
                    "surface_form_norm": r[0],
                    "surface_form_example": r[1],
                    "occurrence_count": r[2],
                    "suggested_type": r[3],
                }
                for r in cur.fetchall()
            ]
