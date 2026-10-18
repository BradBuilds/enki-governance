"""
Refdata API — operator-facing controls for the local bulk-cache packs.

GET    /refdata                       — list all packs with status (fresh/stale/missing)
GET    /refdata/{pack_id}             — pack detail + recent sync history
POST   /refdata/{pack_id}/sync        — trigger a sync (foreground for small, background optional)
POST   /refdata/{pack_id}/enable      — enable a disabled pack
POST   /refdata/{pack_id}/disable     — disable (source handlers skip local lookup)
GET    /refdata/{pack_id}/runs        — recent sync_run rows

Values written to the underlying tables come from
`core.refdata.manager` and the per-pack parsers. This route is the thin
HTTP surface; all real logic lives in the manager.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import JSONResponse

from core.refdata import manager as refdata_manager
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from storage.postgres.db import get_connection

logger = logging.getLogger("enki.api.refdata")

router = APIRouter(prefix="/refdata", tags=["refdata"])


@router.get("")
async def list_all(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """List every pack with current state. Drives the operator's refdata
    panel: green = fresh, yellow = stale, gray = never-synced."""
    packs = refdata_manager.list_packs()
    fresh = sum(1 for p in packs if p.get("fresh"))
    return {
        "packs": packs,
        "summary": {
            "total": len(packs),
            "fresh": fresh,
            "stale_or_missing": len(packs) - fresh,
        },
    }


@router.get("/{pack_id}")
async def get_pack(pack_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Full detail on a single pack including manifest entry + DB state."""
    pack_def = refdata_manager.get_pack_def(pack_id)
    if not pack_def:
        raise HTTPException(404, detail=f"Unknown pack_id: {pack_id}")
    record = refdata_manager.get_pack_record(pack_id)
    return {
        "manifest": pack_def,
        "state": record,
        "is_fresh": refdata_manager.is_fresh(pack_id),
    }


@router.get("/{pack_id}/runs")
async def get_runs(pack_id: str, limit: int = 20,
                   auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Recent sync_run history — both successful and failed. Useful for
    diagnosing 'why is my pack still stale?'."""
    pack_def = refdata_manager.get_pack_def(pack_id)
    if not pack_def:
        raise HTTPException(404, detail=f"Unknown pack_id: {pack_id}")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT run_id, started_at, completed_at, status,
                       records_indexed, bytes_downloaded, duration_ms, error
                FROM refdata_sync_runs
                WHERE pack_id = %s
                ORDER BY started_at DESC
                LIMIT %s
                """,
                (pack_id, limit),
            )
            rows = cur.fetchall()
    runs = [
        {
            "run_id": r[0],
            "started_at": r[1].isoformat() if r[1] else None,
            "completed_at": r[2].isoformat() if r[2] else None,
            "status": r[3],
            "records_indexed": r[4],
            "bytes_downloaded": r[5],
            "duration_ms": r[6],
            "error": r[7],
        }
        for r in rows
    ]
    return {"pack_id": pack_id, "runs": runs}


def _do_sync(pack_id: str):
    """Wrapper for BackgroundTasks — surfaces any unexpected exception in logs."""
    try:
        result = refdata_manager.sync(pack_id)
        logger.info("refdata sync (background) pack=%s result=%s", pack_id, result.get("ok"))
    except Exception:
        logger.exception("refdata sync (background) crashed pack=%s", pack_id)


@router.post("/{pack_id}/sync")
async def sync_pack(
    pack_id: str,
    background_tasks: BackgroundTasks,
    background: bool = True,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Trigger a sync. By default the heavy lifting runs in the background
    so the operator's HTTP request returns quickly; pass `background=false`
    to wait for completion (useful for small packs / testing)."""
    pack_def = refdata_manager.get_pack_def(pack_id)
    if not pack_def:
        raise HTTPException(404, detail=f"Unknown pack_id: {pack_id}")

    if background:
        background_tasks.add_task(_do_sync, pack_id)
        return {"queued": True, "pack_id": pack_id, "background": True}

    # Foreground: actually wait for the sync. asyncio.to_thread keeps the
    # event loop responsive while the parser does its work.
    result = await asyncio.to_thread(refdata_manager.sync, pack_id)
    if not result.get("ok"):
        return JSONResponse(status_code=500, content=result)
    return result


@router.post("/{pack_id}/enable")
async def enable_pack(pack_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    pack_def = refdata_manager.get_pack_def(pack_id)
    if not pack_def:
        raise HTTPException(404, detail=f"Unknown pack_id: {pack_id}")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE refdata_packs SET enabled = TRUE, updated_at = NOW() "
                "WHERE pack_id = %s",
                (pack_id,),
            )
        conn.commit()
    return {"pack_id": pack_id, "enabled": True}


@router.post("/{pack_id}/disable")
async def disable_pack(pack_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    pack_def = refdata_manager.get_pack_def(pack_id)
    if not pack_def:
        raise HTTPException(404, detail=f"Unknown pack_id: {pack_id}")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE refdata_packs SET enabled = FALSE, updated_at = NOW() "
                "WHERE pack_id = %s",
                (pack_id,),
            )
        conn.commit()
    return {"pack_id": pack_id, "enabled": False}
