"""
Data export endpoints — streaming CSV, JSON, JSONL.

GET /exports/signals  — Export signals with filters
GET /exports/cases    — Export cases with filters
GET /exports/audit    — Export audit trail

All exports are tenant-scoped and paginated via cursor for resumability.
Hard cap: 100,000 rows per request.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import StreamingResponse

from storage.postgres.db import get_connection
from services.api.src.unified_auth import UnifiedAuthInfo, require_role

router = APIRouter(prefix="/exports", tags=["exports"])

MAX_EXPORT_ROWS = 100_000


def _stream_csv(columns: list[str], rows):
    """Yield CSV lines from an iterable of tuples."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    yield buf.getvalue()
    buf.seek(0)
    buf.truncate(0)

    for row in rows:
        writer.writerow(row)
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)


def _stream_jsonl(columns: list[str], rows):
    """Yield JSONL lines from an iterable of tuples."""
    for row in rows:
        obj = {}
        for i, col in enumerate(columns):
            val = row[i]
            if isinstance(val, datetime):
                val = val.isoformat()
            obj[col] = val
        yield json.dumps(obj) + "\n"


def _stream_json(columns: list[str], rows):
    """Yield a JSON array from an iterable of tuples."""
    yield "[\n"
    first = True
    for row in rows:
        obj = {}
        for i, col in enumerate(columns):
            val = row[i]
            if isinstance(val, datetime):
                val = val.isoformat()
            obj[col] = val
        prefix = "" if first else ","
        yield prefix + json.dumps(obj) + "\n"
        first = False
    yield "]\n"


def _format_response(fmt: str, columns: list[str], rows, filename_base: str):
    """Build StreamingResponse for the requested format."""
    if fmt == "csv":
        return StreamingResponse(
            _stream_csv(columns, rows),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename_base}.csv"},
        )
    elif fmt == "jsonl":
        return StreamingResponse(
            _stream_jsonl(columns, rows),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f"attachment; filename={filename_base}.jsonl"},
        )
    else:
        return StreamingResponse(
            _stream_json(columns, rows),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={filename_base}.json"},
        )


@router.get("/signals")
async def export_signals(
    auth: UnifiedAuthInfo = Depends(require_role("admin")),
    signal_type: Optional[str] = Query(None),
    device_id: Optional[str] = Query(None),
    after: Optional[str] = Query(None),
    before: Optional[str] = Query(None),
    format: str = Query("csv", regex="^(csv|json|jsonl)$"),
    cursor: Optional[int] = Query(None, description="Resume from rid"),
    limit: int = Query(MAX_EXPORT_ROWS, le=MAX_EXPORT_ROWS),
):
    """Export signals as CSV, JSON, or JSONL. Streaming, tenant-scoped."""
    tenant_id = auth.tenant_id

    conditions = ["tenant_id = %s"]
    params: list = [tenant_id]

    if signal_type:
        conditions.append("signal_type = %s")
        params.append(signal_type)
    if device_id:
        conditions.append("device_id = %s")
        params.append(device_id)
    if after:
        conditions.append("timestamp >= %s")
        params.append(after)
    if before:
        conditions.append("timestamp <= %s")
        params.append(before)
    if cursor is not None:
        conditions.append("rid > %s")
        params.append(cursor)

    params.append(min(limit, MAX_EXPORT_ROWS))
    where = " AND ".join(conditions)

    columns = [
        "rid", "signal_id", "signal_type", "source_domain", "severity",
        "timestamp", "device_id", "identity_id", "latitude", "longitude", "cid",
    ]

    def row_generator():
        with get_connection() as conn:
            with conn.cursor(name="export_signals") as cur:
                cur.itersize = 500
                cur.execute(f"""
                    SELECT {', '.join(columns)}
                    FROM signals
                    WHERE {where}
                    ORDER BY rid ASC
                    LIMIT %s
                """, params)
                yield from cur

    return _format_response(format, columns, row_generator(), "enki_signals")


@router.get("/cases")
async def export_cases(
    auth: UnifiedAuthInfo = Depends(require_role("admin")),
    status: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    format: str = Query("csv", regex="^(csv|json|jsonl)$"),
    limit: int = Query(MAX_EXPORT_ROWS, le=MAX_EXPORT_ROWS),
):
    """Export cases as CSV, JSON, or JSONL."""
    tenant_id = auth.tenant_id

    conditions = ["tenant_id = %s"]
    params: list = [tenant_id]

    if status:
        conditions.append("status = %s")
        params.append(status)
    if priority:
        conditions.append("priority = %s")
        params.append(priority)

    params.append(min(limit, MAX_EXPORT_ROWS))
    where = " AND ".join(conditions)

    columns = [
        "rid", "case_id", "title", "status", "priority",
        "description", "owner", "created_by", "created_at", "updated_at",
    ]

    def row_generator():
        with get_connection() as conn:
            with conn.cursor(name="export_cases") as cur:
                cur.itersize = 500
                cur.execute(f"""
                    SELECT {', '.join(columns)}
                    FROM cases
                    WHERE {where}
                    ORDER BY rid ASC
                    LIMIT %s
                """, params)
                yield from cur

    return _format_response(format, columns, row_generator(), "enki_cases")


@router.get("/audit")
async def export_audit(
    auth: UnifiedAuthInfo = Depends(require_role("admin")),
    format: str = Query("jsonl", regex="^(csv|json|jsonl)$"),
    cursor: Optional[int] = Query(None, description="Resume from rid"),
    limit: int = Query(MAX_EXPORT_ROWS, le=MAX_EXPORT_ROWS),
):
    """Export audit trail as JSONL (default), CSV, or JSON."""
    tenant_id = auth.tenant_id

    conditions = ["tenant_id = %s"]
    params: list = [tenant_id]

    if cursor is not None:
        conditions.append("rid > %s")
        params.append(cursor)

    params.append(min(limit, MAX_EXPORT_ROWS))
    where = " AND ".join(conditions)

    columns = [
        "rid", "cid", "event_type", "payload_json", "timestamp",
        "node_id", "commit_time",
    ]

    def row_generator():
        with get_connection() as conn:
            with conn.cursor(name="export_audit") as cur:
                cur.itersize = 500
                cur.execute(f"""
                    SELECT {', '.join(columns)}
                    FROM audit_events
                    WHERE {where}
                    ORDER BY rid ASC
                    LIMIT %s
                """, params)
                yield from cur

    return _format_response(format, columns, row_generator(), "enki_audit")
