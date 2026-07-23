"""
Signal ingestion and query API endpoints.

POST /signals/ingest     — Ingest raw signal (any format)
POST /signals/ingest/batch — Ingest batch of signals
GET  /signals            — Query signals (filtered, paginated)
GET  /signals/{signal_id} — Get single signal by ID
GET  /signals/ldu-p      — List pending LDU-P entries
POST /signals/ldu-p/{id}/promote — Promote LDU-P to LDU-W signal
PATCH /signals/ldu-p/{id}/reject — Reject LDU-P entry
GET  /signals/chain      — Signal chain verification
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Header, Request, Query, UploadFile
from fastapi.responses import JSONResponse

from storage.ingest.signal_ingestion import ingest_signal, ingest_signal_batch, promote_ldu_p, reject_ldu_p
from storage.signals.store import get_signal, query_signals, list_pending_ldu_p, get_ldu_p, update_ldu_p
from storage.signals.chain import get_signal_chain_head
from services.api.src.unified_auth import unified_auth, optional_unified_auth, UnifiedAuthInfo

router = APIRouter(prefix="/signals", tags=["signals"])


@router.post("/ingest")
async def ingest(
    request: Request,
    x_device_id: str = Header("unknown", alias="X-Device-ID"),
    x_tenant_id: str = Header("default", alias="X-Tenant-ID"),
):
    """
    Ingest a single signal. Accepts ANY content type.

    The pipeline auto-detects format, classifies, parses, and routes
    to LDU-W (if type known) or LDU-P (if type unknown).

    Raw bytes are ALWAYS stored in LDU-T regardless of classification.
    """
    body = await request.body()

    if len(body) > 10_000_000:  # 10MB limit per signal
        return JSONResponse(
            status_code=413,
            content={
                "error": "SIGNAL.PAYLOAD_TOO_LARGE",
                "detail": "Signal payload exceeds 10MB limit. Use chunked upload for large files.",
                "max_bytes": 10_000_000,
            },
        )

    content_type = request.headers.get("content-type")
    source_type = request.headers.get("x-source-type", "unknown")

    result = ingest_signal(
        raw_bytes=body,
        device_id=x_device_id,
        tenant_id=x_tenant_id,
        content_type=content_type,
        source_type=source_type,
    )

    status_code = 201 if result.success else 500

    return JSONResponse(
        status_code=status_code,
        content={
            "status": "accepted" if result.path in ("ldu_w", "duplicate") else
                      "provisioned" if result.path == "ldu_p" else "stored",
            "path": result.path,
            "ldu_t_id": result.ldu_t_id,
            "signal_id": result.signal_id,
            "ldu_p_id": result.ldu_p_id,
            "signal_type": result.signal_type,
            "format_detected": result.format_detected,
            "confidence": result.confidence,
            "observables_count": result.observables_count,
            "error": result.error,
        },
    )


@router.post("/ingest/batch")
async def ingest_batch(
    request: Request,
    x_device_id: str = Header("unknown", alias="X-Device-ID"),
    x_tenant_id: str = Header("default", alias="X-Tenant-ID"),
):
    """
    Ingest a batch of signals (newline-delimited JSON).

    Each line is a JSON object with a "raw" field containing
    base64-encoded payload, or the line itself is the payload.

    Commits the batch to the signal chain after processing.
    """
    body = await request.body()
    lines = body.decode("utf-8", errors="replace").strip().split("\n")

    items = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        items.append({
            "raw_bytes": line.encode("utf-8"),
            "device_id": x_device_id,
            "content_type": "application/json",
            "source_type": request.headers.get("x-source-type", "unknown"),
        })

    if not items:
        return JSONResponse(
            status_code=400,
            content={"error": "SIGNAL.EMPTY_BATCH", "detail": "No signals in batch"},
        )

    result = ingest_signal_batch(items, x_tenant_id)

    return JSONResponse(
        status_code=201,
        content={
            "status": "batch_accepted",
            "total": result.total,
            "accepted": result.accepted,
            "provisioned": result.provisioned,
            "stored_raw": result.stored_raw,
            "chain_hash": result.chain_hash,
            "receipt_rid": result.receipt_rid,
            "results": [
                {
                    "ldu_t_id": r.ldu_t_id,
                    "signal_id": r.signal_id,
                    "ldu_p_id": r.ldu_p_id,
                    "path": r.path,
                    "signal_type": r.signal_type,
                    "format_detected": r.format_detected,
                }
                for r in result.results
            ],
        },
    )


@router.post("/upload/statement")
async def upload_bank_statement(
    file: UploadFile = File(...),
    x_device_id: str = Header("bank-upload", alias="X-Device-ID"),
    x_tenant_id: str = Header("default", alias="X-Tenant-ID"),
    account_label: Optional[str] = Query(None),
):
    """
    Upload a bank statement file (CSV, OFX/QBO, PDF).

    The bank statement parser extracts individual transactions and ingests
    each one as a signal.financial.transaction. Supports drag-and-drop
    from the UI.

    Returns summary of ingested transactions including totals.
    """
    raw_bytes = await file.read()

    if len(raw_bytes) > 50_000_000:  # 50MB limit for statements
        return JSONResponse(
            status_code=413,
            content={
                "error": "STATEMENT.FILE_TOO_LARGE",
                "detail": "Statement file exceeds 50MB limit.",
                "max_bytes": 50_000_000,
            },
        )

    if not raw_bytes:
        return JSONResponse(
            status_code=400,
            content={"error": "STATEMENT.EMPTY_FILE", "detail": "Uploaded file is empty"},
        )

    # Determine content type from file extension
    filename = file.filename or ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    content_type_map = {
        "csv": "text/csv",
        "ofx": "application/x-ofx",
        "qbo": "application/x-ofx",
        "qfx": "application/x-ofx",
        "pdf": "application/pdf",
    }
    content_type = content_type_map.get(ext, file.content_type or "application/octet-stream")

    # Ingest the full statement as a single signal — the parser handles
    # extracting individual transactions into the parsed payload
    result = ingest_signal(
        raw_bytes=raw_bytes,
        device_id=x_device_id,
        tenant_id=x_tenant_id,
        content_type=content_type,
        source_type="bank_statement",
    )

    # Auto-create/update financial account from statement
    account = None
    if result.success and account_label:
        try:
            from storage.financial.accounts import upsert_account, record_balance
            from storage.signals.store import get_signal

            # Determine account type from label hints
            label_lower = (account_label or "").lower()
            if any(kw in label_lower for kw in ["visa", "mastercard", "credit", "amex"]):
                acct_type = "credit_card"
            elif any(kw in label_lower for kw in ["loan", "auto loan", "student"]):
                acct_type = "loan"
            elif any(kw in label_lower for kw in ["mortgage", "home loan"]):
                acct_type = "mortgage"
            elif "saving" in label_lower:
                acct_type = "savings"
            elif "invest" in label_lower or "brokerage" in label_lower:
                acct_type = "investment"
            else:
                acct_type = "checking"

            # Get the parsed signal to extract account info and balance
            sig = get_signal(result.signal_id, x_tenant_id)
            payload = sig.get("payload", {}) if sig else {}
            acct_info = payload.get("account", {})
            transactions = payload.get("transactions", [])

            # Find latest balance from transactions
            latest_balance = None
            if transactions:
                last_txn = transactions[-1]
                latest_balance = last_txn.get("balance")

            account = upsert_account(
                tenant_id=x_tenant_id,
                name=account_label,
                account_type=acct_type,
                institution=acct_info.get("institution"),
                account_number_masked=acct_info.get("account_number"),
                routing_number=acct_info.get("routing_number"),
                current_balance=latest_balance,
                device_id=x_device_id,
            )

            # Record balance snapshot
            if latest_balance is not None:
                from datetime import date
                record_balance(
                    account_id=account["account_id"],
                    tenant_id=x_tenant_id,
                    balance=latest_balance,
                    balance_date=date.today(),
                    source="statement",
                    signal_id=result.signal_id,
                )
        except Exception as e:
            import logging
            logging.getLogger("enki.api").warning(f"Account creation failed: {e}")

    # Build response with financial summary
    response = {
        "status": "accepted" if result.success else "error",
        "path": result.path,
        "ldu_t_id": result.ldu_t_id,
        "signal_id": result.signal_id,
        "signal_type": result.signal_type,
        "format_detected": result.format_detected,
        "confidence": result.confidence,
        "observables_count": result.observables_count,
        "filename": filename,
    }

    if account:
        response["account"] = account
    if account_label:
        response["account_label"] = account_label

    if result.error:
        response["error"] = result.error

    return JSONResponse(
        status_code=201 if result.success else 500,
        content=response,
    )


@router.get("")
async def list_signals(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    signal_type: Optional[str] = Query(None),
    device_id: Optional[str] = Query(None),
    after: Optional[str] = Query(None),
    before: Optional[str] = Query(None),
    limit: int = Query(100, le=1000),
):
    """
    Query signals with filters. Always scoped to tenant.

    Pagination via limit (max 1000). No bulk export.
    """
    tenant_id = auth.tenant_id

    after_dt = None
    before_dt = None
    if after:
        try:
            after_dt = datetime.fromisoformat(after)
        except ValueError:
            return JSONResponse(status_code=400, content={
                "error": "SIGNAL.INVALID_DATE", "detail": f"Invalid 'after' date: {after}"
            })
    if before:
        try:
            before_dt = datetime.fromisoformat(before)
        except ValueError:
            return JSONResponse(status_code=400, content={
                "error": "SIGNAL.INVALID_DATE", "detail": f"Invalid 'before' date: {before}"
            })

    results = query_signals(
        tenant_id=tenant_id,
        signal_type=signal_type,
        device_id=device_id,
        after=after_dt,
        before=before_dt,
        limit=limit,
    )

    return {"signals": results, "count": len(results), "limit": limit}


@router.get("/ldu-p")
async def list_ldu_p_entries(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    limit: int = Query(100, le=1000),
):
    """List pending LDU-P entries (playground) for review."""
    entries = list_pending_ldu_p(tenant_id=auth.tenant_id, limit=limit)
    return {"entries": entries, "count": len(entries)}


@router.get("/ldu-p/{ldu_p_id}")
async def get_ldu_p_entry(
    ldu_p_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Get a single LDU-P entry by ID."""
    entry = get_ldu_p(ldu_p_id, auth.tenant_id)
    if not entry:
        return JSONResponse(status_code=404, content={
            "error": "SIGNAL.LDU_P_NOT_FOUND", "detail": f"LDU-P entry {ldu_p_id} not found"
        })
    return entry


@router.patch("/ldu-p/{ldu_p_id}")
async def edit_ldu_p_entry(
    ldu_p_id: str,
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Edit a pending LDU-P entry before promotion.

    Body (all fields optional):
    {
      "proposed_signal_type": "signal.device.sensor_reading",
      "extracted_structure": { ... },
      "proposed_mapping":    { "field_map": {...} }
    }
    """
    body = await request.json()
    ok = update_ldu_p(
        ldu_p_id,
        auth.tenant_id,
        proposed_signal_type=body.get("proposed_signal_type"),
        extracted_structure=body.get("extracted_structure"),
        proposed_mapping=body.get("proposed_mapping"),
    )
    if not ok:
        return JSONResponse(status_code=404, content={
            "error": "SIGNAL.LDU_P_NOT_FOUND",
            "detail": f"LDU-P entry {ldu_p_id} not found or already resolved",
        })
    return {"updated": True, "ldu_p_id": ldu_p_id}


@router.post("/ldu-p/{ldu_p_id}/promote")
async def promote_ldu_p_entry(
    ldu_p_id: str,
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Promote a provisional LDU-P entry to a full LDU-W signal.

    Body: {
      "signal_type": "signal.device.sensor_reading",
      "resolved_by": "operator",
      "write_mapping": true,           // optional — materialize YAML
      "mapping_overwrite": false,      // optional — replace existing YAML
      "mapping_node_type": "event",    // optional
      "mapping_subtype":   "reading"   // optional
    }
    """
    body = await request.json()
    signal_type = body.get("signal_type", "").strip()
    if not signal_type:
        return JSONResponse(status_code=400, content={
            "error": "SIGNAL.MISSING_TYPE", "detail": "signal_type is required",
        })

    resolved_by = body.get("resolved_by", "operator")

    # Optionally materialize a declarative worker YAML BEFORE promotion,
    # so the next signal of this type hits the registered worker rather
    # than re-entering LDU-P limbo. Fetch the entry first for its
    # structure + proposed_mapping.
    mapping_info: Optional[dict] = None
    if body.get("write_mapping"):
        entry = get_ldu_p(ldu_p_id, auth.tenant_id)
        if entry and entry.get("resolution") == "pending_review":
            from core.extraction.workers.mapping_writer import write_mapping_yaml
            mapping_info = write_mapping_yaml(
                signal_type=signal_type,
                extracted_structure=entry.get("extracted_structure") or {},
                proposed_mapping=entry.get("proposed_mapping") or {},
                author=f"promotion:{resolved_by}",
                node_type=body.get("mapping_node_type", "event"),
                subtype=body.get("mapping_subtype"),
                overwrite=bool(body.get("mapping_overwrite", False)),
            )

    result = promote_ldu_p(ldu_p_id, auth.tenant_id, signal_type, resolved_by)

    if not result.success:
        return JSONResponse(status_code=400, content={
            "error": "SIGNAL.PROMOTION_FAILED", "detail": result.error,
            "mapping": mapping_info,
        })

    return {
        "promoted": True,
        "signal_id": result.signal_id,
        "signal_rid": result.signal_rid,
        "signal_type": result.signal_type,
        "ldu_p_id": ldu_p_id,
        "ldu_t_id": result.ldu_t_id,
        "path": result.path,
        "mapping": mapping_info,
    }


@router.patch("/ldu-p/{ldu_p_id}/reject")
async def reject_ldu_p_entry(
    ldu_p_id: str,
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Reject a provisional LDU-P entry.

    Body: { "reason": "duplicate data", "resolved_by": "operator" }
    """
    body = await request.json()
    reason = body.get("reason", "")
    resolved_by = body.get("resolved_by", "operator")

    ok = reject_ldu_p(ldu_p_id, auth.tenant_id, reason, resolved_by)
    if not ok:
        return JSONResponse(status_code=404, content={
            "error": "SIGNAL.LDU_P_NOT_FOUND",
            "detail": f"LDU-P entry {ldu_p_id} not found or already resolved",
        })

    return {"rejected": True, "ldu_p_id": ldu_p_id, "reason": reason}


@router.get("/chain")
async def signal_chain_status(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Get signal chain head and signal count for this tenant."""
    from storage.node.identity import load_node_id
    try:
        node_id = load_node_id()
    except EnvironmentError:
        node_id = "local"

    chain_hash, signal_count = get_signal_chain_head(auth.tenant_id, node_id)

    return {
        "tenant_id": auth.tenant_id,
        "node_id": node_id,
        "chain_hash": chain_hash,
        "signal_count": signal_count,
    }


@router.get("/{signal_id}")
async def get_signal_by_id(
    signal_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Get a single signal by ID."""
    signal = get_signal(signal_id, auth.tenant_id)
    if not signal:
        return JSONResponse(status_code=404, content={
            "error": "SIGNAL.NOT_FOUND", "detail": f"Signal {signal_id} not found"
        })
    return signal
