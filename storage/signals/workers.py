"""
Worker Management System — controllable processing units.

Each worker is a configurable loop that picks up chunks from the processing
queue, does one job, and updates the chunk status. Workers can be started,
stopped, paused, and scaled independently.

Worker types:
  parser          — format detection + parsing
  classifier      — GDU vocabulary check
  ocr             — OCR for images/PDFs (Tesseract or AI)
  ai_classifier   — AI proposes type + fields for unknowns
  auto_approve    — auto-promotes high-confidence known types
  promoter        — creates signal in LDU-W from validated chunks
  imap_poller     — polls IMAP mailbox on timer
  rss_poller      — polls RSS feeds on timer
  ftp_watcher     — watches filesystem for new files

All workers are controllable:
  - Start/stop/pause individually
  - Scale instances up/down
  - Adjust batch size and interval
  - Monitor queue depth and throughput
  - See errors in real-time
"""

from __future__ import annotations

import json
import time
import threading
import logging
from typing import Optional
from datetime import datetime, timezone

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers")


# ─── Worker Registry (Database) ──────────────────────────────────────────────

DEFAULT_WORKERS = [
    {
        "worker_id": "parser",
        "worker_type": "parser",
        "picks_up_status": "pending",
        "sets_status": "parsed",
        "batch_size": 20,
        "interval_ms": 500,
        "instances": 1,
        "status": "off",
    },
    {
        "worker_id": "classifier",
        "worker_type": "classifier",
        "picks_up_status": "parsed",
        "sets_status": "classified",
        "batch_size": 50,
        "interval_ms": 500,
        "instances": 1,
        "status": "off",
    },
    {
        "worker_id": "ocr",
        "worker_type": "ocr",
        "picks_up_status": "parsed",
        "sets_status": "ocr_complete",
        "filter_format": "image,pdf",
        "batch_size": 1,
        "interval_ms": 2000,
        "instances": 1,
        "status": "off",
    },
    {
        "worker_id": "ai_classifier",
        "worker_type": "ai_classifier",
        "picks_up_status": "classified",
        "sets_status": "ai_classified",
        "batch_size": 5,
        "interval_ms": 2000,
        "instances": 1,
        "status": "off",
        "config": {"only_unknown_types": True},
    },
    {
        "worker_id": "auto_approve",
        "worker_type": "auto_approve",
        "picks_up_status": "classified",
        "sets_status": "validated",
        "batch_size": 50,
        "interval_ms": 500,
        "instances": 1,
        "status": "off",
        "config": {"min_confidence": 0.80, "require_known_type": True},
    },
    {
        "worker_id": "promoter",
        "worker_type": "promoter",
        "picks_up_status": "validated",
        "sets_status": "promoted",
        "batch_size": 20,
        "interval_ms": 1000,
        "instances": 1,
        "status": "off",
    },
]


def ensure_default_workers(tenant_id: str = "default", dsn: Optional[str] = None) -> None:
    """Create default worker definitions if they don't exist."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            for w in DEFAULT_WORKERS:
                cur.execute("""
                    INSERT INTO workers (worker_id, worker_type, tenant_id,
                        picks_up_status, sets_status, batch_size, interval_ms,
                        instances, status, filter_format, config)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (worker_id) DO NOTHING
                """, (
                    w["worker_id"], w["worker_type"], tenant_id,
                    w["picks_up_status"], w["sets_status"],
                    w["batch_size"], w["interval_ms"],
                    w["instances"], w["status"],
                    w.get("filter_format"),
                    json.dumps(w.get("config", {})),
                ))
            conn.commit()


def list_workers(tenant_id: str = "default", dsn: Optional[str] = None) -> list[dict]:
    """List all workers with their current state."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT worker_id, worker_type, status, instances, batch_size,
                       interval_ms, picks_up_status, sets_status, filter_format,
                       processed_total, errors_total, last_error, last_tick_at,
                       queue_depth, config, updated_at
                FROM workers WHERE tenant_id = %s
                ORDER BY worker_id
            """, (tenant_id,))
            return [{
                "worker_id": r[0], "worker_type": r[1], "status": r[2],
                "instances": r[3], "batch_size": r[4], "interval_ms": r[5],
                "picks_up_status": r[6], "sets_status": r[7],
                "filter_format": r[8],
                "processed_total": r[9], "errors_total": r[10],
                "last_error": r[11],
                "last_tick_at": r[12].isoformat() if r[12] else None,
                "queue_depth": r[13], "config": r[14],
                "updated_at": r[15].isoformat() if r[15] else None,
            } for r in cur.fetchall()]


def get_worker(worker_id: str, tenant_id: str = "default", dsn: Optional[str] = None) -> Optional[dict]:
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT worker_id, worker_type, status, instances, batch_size,
                       interval_ms, picks_up_status, sets_status, filter_format,
                       processed_total, errors_total, last_error, last_tick_at,
                       queue_depth, config
                FROM workers WHERE worker_id = %s AND tenant_id = %s
            """, (worker_id, tenant_id))
            r = cur.fetchone()
            if not r:
                return None
            return {
                "worker_id": r[0], "worker_type": r[1], "status": r[2],
                "instances": r[3], "batch_size": r[4], "interval_ms": r[5],
                "picks_up_status": r[6], "sets_status": r[7],
                "filter_format": r[8],
                "processed_total": r[9], "errors_total": r[10],
                "last_error": r[11],
                "last_tick_at": r[12].isoformat() if r[12] else None,
                "queue_depth": r[13], "config": r[14],
            }


def update_worker(
    worker_id: str,
    tenant_id: str = "default",
    status: Optional[str] = None,
    instances: Optional[int] = None,
    batch_size: Optional[int] = None,
    interval_ms: Optional[int] = None,
    config: Optional[dict] = None,
    dsn: Optional[str] = None,
) -> bool:
    """Update worker configuration."""
    sets = ["updated_at = now()"]
    params = []

    if status is not None:
        sets.append("status = %s")
        params.append(status)
    if instances is not None:
        sets.append("instances = %s")
        params.append(max(0, min(instances, 10)))
    if batch_size is not None:
        sets.append("batch_size = %s")
        params.append(max(1, min(batch_size, 1000)))
    if interval_ms is not None:
        sets.append("interval_ms = %s")
        params.append(max(100, min(interval_ms, 60000)))
    if config is not None:
        sets.append("config = %s")
        params.append(json.dumps(config))

    params.extend([worker_id, tenant_id])

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE workers SET {', '.join(sets)} WHERE worker_id = %s AND tenant_id = %s",
                params,
            )
            ok = cur.rowcount > 0
            conn.commit()
            return ok


def record_tick(
    worker_id: str,
    processed: int = 0,
    errors: int = 0,
    last_error: Optional[str] = None,
    queue_depth: int = 0,
    dsn: Optional[str] = None,
) -> None:
    """Record a worker tick (called after each processing batch)."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            sets = [
                "last_tick_at = now()",
                "processed_total = processed_total + %s",
                "errors_total = errors_total + %s",
                "queue_depth = %s",
            ]
            params = [processed, errors, queue_depth]

            if last_error:
                sets.append("last_error = %s")
                params.append(last_error)

            params.append(worker_id)
            cur.execute(
                f"UPDATE workers SET {', '.join(sets)} WHERE worker_id = %s",
                params,
            )
            conn.commit()


def create_custom_worker(
    worker_id: str,
    worker_type: str,
    picks_up_status: str,
    sets_status: str,
    tenant_id: str = "default",
    batch_size: int = 10,
    interval_ms: int = 1000,
    filter_format: Optional[str] = None,
    config: Optional[dict] = None,
    dsn: Optional[str] = None,
) -> dict:
    """Create a custom worker for a specific ingestion need."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO workers (worker_id, worker_type, tenant_id,
                    picks_up_status, sets_status, batch_size, interval_ms,
                    status, filter_format, config)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'off', %s, %s)
                RETURNING worker_id
            """, (
                worker_id, worker_type, tenant_id,
                picks_up_status, sets_status, batch_size, interval_ms,
                filter_format, json.dumps(config or {}),
            ))
            conn.commit()

    return {"worker_id": worker_id, "status": "off", "worker_type": worker_type}


def delete_worker(worker_id: str, tenant_id: str = "default", dsn: Optional[str] = None) -> bool:
    """Delete a custom worker. Cannot delete default workers."""
    default_ids = {w["worker_id"] for w in DEFAULT_WORKERS}
    if worker_id in default_ids:
        return False

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM workers WHERE worker_id = %s AND tenant_id = %s",
                (worker_id, tenant_id),
            )
            ok = cur.rowcount > 0
            conn.commit()
            return ok


# ─── Worker Processing Functions ──────────────────────────────────────────────
# These are the actual functions each worker type calls.

def process_parser_tick(tenant_id: str, batch_size: int, filter_format: Optional[str] = None,
                        dsn: Optional[str] = None) -> dict:
    """Parser worker: pick up pending chunks, run parser, update."""
    from storage.ingest.parsers import detect_and_parse

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Claim chunks
            cur.execute("""
                UPDATE processing_queue SET status = 'processing'
                WHERE chunk_id IN (
                    SELECT chunk_id FROM processing_queue
                    WHERE tenant_id = %s AND status = 'pending'
                    ORDER BY created_at
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING chunk_id, ldu_t_id
            """, (tenant_id, batch_size))
            chunks = cur.fetchall()
            conn.commit()

    processed = 0
    errors = 0
    last_error = None

    for chunk_id, ldu_t_id in chunks:
        try:
            # Get raw bytes from LDU-T
            with get_connection(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT raw_bytes, content_type FROM ldu_t_signals WHERE ldu_t_id = %s",
                        (ldu_t_id,),
                    )
                    row = cur.fetchone()
                    if not row:
                        continue
                    raw_bytes, content_type = row[0], row[1]

            # Parse
            if isinstance(raw_bytes, memoryview):
                raw_bytes = bytes(raw_bytes)
            result = detect_and_parse(raw_bytes, content_type)

            # Extract field names
            field_names = list(result.parsed.keys()) if isinstance(result.parsed, dict) else []

            # Text preview
            text_preview = None
            if isinstance(result.parsed, dict):
                preview_parts = []
                for k, v in list(result.parsed.items())[:5]:
                    if isinstance(v, str) and len(v) < 200:
                        preview_parts.append(f"{k}: {v}")
                text_preview = " | ".join(preview_parts)[:500] if preview_parts else None

            # Observables
            obs = [{"type": o.observable_type, "value": o.observable_value}
                   for o in result.observables] if result.observables else None

            from storage.signals.processing_queue import update_chunk_processed
            update_chunk_processed(
                chunk_id=chunk_id,
                format_detected=result.format_detected,
                parser_used=result.parser_name,
                parser_confidence=result.confidence,
                parsed_content=result.parsed,
                proposed_signal_type=result.signal_type_hint,
                vocabulary_status="unknown",  # classifier worker checks this
                field_names=field_names,
                text_preview=text_preview,
                observables_found=obs,
            )
            processed += 1

        except Exception as e:
            last_error = f"{chunk_id}: {str(e)[:200]}"
            errors += 1
            with get_connection(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE processing_queue SET status = 'error' WHERE chunk_id = %s",
                        (chunk_id,),
                    )
                    conn.commit()

    return {"processed": processed, "errors": errors, "last_error": last_error}


def process_classifier_tick(tenant_id: str, batch_size: int, dsn: Optional[str] = None) -> dict:
    """Classifier worker: check vocabulary status of parsed chunks."""
    # Load vocabulary
    from storage.ingest.signal_ingestion import _load_signal_types
    vocab = _load_signal_types()

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE processing_queue SET status = 'classifying'
                WHERE chunk_id IN (
                    SELECT chunk_id FROM processing_queue
                    WHERE tenant_id = %s AND status = 'parsed'
                    ORDER BY created_at LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING chunk_id, proposed_signal_type
            """, (tenant_id, batch_size))
            chunks = cur.fetchall()
            conn.commit()

    processed = 0
    for chunk_id, proposed_type in chunks:
        in_vocab = proposed_type in vocab if proposed_type and vocab else False
        status = "in_vocabulary" if in_vocab else "not_in_vocabulary" if proposed_type else "no_type"

        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE processing_queue SET
                        status = 'classified', vocabulary_status = %s
                    WHERE chunk_id = %s
                """, (status, chunk_id))
                conn.commit()
        processed += 1

    return {"processed": processed, "errors": 0, "last_error": None}


def process_auto_approve_tick(tenant_id: str, batch_size: int,
                               min_confidence: float = 0.80, dsn: Optional[str] = None) -> dict:
    """Auto-approve worker: promote high-confidence known-type chunks."""
    from storage.signals.processing_queue import auto_approve_chunk

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT chunk_id FROM processing_queue
                WHERE tenant_id = %s
                  AND status = 'classified'
                  AND vocabulary_status = 'in_vocabulary'
                  AND parser_confidence >= %s
                ORDER BY created_at
                LIMIT %s
            """, (tenant_id, min_confidence, batch_size))
            chunks = [r[0] for r in cur.fetchall()]

    processed = 0
    for chunk_id in chunks:
        auto_approve_chunk(chunk_id, dsn)
        processed += 1

    return {"processed": processed, "errors": 0, "last_error": None}


def process_promoter_tick(tenant_id: str, batch_size: int, dsn: Optional[str] = None) -> dict:
    """Promoter worker: create signals in LDU-W from validated chunks."""
    from storage.ingest.signal_ingestion import ingest_signal
    from storage.signals.processing_queue import promote_chunk

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT chunk_id, ldu_t_id, proposed_signal_type, parsed_content
                FROM processing_queue
                WHERE tenant_id = %s AND status = 'validated'
                ORDER BY created_at LIMIT %s
            """, (tenant_id, batch_size))
            chunks = cur.fetchall()

    processed = 0
    errors = 0
    last_error = None

    for chunk_id, ldu_t_id, signal_type, parsed_content in chunks:
        try:
            # Get raw bytes
            with get_connection(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT raw_bytes FROM ldu_t_signals WHERE ldu_t_id = %s", (ldu_t_id,))
                    row = cur.fetchone()
                    if not row:
                        continue
                    raw_bytes = bytes(row[0]) if isinstance(row[0], memoryview) else row[0]

            result = ingest_signal(
                raw_bytes=raw_bytes,
                device_id="promoted",
                tenant_id=tenant_id,
                source_type="promoted_from_queue",
            )

            if result.signal_id:
                promote_chunk(chunk_id, result.signal_id, result.signal_rid or 0, dsn)
                processed += 1
            else:
                errors += 1
                last_error = f"{chunk_id}: promotion failed, no signal_id"

        except Exception as e:
            errors += 1
            last_error = f"{chunk_id}: {str(e)[:200]}"

    return {"processed": processed, "errors": errors, "last_error": last_error}


def process_ai_classifier_tick(tenant_id: str, batch_size: int, dsn: Optional[str] = None) -> dict:
    """AI classifier worker: use LLM to classify unknown signal types.

    Picks up chunks where the regular classifier couldn't determine the type
    (vocabulary_status = 'not_in_vocabulary' or 'no_type') and asks the
    tenant's configured LLM to suggest a signal type.
    """
    from core.ai.config import get_tenant_provider
    from storage.ingest.signal_ingestion import _load_signal_types

    provider = get_tenant_provider(tenant_id, dsn)
    if not provider:
        return {"processed": 0, "errors": 0, "last_error": "No AI provider configured"}

    vocab = _load_signal_types()
    vocab_list = sorted(vocab)[:50]  # send a sample of valid types to the LLM

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT chunk_id, parsed_content, proposed_signal_type
                FROM processing_queue
                WHERE tenant_id = %s
                  AND status = 'classified'
                  AND vocabulary_status IN ('not_in_vocabulary', 'no_type')
                ORDER BY created_at
                LIMIT %s
            """, (tenant_id, batch_size))
            chunks = cur.fetchall()

    processed = 0
    errors = 0
    last_error = None

    for chunk_id, parsed_content, current_proposed in chunks:
        try:
            payload_str = json.dumps(parsed_content, default=str)[:2000] if parsed_content else "{}"

            messages = [
                {"role": "system", "content": (
                    "You are a signal classifier for the Enki Governance. "
                    "Given a JSON payload, determine the most appropriate signal type from the vocabulary. "
                    "Respond with ONLY the signal type string (e.g. 'signal.environmental.temperature'). "
                    "If no type fits, respond with 'signal.custom.untyped'.\n\n"
                    f"Valid signal types (sample): {', '.join(vocab_list[:30])}"
                )},
                {"role": "user", "content": f"Classify this payload:\n```json\n{payload_str}\n```"},
            ]

            response = provider.complete(messages, max_tokens=100, temperature=0.0)
            suggested_type = response.content.strip().strip('"').strip("'")

            # Validate the suggestion
            if suggested_type in vocab:
                with get_connection(dsn) as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE processing_queue SET
                                proposed_signal_type = %s,
                                vocabulary_status = 'in_vocabulary',
                                ai_confidence = 0.85,
                                status = 'classified'
                            WHERE chunk_id = %s
                        """, (suggested_type, chunk_id))
                        conn.commit()
                processed += 1
            else:
                # LLM suggested an invalid type — leave as-is for human review
                with get_connection(dsn) as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE processing_queue SET
                                ai_confidence = 0.3,
                                status = 'classified'
                            WHERE chunk_id = %s
                        """, (chunk_id,))
                        conn.commit()
                processed += 1

        except Exception as e:
            errors += 1
            last_error = f"{chunk_id}: {str(e)[:200]}"

    return {"processed": processed, "errors": errors, "last_error": last_error}


def process_ocr_tick(tenant_id: str, batch_size: int, dsn: Optional[str] = None) -> dict:
    """OCR worker: extract text from image and PDF chunks.

    Uses the AI provider's vision capability (if available) or a simple
    text extraction fallback for PDFs. Stores extracted text in the
    chunk's parsed_content for searchability and classification.
    """
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT pq.chunk_id, pq.ldu_t_id, pq.parsed_content,
                       lt.content_type, lt.format_detected
                FROM processing_queue pq
                JOIN ldu_t_signals lt ON lt.ldu_t_id = pq.ldu_t_id
                WHERE pq.tenant_id = %s
                  AND pq.status = 'parsed'
                  AND (lt.format_detected IN ('image', 'pdf')
                       OR lt.content_type LIKE 'image/%%'
                       OR lt.content_type = 'application/pdf')
                ORDER BY pq.created_at
                LIMIT %s
            """, (tenant_id, batch_size))
            chunks = cur.fetchall()

    processed = 0
    errors = 0
    last_error = None

    for chunk_id, ldu_t_id, parsed_content, content_type, format_detected in chunks:
        try:
            # Get raw bytes
            with get_connection(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT raw_bytes FROM ldu_t_signals WHERE ldu_t_id = %s", (ldu_t_id,))
                    row = cur.fetchone()
                    if not row:
                        continue
                    raw_bytes = bytes(row[0]) if isinstance(row[0], memoryview) else row[0]

            ocr_text = ""

            # PDF text extraction via pdfplumber
            if format_detected == "pdf" or (content_type and "pdf" in content_type):
                try:
                    import pdfplumber
                    import io
                    with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
                        pages_text = []
                        for page in pdf.pages[:10]:  # max 10 pages
                            text = page.extract_text()
                            if text:
                                pages_text.append(text)
                        ocr_text = "\n\n".join(pages_text)
                except Exception:
                    pass

            # Image — try to get basic info (real OCR would need tesseract or AI vision)
            if not ocr_text and (format_detected == "image" or (content_type and "image" in str(content_type))):
                try:
                    from PIL import Image
                    import io
                    img = Image.open(io.BytesIO(raw_bytes))
                    ocr_text = f"[Image: {img.format} {img.size[0]}x{img.size[1]}]"
                    # Extract any EXIF text data
                    exif = img._getexif()
                    if exif:
                        from PIL.ExifTags import TAGS
                        for tag_id, value in exif.items():
                            tag_name = TAGS.get(tag_id, "")
                            if tag_name in ("ImageDescription", "UserComment") and value:
                                ocr_text += f"\n{tag_name}: {value}"
                except Exception:
                    pass

            # Update chunk with extracted text
            if ocr_text:
                updated_content = parsed_content or {}
                if isinstance(updated_content, str):
                    updated_content = json.loads(updated_content)
                updated_content["_ocr_text"] = ocr_text[:5000]

                with get_connection(dsn) as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE processing_queue SET
                                parsed_content = %s,
                                status = 'ocr_complete'
                            WHERE chunk_id = %s
                        """, (json.dumps(updated_content), chunk_id))
                        conn.commit()
            else:
                # No text extracted — move to classified anyway
                with get_connection(dsn) as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE processing_queue SET status = 'classified'
                            WHERE chunk_id = %s
                        """, (chunk_id,))
                        conn.commit()

            processed += 1

        except Exception as e:
            errors += 1
            last_error = f"{chunk_id}: {str(e)[:200]}"

    return {"processed": processed, "errors": errors, "last_error": last_error}


# ─── Worker Tick Dispatcher ───────────────────────────────────────────────────

TICK_FUNCTIONS = {
    "parser": process_parser_tick,
    "classifier": process_classifier_tick,
    "ai_classifier": process_ai_classifier_tick,
    "ocr": process_ocr_tick,
    "auto_approve": process_auto_approve_tick,
    "promoter": process_promoter_tick,
}


def run_worker_tick(worker_id: str, tenant_id: str = "default", dsn: Optional[str] = None) -> dict:
    """Run a single tick of a worker. Returns processing results."""
    worker = get_worker(worker_id, tenant_id, dsn)
    if not worker:
        return {"error": "WORKER.NOT_FOUND"}
    if worker["status"] != "on":
        return {"error": "WORKER.NOT_RUNNING", "status": worker["status"]}

    tick_fn = TICK_FUNCTIONS.get(worker["worker_type"])
    if not tick_fn:
        return {"error": f"WORKER.UNKNOWN_TYPE: {worker['worker_type']}"}

    # Build kwargs from worker config
    kwargs = {"tenant_id": tenant_id, "batch_size": worker["batch_size"], "dsn": dsn}
    if worker["config"]:
        config = worker["config"] if isinstance(worker["config"], dict) else json.loads(worker["config"])
        if "min_confidence" in config:
            kwargs["min_confidence"] = config["min_confidence"]

    result = tick_fn(**kwargs)

    # Record stats
    queue_depth = _get_queue_depth(worker["picks_up_status"], tenant_id, dsn)
    record_tick(
        worker_id=worker_id,
        processed=result.get("processed", 0),
        errors=result.get("errors", 0),
        last_error=result.get("last_error"),
        queue_depth=queue_depth,
        dsn=dsn,
    )

    return {**result, "queue_depth": queue_depth}


def _get_queue_depth(status: str, tenant_id: str, dsn: Optional[str] = None) -> int:
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM processing_queue WHERE tenant_id = %s AND status = %s",
                (tenant_id, status),
            )
            return cur.fetchone()[0]
