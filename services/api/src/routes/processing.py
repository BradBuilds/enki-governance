"""
Processing Queue & Import Jobs API.

Bulk Import:
  POST /import/upload          — upload archive (zip/tar/mbox/csv) for processing
  GET  /import/jobs            — list import jobs
  GET  /import/jobs/{job_id}   — get job progress

Processing Queue:
  GET  /processing/queue       — list queue items (filtered by status, type, job)
  GET  /processing/queue/summary — counts by status + pending groups
  GET  /processing/queue/{chunk_id} — get chunk details
  POST /processing/validate    — approve/reject/edit a chunk
  POST /processing/validate-group — approve/reject all in a group
"""

from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from pydantic import BaseModel

from storage.signals.processing_queue import (
    list_queue, get_chunk, get_queue_summary,
    validate_chunk, validate_group,
    list_import_jobs, get_import_job,
)
from storage.ingest.archive_splitter import split_and_ingest

router = APIRouter(tags=["processing"])


# ─── Models ──────────────────────────────────────────────────────────────────

class ValidateRequest(BaseModel):
    chunk_id: str
    action: str  # approve, reject, edit
    signal_type_override: Optional[str] = None
    note: Optional[str] = None

class ValidateGroupRequest(BaseModel):
    group: str
    action: str  # approve, reject
    signal_type_override: Optional[str] = None
    note: Optional[str] = None


# ─── Import ──────────────────────────────────────────────────────────────────

@router.post("/import/upload")
async def upload_archive(
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
    x_device_id: str = Header("archive-import", alias="X-Device-ID"),
):
    """Upload an archive for bulk import. Splits into chunks and enqueues.

    Supports: zip, tar, tar.gz, mbox, csv, jsonl.
    Max: 200MB per upload.
    """
    body = await request.body()

    if len(body) > 200_000_000:
        return JSONResponse(status_code=413, content={
            "error": "IMPORT.TOO_LARGE",
            "detail": "Archive exceeds 200MB limit.",
            "max_bytes": 200_000_000,
        })

    # Get filename from content-disposition or X-Filename header
    filename = request.headers.get("x-filename", "upload.bin")

    result = split_and_ingest(
        raw_bytes=body,
        filename=filename,
        tenant_id=auth.tenant_id,
        device_id=x_device_id,
        source_name=filename,
    )

    return JSONResponse(status_code=201, content=result)


@router.get("/import/jobs")
async def list_jobs(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """List all import jobs."""
    jobs = list_import_jobs(auth.tenant_id)
    return {"jobs": jobs, "total": len(jobs)}


@router.get("/import/jobs/{job_id}")
async def get_job(job_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Get import job progress."""
    job = get_import_job(job_id, auth.tenant_id)
    if not job:
        return JSONResponse(status_code=404, content={"error": "JOB.NOT_FOUND"})
    return job


# ─── Processing Queue ────────────────────────────────────────────────────────

@router.get("/processing/queue")
async def list_processing_queue(
    status: Optional[str] = None,
    job_id: Optional[str] = None,
    proposed_type: Optional[str] = None,
    limit: int = 100,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """List items in the processing queue."""
    items = list_queue(auth.tenant_id, status=status, job_id=job_id,
                       proposed_type=proposed_type, limit=min(limit, 1000))
    return {"items": items, "total": len(items)}


@router.get("/processing/queue/summary")
async def queue_summary(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Get processing queue status counts and pending groups."""
    return get_queue_summary(auth.tenant_id)


@router.get("/processing/queue/{chunk_id}")
async def get_queue_item(chunk_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Get full details of a processing queue chunk."""
    chunk = get_chunk(chunk_id, auth.tenant_id)
    if not chunk:
        return JSONResponse(status_code=404, content={"error": "CHUNK.NOT_FOUND"})
    return chunk


@router.post("/processing/validate")
async def validate_item(
    body: ValidateRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Validate a single chunk: approve, reject, or edit."""
    result = validate_chunk(
        chunk_id=body.chunk_id,
        validated_by=auth.subject_id,
        action=body.action,
        signal_type_override=body.signal_type_override,
        note=body.note,
    )
    if "error" in result:
        return JSONResponse(status_code=400, content=result)
    return result


@router.post("/processing/validate-group")
async def validate_group_endpoint(
    body: ValidateGroupRequest,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Bulk validate all chunks in a group."""
    result = validate_group(
        proposed_group=body.group,
        tenant_id=auth.tenant_id,
        validated_by=auth.subject_id,
        action=body.action,
        signal_type_override=body.signal_type_override,
        note=body.note,
    )
    return result
