"""
Archive Splitter — breaks large files into individual chunks for processing.

Supports:
  zip     → individual files
  tar/gz  → individual files
  mbox    → individual emails
  csv     → row batches (configurable batch size)
  jsonl   → individual JSON lines
  pdf     → individual pages (text extraction)
  folder  → recursive file walk

Each chunk is stored in LDU-T then enqueued in the processing queue.
"""

from __future__ import annotations

import io
import os
import json
import uuid
import zipfile
import tarfile
import mailbox
import tempfile
from typing import Optional, Generator
from dataclasses import dataclass

from storage.ingest.signal_ingestion import ingest_signal
from storage.signals.processing_queue import (
    create_import_job, enqueue_chunk, update_job_counts,
)


@dataclass
class ChunkItem:
    raw_bytes: bytes
    filename: str
    content_type: Optional[str] = None
    index: int = 0


def split_and_ingest(
    raw_bytes: bytes,
    filename: str,
    tenant_id: str,
    device_id: str = "archive-import",
    source_name: Optional[str] = None,
    created_by: str = "system",
    csv_batch_size: int = 100,
    dsn: Optional[str] = None,
) -> dict:
    """
    Split an archive into chunks, store each in LDU-T, and enqueue for processing.

    Returns import job summary.
    """
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""

    # Determine archive type
    splitter = _get_splitter(ext, raw_bytes)
    if not splitter:
        # Not an archive — treat as single file, enqueue directly
        return _ingest_single(raw_bytes, filename, tenant_id, device_id, dsn)

    # Create import job
    source_type = _ext_to_source_type(ext)
    job = create_import_job(
        tenant_id=tenant_id,
        source_type=source_type,
        source_name=source_name or filename,
        created_by=created_by,
        dsn=dsn,
    )
    job_id = job["job_id"]

    # Split and ingest each chunk
    chunk_count = 0
    errors = []

    for chunk in splitter(raw_bytes, filename, csv_batch_size):
        try:
            result = ingest_signal(
                raw_bytes=chunk.raw_bytes,
                device_id=device_id,
                tenant_id=tenant_id,
                content_type=chunk.content_type,
                source_type=f"archive:{ext}",
            )

            if result.ldu_t_id:
                enqueue_chunk(
                    tenant_id=tenant_id,
                    ldu_t_id=result.ldu_t_id,
                    job_id=job_id,
                    source_path=chunk.filename,
                    source_archive=filename,
                    chunk_index=chunk.index,
                    size_bytes=len(chunk.raw_bytes),
                    dsn=dsn,
                )
                chunk_count += 1
        except Exception as e:
            errors.append({"file": chunk.filename, "error": str(e)})

    # Update job counts
    update_job_counts(job_id, dsn)

    return {
        "job_id": job_id,
        "source_type": source_type,
        "source_name": source_name or filename,
        "chunks_created": chunk_count,
        "errors": errors[:20],  # Cap error list
        "status": "processing",
    }


def _ingest_single(raw_bytes, filename, tenant_id, device_id, dsn) -> dict:
    """Ingest a single file (not an archive) and enqueue it."""
    job = create_import_job(tenant_id, "single_file", filename, dsn=dsn)

    result = ingest_signal(
        raw_bytes=raw_bytes,
        device_id=device_id,
        tenant_id=tenant_id,
        source_type="file_upload",
    )

    if result.ldu_t_id:
        enqueue_chunk(
            tenant_id=tenant_id,
            ldu_t_id=result.ldu_t_id,
            job_id=job["job_id"],
            source_path=filename,
            size_bytes=len(raw_bytes),
            dsn=dsn,
        )

    update_job_counts(job["job_id"], dsn)

    return {
        "job_id": job["job_id"],
        "source_type": "single_file",
        "chunks_created": 1,
        "errors": [],
        "status": "processing",
    }


# ─── Splitter Registry ────────────────────────────────────────────────────────

def _get_splitter(ext: str, raw_bytes: bytes):
    """Get the right splitter function for a file extension."""
    splitters = {
        "zip": _split_zip,
        "tar": _split_tar,
        "gz": _split_tar,
        "tgz": _split_tar,
        "mbox": _split_mbox,
        "csv": _split_csv,
        "tsv": _split_csv,
        "jsonl": _split_jsonl,
        "ndjson": _split_jsonl,
    }

    if ext in splitters:
        return splitters[ext]

    # Check magic bytes for zip
    if raw_bytes[:4] == b'PK\x03\x04':
        return _split_zip

    # Check for gzip
    if raw_bytes[:2] == b'\x1f\x8b':
        return _split_tar

    return None


def _ext_to_source_type(ext: str) -> str:
    return {
        "zip": "zip_archive", "tar": "tar_archive", "gz": "tar_archive",
        "tgz": "tar_archive", "mbox": "email_archive",
        "csv": "csv_file", "tsv": "csv_file",
        "jsonl": "jsonl_file", "ndjson": "jsonl_file",
    }.get(ext, "archive")


# ─── Splitters ────────────────────────────────────────────────────────────────

def _split_zip(raw_bytes: bytes, filename: str, batch_size: int) -> Generator[ChunkItem, None, None]:
    """Extract files from a ZIP archive."""
    with zipfile.ZipFile(io.BytesIO(raw_bytes), "r") as zf:
        for i, info in enumerate(zf.infolist()):
            if info.is_dir():
                continue
            if info.file_size == 0:
                continue
            if info.file_size > 50_000_000:  # Skip files > 50MB
                continue

            data = zf.read(info.filename)
            ct = _guess_content_type(info.filename)
            yield ChunkItem(raw_bytes=data, filename=info.filename, content_type=ct, index=i)


def _split_tar(raw_bytes: bytes, filename: str, batch_size: int) -> Generator[ChunkItem, None, None]:
    """Extract files from a TAR/GZ archive."""
    mode = "r:gz" if raw_bytes[:2] == b'\x1f\x8b' else "r:"
    with tarfile.open(fileobj=io.BytesIO(raw_bytes), mode=mode) as tf:
        for i, member in enumerate(tf.getmembers()):
            if not member.isfile():
                continue
            if member.size == 0 or member.size > 50_000_000:
                continue

            f = tf.extractfile(member)
            if f is None:
                continue
            data = f.read()
            ct = _guess_content_type(member.name)
            yield ChunkItem(raw_bytes=data, filename=member.name, content_type=ct, index=i)


def _split_mbox(raw_bytes: bytes, filename: str, batch_size: int) -> Generator[ChunkItem, None, None]:
    """Extract individual emails from an mbox file."""
    # Write to temp file (mailbox module needs a file path)
    with tempfile.NamedTemporaryFile(suffix=".mbox", delete=False) as tmp:
        tmp.write(raw_bytes)
        tmp_path = tmp.name

    try:
        mbox = mailbox.mbox(tmp_path)
        for i, message in enumerate(mbox):
            msg_bytes = message.as_bytes()
            subject = message.get("Subject", f"email_{i}")
            yield ChunkItem(
                raw_bytes=msg_bytes,
                filename=f"{subject[:50]}.eml",
                content_type="message/rfc822",
                index=i,
            )
    finally:
        os.unlink(tmp_path)


def _split_csv(raw_bytes: bytes, filename: str, batch_size: int) -> Generator[ChunkItem, None, None]:
    """Split a CSV into row batches."""
    text = raw_bytes.decode("utf-8", errors="replace")
    lines = text.strip().split("\n")

    if len(lines) < 2:
        yield ChunkItem(raw_bytes=raw_bytes, filename=filename, content_type="text/csv", index=0)
        return

    header = lines[0]

    for batch_idx in range(0, len(lines) - 1, batch_size):
        batch_lines = lines[1 + batch_idx: 1 + batch_idx + batch_size]
        batch_text = header + "\n" + "\n".join(batch_lines)
        yield ChunkItem(
            raw_bytes=batch_text.encode("utf-8"),
            filename=f"{filename}_batch_{batch_idx // batch_size}",
            content_type="text/csv",
            index=batch_idx // batch_size,
        )


def _split_jsonl(raw_bytes: bytes, filename: str, batch_size: int) -> Generator[ChunkItem, None, None]:
    """Split a JSONL file into individual JSON lines."""
    text = raw_bytes.decode("utf-8", errors="replace")
    for i, line in enumerate(text.strip().split("\n")):
        line = line.strip()
        if not line:
            continue
        yield ChunkItem(
            raw_bytes=line.encode("utf-8"),
            filename=f"{filename}_line_{i}",
            content_type="application/json",
            index=i,
        )


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _guess_content_type(filename: str) -> Optional[str]:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    types = {
        "json": "application/json", "csv": "text/csv", "tsv": "text/csv",
        "xml": "application/xml", "html": "text/html",
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
        "pdf": "application/pdf", "doc": "application/msword",
        "txt": "text/plain", "log": "text/plain",
        "eml": "message/rfc822", "msg": "message/rfc822",
        "mp4": "video/mp4", "mp3": "audio/mpeg", "wav": "audio/wav",
    }
    return types.get(ext)
