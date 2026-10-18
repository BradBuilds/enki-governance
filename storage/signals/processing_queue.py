"""
Processing Queue — universal examination layer for all ingestion.

Every signal from every source flows through here. The queue determines
whether data goes straight to LDU-W (instant), gets auto-approved,
or needs human review.

Three speeds:
  INSTANT:       structured + known type + confidence >= 95% → LDU-W
  AUTO-APPROVED: structured + known type + confidence >= 80% → LDU-W (logged)
  QUEUED:        everything else → sits in queue → human review

The queue tracks:
  - Structural parameters (fields, types, encoding)
  - Classification (proposed type, vocabulary status, AI confidence)
  - Content extraction (OCR text, key phrases, observables)
  - Grouping (similar items grouped for bulk approval)
  - Validation state (who approved, when, notes)
"""

from __future__ import annotations

import json
import uuid
from typing import Optional
from datetime import datetime, timezone

from storage.postgres.db import get_connection


# ─── Routing Decision ─────────────────────────────────────────────────────────

INSTANT_THRESHOLD = 0.95
AUTO_APPROVE_THRESHOLD = 0.80


def route_signal(
    signal_type: Optional[str],
    confidence: float,
    vocabulary_types: set,
) -> str:
    """Decide how a signal should be routed.

    Returns: 'instant', 'auto_approved', or 'queued'
    """
    if not signal_type:
        return "queued"

    in_vocab = signal_type in vocabulary_types

    if in_vocab and confidence >= INSTANT_THRESHOLD:
        return "instant"
    elif in_vocab and confidence >= AUTO_APPROVE_THRESHOLD:
        return "auto_approved"
    else:
        return "queued"


# ─── Import Jobs ──────────────────────────────────────────────────────────────

def create_import_job(
    tenant_id: str,
    source_type: str,
    source_name: str,
    created_by: str = "system",
    dsn: Optional[str] = None,
) -> dict:
    """Create a new import job for bulk ingestion."""
    job_id = f"job_{uuid.uuid4().hex[:12]}"

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO import_jobs (job_id, tenant_id, source_type, source_name, created_by)
                VALUES (%s, %s, %s, %s, %s)
            """, (job_id, tenant_id, source_type, source_name, created_by))
            conn.commit()

    return {"job_id": job_id, "source_type": source_type, "source_name": source_name, "status": "scanning"}


def get_import_job(job_id: str, tenant_id: str, dsn: Optional[str] = None) -> Optional[dict]:
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT job_id, source_type, source_name, total_chunks, processed,
                       validated, rejected, promoted, status, created_at, completed_at
                FROM import_jobs WHERE job_id = %s AND tenant_id = %s
            """, (job_id, tenant_id))
            r = cur.fetchone()
            if not r:
                return None
            return {
                "job_id": r[0], "source_type": r[1], "source_name": r[2],
                "total_chunks": r[3], "processed": r[4], "validated": r[5],
                "rejected": r[6], "promoted": r[7], "status": r[8],
                "created_at": r[9].isoformat() if r[9] else None,
                "completed_at": r[10].isoformat() if r[10] else None,
            }


def list_import_jobs(tenant_id: str, dsn: Optional[str] = None) -> list[dict]:
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT job_id, source_type, source_name, total_chunks, processed,
                       validated, rejected, promoted, status, created_at
                FROM import_jobs WHERE tenant_id = %s ORDER BY created_at DESC
            """, (tenant_id,))
            return [{
                "job_id": r[0], "source_type": r[1], "source_name": r[2],
                "total_chunks": r[3], "processed": r[4], "validated": r[5],
                "rejected": r[6], "promoted": r[7], "status": r[8],
                "created_at": r[9].isoformat() if r[9] else None,
            } for r in cur.fetchall()]


def update_job_counts(job_id: str, dsn: Optional[str] = None) -> None:
    """Recalculate job counts from processing_queue."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE import_jobs SET
                    total_chunks = (SELECT COUNT(*) FROM processing_queue WHERE job_id = %s),
                    processed = (SELECT COUNT(*) FROM processing_queue WHERE job_id = %s AND status NOT IN ('pending', 'processing')),
                    validated = (SELECT COUNT(*) FROM processing_queue WHERE job_id = %s AND status = 'validated'),
                    rejected = (SELECT COUNT(*) FROM processing_queue WHERE job_id = %s AND status = 'rejected'),
                    promoted = (SELECT COUNT(*) FROM processing_queue WHERE job_id = %s AND status = 'promoted')
                WHERE job_id = %s
            """, (job_id, job_id, job_id, job_id, job_id, job_id))
            conn.commit()


# ─── Processing Queue ─────────────────────────────────────────────────────────

def enqueue_chunk(
    tenant_id: str,
    ldu_t_id: str,
    job_id: Optional[str] = None,
    source_path: Optional[str] = None,
    source_archive: Optional[str] = None,
    chunk_index: Optional[int] = None,
    size_bytes: Optional[int] = None,
    dsn: Optional[str] = None,
) -> str:
    """Add a chunk to the processing queue. Returns chunk_id."""
    chunk_id = f"chunk_{uuid.uuid4().hex[:12]}"

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO processing_queue
                    (chunk_id, job_id, tenant_id, ldu_t_id, source_path,
                     source_archive, chunk_index, size_bytes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (chunk_id, job_id, tenant_id, ldu_t_id, source_path,
                  source_archive, chunk_index, size_bytes))
            conn.commit()

    return chunk_id


def update_chunk_processed(
    chunk_id: str,
    format_detected: str,
    parser_used: str,
    parser_confidence: float,
    parsed_content: dict,
    proposed_signal_type: Optional[str],
    vocabulary_status: str,
    field_names: list[str],
    text_preview: Optional[str] = None,
    ocr_text: Optional[str] = None,
    observables_found: Optional[list] = None,
    key_phrases: Optional[list] = None,
    ai_classification: Optional[dict] = None,
    ai_confidence: Optional[float] = None,
    vocabulary_gaps: Optional[list] = None,
    similar_types: Optional[list] = None,
    proposed_group: Optional[str] = None,
    dsn: Optional[str] = None,
) -> None:
    """Update a chunk after processing (parsing, OCR, AI classification).
    Status defaults to 'parsed' for the classifier to pick up."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE processing_queue SET
                    status = 'parsed',
                    format_detected = %s,
                    parser_used = %s,
                    parser_confidence = %s,
                    parsed_content = %s,
                    proposed_signal_type = %s,
                    vocabulary_status = %s,
                    field_names = %s,
                    field_count = %s,
                    text_preview = %s,
                    ocr_text = %s,
                    observables_found = %s,
                    key_phrases = %s,
                    ai_classification = %s,
                    ai_confidence = %s,
                    vocabulary_gaps = %s,
                    similar_types = %s,
                    proposed_group = %s,
                    processed_at = now()
                WHERE chunk_id = %s
            """, (
                format_detected, parser_used, parser_confidence,
                json.dumps(parsed_content), proposed_signal_type, vocabulary_status,
                field_names, len(field_names),
                text_preview, ocr_text,
                json.dumps(observables_found) if observables_found else None,
                key_phrases,
                json.dumps(ai_classification) if ai_classification else None,
                ai_confidence,
                vocabulary_gaps, similar_types, proposed_group,
                chunk_id,
            ))
            conn.commit()


def validate_chunk(
    chunk_id: str,
    validated_by: str,
    action: str,  # "approve", "reject", "edit"
    signal_type_override: Optional[str] = None,
    note: Optional[str] = None,
    dsn: Optional[str] = None,
) -> dict:
    """Validate a processed chunk: approve, reject, or edit."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            if action == "reject":
                cur.execute("""
                    UPDATE processing_queue SET
                        status = 'rejected', validated_by = %s,
                        validation_note = %s, validated_at = now()
                    WHERE chunk_id = %s
                    RETURNING chunk_id, status
                """, (validated_by, note, chunk_id))
            elif action in ("approve", "edit"):
                new_type = signal_type_override  # None = use proposed type
                cur.execute("""
                    UPDATE processing_queue SET
                        status = 'validated', validated_by = %s,
                        validation_note = %s, validated_at = now(),
                        proposed_signal_type = COALESCE(%s, proposed_signal_type)
                    WHERE chunk_id = %s
                    RETURNING chunk_id, status, proposed_signal_type, parsed_content, ldu_t_id, tenant_id
                """, (validated_by, note, new_type, chunk_id))
            else:
                return {"error": "Invalid action"}

            row = cur.fetchone()
            conn.commit()

            if not row:
                return {"error": "CHUNK_NOT_FOUND"}

            return {"chunk_id": row[0], "status": row[1]}


def auto_approve_chunk(chunk_id: str, dsn: Optional[str] = None) -> None:
    """Mark a chunk as auto-approved (high confidence + known type)."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE processing_queue SET
                    status = 'validated', auto_approved = true,
                    validated_by = 'system', validated_at = now()
                WHERE chunk_id = %s
            """, (chunk_id,))
            conn.commit()


def promote_chunk(chunk_id: str, signal_id: str, signal_rid: int, dsn: Optional[str] = None) -> None:
    """Mark a validated chunk as promoted to LDU-W."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE processing_queue SET
                    status = 'promoted', signal_id = %s, signal_rid = %s
                WHERE chunk_id = %s
            """, (signal_id, signal_rid, chunk_id))
            conn.commit()


def validate_group(
    proposed_group: str,
    tenant_id: str,
    validated_by: str,
    action: str,
    signal_type_override: Optional[str] = None,
    note: Optional[str] = None,
    dsn: Optional[str] = None,
) -> dict:
    """Bulk validate all chunks in a group."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            if action == "reject":
                cur.execute("""
                    UPDATE processing_queue SET
                        status = 'rejected', validated_by = %s,
                        validation_note = %s, validated_at = now()
                    WHERE proposed_group = %s AND tenant_id = %s AND status = 'processed'
                """, (validated_by, note, proposed_group, tenant_id))
            else:
                cur.execute("""
                    UPDATE processing_queue SET
                        status = 'validated', validated_by = %s,
                        validation_note = %s, validated_at = now(),
                        proposed_signal_type = COALESCE(%s, proposed_signal_type)
                    WHERE proposed_group = %s AND tenant_id = %s AND status = 'processed'
                """, (validated_by, note, signal_type_override, proposed_group, tenant_id))

            count = cur.rowcount
            conn.commit()

    return {"action": action, "group": proposed_group, "count": count}


# ─── Query ────────────────────────────────────────────────────────────────────

def list_queue(
    tenant_id: str,
    status: Optional[str] = None,
    job_id: Optional[str] = None,
    proposed_type: Optional[str] = None,
    limit: int = 100,
    dsn: Optional[str] = None,
) -> list[dict]:
    """List items in the processing queue with filters."""
    conditions = ["tenant_id = %s"]
    params: list = [tenant_id]

    if status:
        conditions.append("status = %s")
        params.append(status)
    if job_id:
        conditions.append("job_id = %s")
        params.append(job_id)
    if proposed_type:
        conditions.append("proposed_signal_type = %s")
        params.append(proposed_type)

    params.append(limit)

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT chunk_id, job_id, ldu_t_id, source_path, source_archive,
                       status, format_detected, parser_confidence,
                       proposed_signal_type, vocabulary_status,
                       text_preview, ai_confidence, proposed_group,
                       auto_approved, size_bytes, field_count,
                       created_at, processed_at, validated_at
                FROM processing_queue
                WHERE {' AND '.join(conditions)}
                ORDER BY created_at DESC
                LIMIT %s
            """, params)
            return [{
                "chunk_id": r[0], "job_id": r[1], "ldu_t_id": r[2],
                "source_path": r[3], "source_archive": r[4],
                "status": r[5], "format_detected": r[6], "parser_confidence": r[7],
                "proposed_signal_type": r[8], "vocabulary_status": r[9],
                "text_preview": r[10], "ai_confidence": r[11],
                "proposed_group": r[12], "auto_approved": r[13],
                "size_bytes": r[14], "field_count": r[15],
                "created_at": r[16].isoformat() if r[16] else None,
                "processed_at": r[17].isoformat() if r[17] else None,
                "validated_at": r[18].isoformat() if r[18] else None,
            } for r in cur.fetchall()]


def get_chunk(chunk_id: str, tenant_id: str, dsn: Optional[str] = None) -> Optional[dict]:
    """Get full chunk details including parsed content."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT chunk_id, job_id, ldu_t_id, source_path, source_archive,
                       chunk_index, status, format_detected, parser_used,
                       parser_confidence, parsed_content, ocr_text, transcription,
                       ai_classification, ai_confidence, field_names, field_count,
                       row_count, encoding, size_bytes, proposed_signal_type,
                       vocabulary_status, vocabulary_gaps, similar_types,
                       text_preview, observables_found, key_phrases,
                       proposed_group, validated_by, validation_note,
                       auto_approved, signal_id, created_at, processed_at, validated_at
                FROM processing_queue
                WHERE chunk_id = %s AND tenant_id = %s
            """, (chunk_id, tenant_id))
            r = cur.fetchone()
            if not r:
                return None
            return {
                "chunk_id": r[0], "job_id": r[1], "ldu_t_id": r[2],
                "source_path": r[3], "source_archive": r[4], "chunk_index": r[5],
                "status": r[6], "format_detected": r[7], "parser_used": r[8],
                "parser_confidence": r[9], "parsed_content": r[10],
                "ocr_text": r[11], "transcription": r[12],
                "ai_classification": r[13], "ai_confidence": r[14],
                "field_names": r[15], "field_count": r[16],
                "row_count": r[17], "encoding": r[18], "size_bytes": r[19],
                "proposed_signal_type": r[20], "vocabulary_status": r[21],
                "vocabulary_gaps": r[22], "similar_types": r[23],
                "text_preview": r[24], "observables_found": r[25],
                "key_phrases": r[26], "proposed_group": r[27],
                "validated_by": r[28], "validation_note": r[29],
                "auto_approved": r[30], "signal_id": r[31],
                "created_at": r[32].isoformat() if r[32] else None,
                "processed_at": r[33].isoformat() if r[33] else None,
                "validated_at": r[34].isoformat() if r[34] else None,
            }


def get_queue_summary(tenant_id: str, dsn: Optional[str] = None) -> dict:
    """Get counts by status for dashboard display."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT status, COUNT(*) FROM processing_queue
                WHERE tenant_id = %s GROUP BY status
            """, (tenant_id,))
            counts = {r[0]: r[1] for r in cur.fetchall()}

            # Groups for bulk approval
            cur.execute("""
                SELECT proposed_group, proposed_signal_type, COUNT(*), AVG(parser_confidence)
                FROM processing_queue
                WHERE tenant_id = %s AND status = 'processed' AND proposed_group IS NOT NULL
                GROUP BY proposed_group, proposed_signal_type
                ORDER BY COUNT(*) DESC
                LIMIT 20
            """, (tenant_id,))
            groups = [{
                "group": r[0], "proposed_type": r[1],
                "count": r[2], "avg_confidence": round(r[3], 2) if r[3] else None,
            } for r in cur.fetchall()]

    return {
        "pending": counts.get("pending", 0),
        "processing": counts.get("processing", 0),
        "processed": counts.get("processed", 0),
        "validated": counts.get("validated", 0),
        "promoted": counts.get("promoted", 0),
        "rejected": counts.get("rejected", 0),
        "total": sum(counts.values()),
        "groups": groups,
    }
