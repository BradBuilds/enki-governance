"""
Issue persistence — collapse candidates, contradictions, type conflicts.

Issues are first-class nodes in the graph. The system surfaces ambiguity
as a navigable node rather than hiding it in a queue, so the analyst can
inspect surrounding evidence and decide deliberately.

Three families:
  - collapse_candidate  → table `collapse_candidates`
  - contradiction       → table `contradictions` (kind='extraction' or 'fact'
                          or 'type' or 'statement')

Lifecycle:
  open → reviewed/decided → either resolved (merge/keep_separate/...) or
  accepted_both (both readings stay, e.g. genuinely ambiguous).
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.documents.issues")


def record_collapse_candidate(
    *,
    tenant_id: str,
    entity_a_id: int,
    entity_b_id: int,
    evidence_for: list[dict],
    evidence_against: list[dict],
    score: float,
    dataset_id: Optional[str] = None,
    suggested_action: str = "review",
) -> int:
    """Surface a possible merge between two entities. Idempotent on
    (entity_a, entity_b, dataset)."""
    if entity_a_id == entity_b_id:
        raise ValueError("Cannot create collapse candidate with same entity on both sides")

    # Canonicalize order so (A,B) and (B,A) produce the same row
    a, b = sorted([entity_a_id, entity_b_id])

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO collapse_candidates
                    (tenant_id, entity_a_id, entity_b_id,
                     suggested_action, evidence_for, evidence_against,
                     score, dataset_id, status)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, 'pending')
                ON CONFLICT (entity_a_id, entity_b_id, dataset_id) DO UPDATE SET
                    evidence_for = EXCLUDED.evidence_for,
                    evidence_against = EXCLUDED.evidence_against,
                    score = EXCLUDED.score,
                    suggested_action = EXCLUDED.suggested_action
                RETURNING candidate_id
                """,
                (
                    tenant_id, a, b, suggested_action,
                    json.dumps(evidence_for), json.dumps(evidence_against),
                    score, dataset_id,
                ),
            )
            candidate_id = cur.fetchone()[0]
        conn.commit()
    return candidate_id


def decide_collapse_candidate(
    candidate_id: int, decision: str,
    decided_by: str, decision_note: Optional[str] = None,
) -> dict:
    """Resolve a pending candidate. decision ∈ {merge, keep_separate, rejected}.
    For 'merge', the caller is responsible for calling
    core.gdu.entity_resolution.merge_entities — this function only marks
    the queue row."""
    if decision not in ("merge", "keep_separate", "rejected"):
        raise ValueError(f"unknown decision: {decision}")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE collapse_candidates
                SET status = CASE
                        WHEN %s = 'merge' THEN 'merged'
                        WHEN %s = 'keep_separate' THEN 'kept_separate'
                        ELSE 'rejected'
                    END,
                    decided_by = %s,
                    decided_at = NOW(),
                    decision_note = %s
                WHERE candidate_id = %s
                RETURNING entity_a_id, entity_b_id, status
                """,
                (decision, decision, decided_by, decision_note, candidate_id),
            )
            row = cur.fetchone()
        conn.commit()
    if not row:
        return {"ok": False, "reason": "not found"}
    return {"ok": True, "candidate_id": candidate_id,
            "entity_a_id": row[0], "entity_b_id": row[1], "status": row[2]}


def pending_candidates(
    dataset_id: Optional[str] = None, limit: int = 100,
) -> list[dict]:
    """List candidates awaiting decision. Drives the review queue UI."""
    where = ["status = 'pending'"]
    params: list = []
    if dataset_id:
        where.append("dataset_id = %s")
        params.append(dataset_id)
    params.append(limit)

    sql = f"""
        SELECT c.candidate_id, c.entity_a_id, c.entity_b_id,
               ea.canonical_name, eb.canonical_name,
               ea.canonical_type, eb.canonical_type,
               c.suggested_action, c.score, c.evidence_for, c.evidence_against,
               c.dataset_id, c.created_at
        FROM collapse_candidates c
        LEFT JOIN entities ea ON ea.entity_id = c.entity_a_id
        LEFT JOIN entities eb ON eb.entity_id = c.entity_b_id
        WHERE {' AND '.join(where)}
        ORDER BY c.score DESC, c.created_at DESC
        LIMIT %s
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [
                {
                    "candidate_id": r[0],
                    "entity_a_id": r[1], "entity_b_id": r[2],
                    "entity_a_name": r[3], "entity_b_name": r[4],
                    "entity_a_type": r[5], "entity_b_type": r[6],
                    "suggested_action": r[7],
                    "score": r[8],
                    "evidence_for": r[9],
                    "evidence_against": r[10],
                    "dataset_id": r[11],
                    "created_at": r[12].isoformat() if r[12] else None,
                }
                for r in cur.fetchall()
            ]


def record_contradiction(
    *,
    tenant_id: str,
    kind: str,
    document_id: Optional[int] = None,
    page_id: Optional[int] = None,
    char_start: Optional[int] = None,
    char_end: Optional[int] = None,
    reading_a: Optional[str] = None,
    reading_b: Optional[str] = None,
    statement_a_id: Optional[int] = None,
    statement_b_id: Optional[int] = None,
    entity_id: Optional[int] = None,
    severity: float = 0.5,
) -> int:
    """Record a contradiction. kind ∈ {extraction, fact, type, statement}."""
    if kind not in ("extraction", "fact", "type", "statement"):
        raise ValueError(f"unknown contradiction kind: {kind}")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO contradictions
                    (tenant_id, kind, document_id, page_id,
                     char_start, char_end, reading_a, reading_b,
                     statement_a_id, statement_b_id, entity_id,
                     severity, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'open')
                RETURNING contradiction_id
                """,
                (
                    tenant_id, kind, document_id, page_id,
                    char_start, char_end, reading_a, reading_b,
                    statement_a_id, statement_b_id, entity_id, severity,
                ),
            )
            cid = cur.fetchone()[0]
        conn.commit()
    return cid


def open_contradictions(
    dataset_id: Optional[str] = None,
    entity_id: Optional[int] = None,
    limit: int = 100,
) -> list[dict]:
    where = ["c.status = 'open'"]
    params: list = []
    if entity_id:
        where.append("c.entity_id = %s")
        params.append(entity_id)
    if dataset_id:
        where.append("d.dataset_id = %s")
        params.append(dataset_id)
    params.append(limit)

    sql = f"""
        SELECT c.contradiction_id, c.kind, c.reading_a, c.reading_b,
               c.severity, c.entity_id, e.canonical_name,
               c.document_id, d.title, d.dataset_id,
               c.page_id, p.page_number, c.created_at
        FROM contradictions c
        LEFT JOIN entities e ON e.entity_id = c.entity_id
        LEFT JOIN documents d ON d.document_id = c.document_id
        LEFT JOIN document_pages p ON p.page_id = c.page_id
        WHERE {' AND '.join(where)}
        ORDER BY c.severity DESC, c.created_at DESC
        LIMIT %s
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [
                {
                    "contradiction_id": r[0], "kind": r[1],
                    "reading_a": r[2], "reading_b": r[3],
                    "severity": r[4],
                    "entity_id": r[5], "entity_name": r[6],
                    "document_id": r[7], "document_title": r[8],
                    "dataset_id": r[9],
                    "page_id": r[10], "page_number": r[11],
                    "created_at": r[12].isoformat() if r[12] else None,
                }
                for r in cur.fetchall()
            ]
