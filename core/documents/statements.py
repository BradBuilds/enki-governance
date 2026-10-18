"""
Statement persistence — claims/assertions extracted from documents.

A statement is the "who said what about whom" unit. Subject-predicate-object,
plus the document trail (page, paragraph). Backs the testimony / accusation
graph: a deposition becomes a series of Statement nodes whose `predicate`
is one of {testified_against, accused, alleged, named_as_associate,
denied, corroborated, claimed_to_know, ...} and whose object_entity points
at the person/event named.

Statements coexist with the existing `assertions` table — that one stores
fully-normalized subject-predicate-object claims in canonical vocab, this
one stores the per-document occurrence with verbatim text and modality.
A statement promotes to an assertion when corroborated_by_count crosses
the promotion threshold for its dataset.
"""

from __future__ import annotations

import logging
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.documents.statements")

# Canonical predicates for the testimony / "who told on who" graph.
# Mirrored in edgevocab so the same string flows from extractor → DB → graph.
TESTIMONY_PREDICATES = {
    "testified_against",
    "testified_for",
    "accused",
    "accused_by",
    "named_as_associate",
    "informed_on",
    "alleged",
    "denied",
    "witnessed",
    "corroborated",
    "claimed_to_know",
    "introduced",
    "traveled_with",
    "met_with",
    "communicated_with",
    "paid",
    "received_from",
    "employed",
    "represented",
}


def persist_statement(
    *,
    tenant_id: str,
    document_id: int,
    page_id: Optional[int],
    paragraph_id: Optional[int],
    speaker_entity_id: Optional[int],
    predicate: str,
    object_entity_id: Optional[int] = None,
    raw_text: Optional[str] = None,
    summarized_text: Optional[str] = None,
    modality: str = "asserted",
    confidence: float = 0.7,
    polarity: str = "affirmative",
    speaker_role: Optional[str] = None,
    speaker_mention_id: Optional[int] = None,
    object_mention_id: Optional[int] = None,
    extraction_method: str = "llm",
    extraction_profile_id: Optional[str] = None,
) -> int:
    """Insert a statement row. Returns statement_id."""
    if predicate not in TESTIMONY_PREDICATES:
        log.debug("Statement with non-standard predicate=%s — accepted but not in vocab", predicate)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO statements
                    (tenant_id, document_id, page_id, paragraph_id,
                     speaker_entity_id, speaker_mention_id,
                     predicate, object_entity_id, object_mention_id,
                     raw_text, summarized_text,
                     modality, confidence, polarity, speaker_role,
                     extraction_method, extraction_profile_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s)
                RETURNING statement_id
                """,
                (
                    tenant_id, document_id, page_id, paragraph_id,
                    speaker_entity_id, speaker_mention_id,
                    predicate, object_entity_id, object_mention_id,
                    raw_text, summarized_text,
                    modality, confidence, polarity, speaker_role,
                    extraction_method, extraction_profile_id,
                ),
            )
            statement_id = cur.fetchone()[0]
        conn.commit()
    return statement_id


def statements_about(entity_id: int, limit: int = 50) -> list[dict]:
    """Every statement where this entity is the OBJECT — i.e. what has been
    said ABOUT them. Powers the 'who told on me?' panel in the dossier."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.statement_id, s.predicate, s.raw_text, s.summarized_text,
                       s.modality, s.confidence, s.polarity, s.speaker_role,
                       s.speaker_entity_id, e.canonical_name,
                       s.document_id, d.title, d.dataset_id,
                       s.page_id, p.page_number, s.paragraph_id,
                       s.created_at,
                       s.corroborated_by_count, s.contradicted_by_count
                FROM statements s
                LEFT JOIN entities e ON e.entity_id = s.speaker_entity_id
                LEFT JOIN documents d ON d.document_id = s.document_id
                LEFT JOIN document_pages p ON p.page_id = s.page_id
                WHERE s.object_entity_id = %s
                ORDER BY s.confidence DESC, s.created_at DESC
                LIMIT %s
                """,
                (entity_id, limit),
            )
            return [
                {
                    "statement_id": r[0],
                    "predicate": r[1],
                    "raw_text": r[2],
                    "summarized_text": r[3],
                    "modality": r[4],
                    "confidence": r[5],
                    "polarity": r[6],
                    "speaker_role": r[7],
                    "speaker_entity_id": r[8],
                    "speaker_name": r[9],
                    "document_id": r[10],
                    "document_title": r[11],
                    "dataset_id": r[12],
                    "page_id": r[13],
                    "page_number": r[14],
                    "paragraph_id": r[15],
                    "created_at": r[16].isoformat() if r[16] else None,
                    "corroborated_by_count": r[17],
                    "contradicted_by_count": r[18],
                }
                for r in cur.fetchall()
            ]


def statements_by(entity_id: int, limit: int = 50) -> list[dict]:
    """Every statement where this entity is the SPEAKER — what they have said.
    Powers the 'their own testimony' panel."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.statement_id, s.predicate, s.raw_text, s.summarized_text,
                       s.modality, s.confidence, s.polarity,
                       s.object_entity_id, e.canonical_name,
                       s.document_id, d.title, d.dataset_id,
                       s.created_at
                FROM statements s
                LEFT JOIN entities e ON e.entity_id = s.object_entity_id
                LEFT JOIN documents d ON d.document_id = s.document_id
                WHERE s.speaker_entity_id = %s
                ORDER BY s.created_at DESC
                LIMIT %s
                """,
                (entity_id, limit),
            )
            return [
                {
                    "statement_id": r[0],
                    "predicate": r[1],
                    "raw_text": r[2],
                    "summarized_text": r[3],
                    "modality": r[4],
                    "confidence": r[5],
                    "polarity": r[6],
                    "object_entity_id": r[7],
                    "object_name": r[8],
                    "document_id": r[9],
                    "document_title": r[10],
                    "dataset_id": r[11],
                    "created_at": r[12].isoformat() if r[12] else None,
                }
                for r in cur.fetchall()
            ]


def testimony_chain(entity_id: int) -> dict:
    """Aggregate 'who told on this entity' + 'whom did this entity tell on'.
    Used by the dossier testimony panel and the graph view."""
    return {
        "entity_id": entity_id,
        "said_about_them": statements_about(entity_id, limit=100),
        "said_by_them": statements_by(entity_id, limit=100),
    }
