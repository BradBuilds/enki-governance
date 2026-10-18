"""
Governance observability endpoints.

Views into the GDU/spine state and entity governance conflicts.

Observability endpoints (state, activations, diff, verify) are public read-only.
Entity governance endpoints (conflicts, ldu-p, chain) require authentication
since they access tenant-scoped data and allow data mutations.

Routes:
  GET  /governance/state           — current active set + canonicality (public)
  GET  /governance/activations     — activation records on disk (public)
  GET  /governance/activations/{id} — full activation record (public)
  GET  /governance/diff             — compare two active sets (public)
  GET  /governance/verify           — re-run disk verifier (public)
  GET  /governance/conflicts        — entity conflicts (auth required)
  POST /governance/conflicts/{id}/resolve — resolve conflict (auth required)
  GET  /governance/ldu-p            — provisional entities (auth required)
  GET  /governance/chain/state      — entity chain hash (auth required)
  GET  /governance/chain/recent-events — recent mutations (auth required)
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

router = APIRouter(prefix="/governance", tags=["governance"])
log = logging.getLogger("enki.governance")

PROJECT_ROOT = Path(__file__).resolve().parents[4]
ADMISSION_DIR = PROJECT_ROOT / "storage" / "admission"


@router.get("/state")
def state() -> dict:
    """Current GDU state — richer than /health.governance.

    Returns the same canonicality flag plus the full verification metadata,
    the active set version, and the loaded pack count.
    """
    try:
        from core.gdu.signal_vocabulary import is_canonical, get_verification, get_signal_types
        from core.gdu.config import ACTIVE_SET_PATH

        verification = get_verification()
        types = get_signal_types()

        return {
            "canonical": is_canonical(),
            "active_set_path": str(ACTIVE_SET_PATH.relative_to(PROJECT_ROOT)),
            "active_set_version": verification.get("active_set_version"),
            "method": verification.get("method"),
            "pack_hash_verified": verification.get("pack_hash_verified", False),
            "signal_types_count": len(types),
            "verification": verification,
        }
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={"canonical": False, "error": str(e)},
        )


@router.get("/activations")
def list_activations() -> dict:
    """List all activation records on disk.

    Returns a summary of each activation in storage/admission/<id>/.
    Sorted newest-first by activation timestamp.
    """
    if not ADMISSION_DIR.exists():
        return {"activations": [], "count": 0, "admission_dir": str(ADMISSION_DIR)}

    records = []
    for sub in sorted(ADMISSION_DIR.iterdir(), reverse=True):
        if not sub.is_dir():
            continue
        activation_path = sub / "activation_record.json"
        results_path = sub / "ceremony_results.json"
        if not activation_path.exists():
            continue
        try:
            with open(activation_path) as f:
                rec = json.load(f)
            # Activation records nest the data under "activation" — unwrap if so
            data = rec.get("activation", rec)
            summary = {
                "id": sub.name,
                "activation_id": data.get("activation_id"),
                "active_set_version": data.get("active_set_version"),
                "activated_by": data.get("activated_by") or data.get("operator"),
                "activated_at": data.get("activated_at") or data.get("timestamp"),
                "transcript_hash": data.get("transcript_hash"),
            }
            if results_path.exists():
                try:
                    with open(results_path) as rf:
                        results = json.load(rf)
                    summary["success"] = results.get("success")
                except Exception:
                    pass
            records.append(summary)
        except Exception as e:
            records.append({"id": sub.name, "error": str(e)})

    return {
        "activations": records,
        "count": len(records),
        "admission_dir": str(ADMISSION_DIR.relative_to(PROJECT_ROOT)),
    }


@router.get("/activations/{activation_id}")
def get_activation(activation_id: str) -> dict:
    """Return the full activation record + validation transcript for one activation.

    activation_id is the directory name under storage/admission/ (e.g. 'v1').
    """
    # Sanitize: only allow safe directory names, no path traversal
    if "/" in activation_id or ".." in activation_id or activation_id.startswith("."):
        raise HTTPException(status_code=400, detail="invalid activation id")

    sub = ADMISSION_DIR / activation_id
    if not sub.exists() or not sub.is_dir():
        raise HTTPException(status_code=404, detail=f"activation {activation_id!r} not found")

    result = {"id": activation_id}
    for filename in ("activation_record.json", "validation_transcript.json", "ceremony_results.json"):
        path = sub / filename
        if path.exists():
            try:
                with open(path) as f:
                    result[filename.replace(".json", "")] = json.load(f)
            except Exception as e:
                result[filename.replace(".json", "")] = {"error": str(e)}
    return result


@router.get("/diff")
def diff_active_sets(
    a: str = Query(..., description="path or version label, e.g. 'v4' or 'active_set_v4.json'"),
    b: str = Query(..., description="path or version label, e.g. 'v5'"),
) -> dict:
    """Diff two active sets — what changed between them.

    Reports added, removed, and modified packs (by content_hash). Useful for
    reviewing what an upcoming activation will introduce.

    Examples:
      GET /governance/diff?a=v4&b=v5
      GET /governance/diff?a=active_set_v4.json&b=active_set_v5.json
    """
    def _resolve(label: str) -> Path:
        # Accept short labels like 'v4' or full filenames
        if label.startswith("v") and label[1:].isdigit():
            label = f"active_set_{label}.json"
        elif not label.endswith(".json"):
            label = f"{label}.json"
        # Sanitize against traversal
        if "/" in label or ".." in label:
            raise HTTPException(status_code=400, detail=f"invalid active set label: {label}")
        path = PROJECT_ROOT / label
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"{label} not found at project root")
        return path

    path_a, path_b = _resolve(a), _resolve(b)

    try:
        with open(path_a) as f:
            set_a = json.load(f)
        with open(path_b) as f:
            set_b = json.load(f)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"parse error: {e}")

    packs_a = {p["pack_id"]: p for p in set_a.get("packs", [])}
    packs_b = {p["pack_id"]: p for p in set_b.get("packs", [])}

    added = sorted(packs_b.keys() - packs_a.keys())
    removed = sorted(packs_a.keys() - packs_b.keys())
    common = packs_a.keys() & packs_b.keys()
    modified = []
    unchanged = []
    for pid in sorted(common):
        a_entry = packs_a[pid]
        b_entry = packs_b[pid]
        if a_entry.get("content_hash") != b_entry.get("content_hash") or a_entry.get("version") != b_entry.get("version"):
            modified.append({
                "pack_id": pid,
                "before": {"version": a_entry.get("version"), "content_hash": a_entry.get("content_hash")},
                "after": {"version": b_entry.get("version"), "content_hash": b_entry.get("content_hash")},
            })
        else:
            unchanged.append(pid)

    return {
        "a": {"path": path_a.name, "version": set_a.get("version"), "pack_count": len(packs_a)},
        "b": {"path": path_b.name, "version": set_b.get("version"), "pack_count": len(packs_b)},
        "added": [{"pack_id": pid, **packs_b[pid]} for pid in added],
        "removed": [{"pack_id": pid, **packs_a[pid]} for pid in removed],
        "modified": modified,
        "unchanged_count": len(unchanged),
        "unchanged": unchanged,
    }


@router.get("/verify")
def verify_disk() -> dict:
    """Re-run the pack verifier against the currently active set.

    This is a live disk-drift check — it answers the question 'did anything
    change on disk since activation?' Run on demand or on a schedule.

    The verifier shells out to scripts/governance/verify_pack_set.py so the
    output is identical to the CLI version. Returns exit code, stdout summary,
    and a boolean ok flag.
    """
    try:
        from core.gdu.config import ACTIVE_SET_PATH
        verifier_path = PROJECT_ROOT / "scripts" / "governance" / "verify_pack_set.py"
        if not verifier_path.exists():
            raise HTTPException(status_code=500, detail="verifier script not found")

        proc = subprocess.run(
            [sys.executable, str(verifier_path), str(ACTIVE_SET_PATH.name)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "active_set": ACTIVE_SET_PATH.name,
            "stdout": proc.stdout,
            "stderr": proc.stderr if proc.stderr else None,
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="verifier timed out (>30s)")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"verify failed: {type(e).__name__}: {e}")


# ─── Entity Governance Endpoints (Phase 1+2) ────────────────────────────


@router.get("/conflicts")
async def list_conflicts(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    severity: Optional[str] = None,
    conflict_type: Optional[str] = None,
    reviewed: Optional[bool] = None,
    limit: int = 50,
):
    """List entity governance conflicts. Requires authentication."""
    from storage.postgres.db import get_connection
    import logging
    log = logging.getLogger("enki.governance.conflicts")

    clauses = ["tenant_id = %s"]
    params: list = [auth.tenant_id]
    if severity:
        clauses.append("severity = %s")
        params.append(severity)
    if conflict_type:
        clauses.append("conflict_type = %s")
        params.append(conflict_type)
    if reviewed is not None:
        clauses.append("reviewed_at IS NOT NULL" if reviewed else "reviewed_at IS NULL")
    params.append(min(limit, 200))

    conflicts = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT c.conflict_id, c.conflict_type, c.severity,
                           c.entity_id, c.related_entity_ids, c.competing_values,
                           c.auto_resolved, c.auto_resolution, c.detected_by,
                           c.detected_at, c.reviewed_by, c.reviewed_at,
                           c.resolution, c.metadata,
                           e.canonical_name, e.canonical_type
                    FROM governance_conflicts c
                    LEFT JOIN entities e ON e.entity_id = c.entity_id
                    WHERE {' AND '.join(clauses)}
                    ORDER BY
                        CASE c.severity WHEN 'urgent' THEN 0 WHEN 'routine' THEN 1 ELSE 2 END,
                        c.detected_at DESC
                    LIMIT %s
                """, params)

                for r in cur.fetchall():
                    conflicts.append({
                        "conflict_id": r[0], "conflict_type": r[1], "severity": r[2],
                        "entity_id": r[3], "related_entity_ids": r[4] or [],
                        "competing_values": r[5] or [],
                        "auto_resolved": r[6], "auto_resolution": r[7],
                        "detected_by": r[8],
                        "detected_at": r[9].isoformat() if r[9] else None,
                        "reviewed_by": r[10],
                        "reviewed_at": r[11].isoformat() if r[11] else None,
                        "resolution": r[12], "metadata": r[13] or {},
                        "entity_name": r[14], "entity_type": r[15],
                    })
    except Exception as e:
        log.error("Conflict list failed: %s", e)
        return {"conflicts": [], "error": str(e)}

    return {"conflicts": conflicts, "count": len(conflicts)}


@router.get("/conflicts/summary")
async def conflict_summary(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Conflict counts by severity and type. Requires authentication."""
    from core.gdu.entity_governance import get_pending_conflict_count
    counts = get_pending_conflict_count(auth.tenant_id)
    from storage.postgres.db import get_connection
    by_type: dict = {}
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT conflict_type, count(*)
                    FROM governance_conflicts
                    WHERE tenant_id = %s AND reviewed_at IS NULL
                    GROUP BY conflict_type
                """, (auth.tenant_id,))
                for r in cur.fetchall():
                    by_type[r[0]] = r[1]
    except Exception:
        pass
    return {"counts": counts, "by_type": by_type}


@router.post("/conflicts/{conflict_id}/resolve")
async def resolve_conflict(
    conflict_id: int,
    body: dict = Body(...),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Operator resolution of a conflict. Requires authentication."""
    from storage.postgres.db import get_connection
    import logging
    log = logging.getLogger("enki.governance.resolve")

    resolution = body.get("resolution", "skip")
    note = body.get("note", "")

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE governance_conflicts SET
                        reviewed_by = %s, reviewed_at = NOW(),
                        resolution = %s, resolution_note = %s
                    WHERE conflict_id = %s AND tenant_id = %s
                    RETURNING entity_id
                """, (auth.subject_id, resolution, note, conflict_id, auth.tenant_id))
                row = cur.fetchone()
                if row and row[0]:
                    cur.execute("""
                        UPDATE entities SET conflict_count = GREATEST(conflict_count - 1, 0)
                        WHERE entity_id = %s
                    """, (row[0],))
            conn.commit()
        return {"status": "resolved", "conflict_id": conflict_id, "resolution": resolution}
    except Exception as e:
        log.error("Resolve conflict failed: %s", e)
        return {"status": "error", "error": str(e)}


@router.get("/ldu-p")
async def list_provisional_entities(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    status: str = "pending",
    limit: int = 50,
):
    """List provisional entities awaiting GDU admission. Requires authentication."""
    from storage.postgres.db import get_connection
    entries = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT ldu_p_id, raw_name, suspected_type, source_pipeline,
                           source_signal_id, confidence, evidence,
                           rejection_reason, status, created_at
                    FROM entity_ldu_p
                    WHERE tenant_id = %s AND status = %s
                    ORDER BY created_at DESC LIMIT %s
                """, (auth.tenant_id, status, min(limit, 200)))
                for r in cur.fetchall():
                    entries.append({
                        "ldu_p_id": r[0], "raw_name": r[1], "suspected_type": r[2],
                        "source_pipeline": r[3], "source_signal_id": r[4],
                        "confidence": r[5], "evidence": r[6],
                        "rejection_reason": r[7], "status": r[8],
                        "created_at": r[9].isoformat() if r[9] else None,
                    })
    except Exception as e:
        return {"entries": [], "error": str(e)}
    return {"entries": entries, "count": len(entries)}


@router.post("/ldu-p/{ldu_p_id}/promote")
async def promote_entity_ldu_p(
    ldu_p_id: str,
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Promote a provisional entity LDU-P entry — create it as a real entity via GDU gateway."""
    from storage.postgres.db import get_connection
    from core.gdu.enforce import admit_entity

    body = await request.json()
    entity_type = body.get("signal_type", body.get("entity_type", ""))
    resolved_by = body.get("resolved_by", "governance_ui")

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT raw_name, suspected_type FROM entity_ldu_p WHERE ldu_p_id = %s AND tenant_id = %s AND status = 'pending'",
                    (ldu_p_id, auth.tenant_id),
                )
                row = cur.fetchone()
                if not row:
                    return JSONResponse(status_code=404, content={"error": "LDU-P entry not found or already resolved"})

                raw_name, suspected = row
                use_type = entity_type or suspected or "person"

                # Admit through GDU gateway
                result = admit_entity(
                    tenant_id=auth.tenant_id,
                    name=raw_name,
                    entity_type=use_type,
                    signal_rid=0,
                )

                # Mark as promoted
                cur.execute(
                    "UPDATE entity_ldu_p SET status = 'promoted', resolved_by = %s, resolved_at = NOW() WHERE ldu_p_id = %s",
                    (resolved_by, ldu_p_id),
                )
                conn.commit()

        return {
            "promoted": True,
            "ldu_p_id": ldu_p_id,
            "entity_name": raw_name,
            "entity_type": use_type,
            "node_id": result.node_id if result else None,
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.patch("/ldu-p/{ldu_p_id}/reject")
async def reject_entity_ldu_p(
    ldu_p_id: str,
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Reject a provisional entity LDU-P entry."""
    from storage.postgres.db import get_connection

    body = await request.json()
    resolved_by = body.get("resolved_by", "governance_ui")

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE entity_ldu_p SET status = 'rejected', resolved_by = %s, resolved_at = NOW() WHERE ldu_p_id = %s AND tenant_id = %s AND status = 'pending'",
                    (resolved_by, ldu_p_id, auth.tenant_id),
                )
                if cur.rowcount == 0:
                    return JSONResponse(status_code=404, content={"error": "LDU-P entry not found or already resolved"})
                conn.commit()
        return {"rejected": True, "ldu_p_id": ldu_p_id}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/ldup-triage")
async def ldup_triage(
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Group pending entity_ldu_p rows by suspected_type with example surface
    forms and counts. Powers the /validation/ldup-triage admin page —
    shows the human operator what novel types are stuck in purgatory and
    need a routing/dropping/accepting decision so the substrate can keep
    flowing.

    The result also includes the current entityvocab state (subtypes,
    routes, drops) so the UI can show what each decision would mean.
    """
    from storage.postgres.db import get_connection
    from core.ldu.subtype_validator import _definitions

    groups = []
    total_pending = 0
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT suspected_type, COUNT(*) AS n,
                           MIN(created_at) AS first_seen,
                           MAX(created_at) AS last_seen,
                           (ARRAY_AGG(raw_name ORDER BY created_at DESC))[1:5] AS examples
                      FROM entity_ldu_p
                     WHERE tenant_id = %s AND status = 'pending'
                     GROUP BY suspected_type
                     ORDER BY n DESC
                     LIMIT 100
                """, (auth.tenant_id,))
                for r in cur.fetchall():
                    groups.append({
                        "suspected_type": r[0] or "",
                        "count": int(r[1]),
                        "first_seen": r[2].isoformat() if r[2] else None,
                        "last_seen": r[3].isoformat() if r[3] else None,
                        "examples": list(r[4] or [])[:5],
                    })
                cur.execute(
                    "SELECT COUNT(*) FROM entity_ldu_p WHERE tenant_id = %s AND status = 'pending'",
                    (auth.tenant_id,),
                )
                total_pending = int(cur.fetchone()[0])
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

    defs = _definitions()
    subtypes = sorted((defs.get("subtypes") or {}).keys())
    routes = (defs.get("cross_category_routes") or {}).get("routes", {})
    drops = sorted((defs.get("drop_types") or {}).get("list", []))

    target_categories = sorted({v[0] for v in routes.values()})
    targets_by_category: dict[str, list[str]] = {}
    for v in routes.values():
        targets_by_category.setdefault(v[0], [])
        if v[1] not in targets_by_category[v[0]]:
            targets_by_category[v[0]].append(v[1])
    for cat in targets_by_category:
        targets_by_category[cat].sort()

    return {
        "groups": groups,
        "total_pending": total_pending,
        "vocab": {
            "subtypes": subtypes,
            "drops": drops,
            "targets_by_category": targets_by_category,
        },
    }


@router.post("/ldup-decide")
async def ldup_decide(
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Apply a routing/dropping/accept decision to every pending
    entity_ldu_p row matching a suspected_type, and optionally persist
    the decision to entityvocab so future ingest applies it
    automatically.

    Body shape:
        {
          "suspected_type": "warzone",
          "decision":       "route" | "drop" | "accept" | "alias",
          "target_category": "place",            # required for route
          "target_subtype":  "named_area",       # required for route
          "canonical_subtype": "organization",   # required for accept/alias
          "persist_to_vocab": true               # default true
        }

    Effects:
      - "route":  matching rows get rejection_reason='route:<cat>.<sub>';
                  promoter picks them up next tick (≤5 min).
      - "drop":   matching rows immediately marked status='dropped'.
      - "accept": matching rows get rejection_reason='' (cleared);
                  promoter re-validates and admits to entities.
      - "alias":  same as accept but ALSO appends the suspected_type to
                  the canonical subtype's aliases in entityvocab.
    """
    from storage.postgres.db import get_connection
    from core.ldu.subtype_validator import _definitions, reload as vocab_reload

    body = await request.json()
    suspected = (body.get("suspected_type") or "").strip()
    decision = (body.get("decision") or "").strip()
    target_cat = (body.get("target_category") or "").strip().lower() or None
    target_sub = (body.get("target_subtype") or "").strip().lower() or None
    canonical = (body.get("canonical_subtype") or "").strip().lower() or None
    persist = bool(body.get("persist_to_vocab", True))

    if not suspected:
        return JSONResponse(status_code=400, content={"error": "suspected_type required"})
    if decision not in ("route", "drop", "accept", "alias"):
        return JSONResponse(status_code=400, content={"error": "decision must be one of route/drop/accept/alias"})
    if decision == "route" and not (target_cat and target_sub):
        return JSONResponse(status_code=400, content={"error": "route decision requires target_category + target_subtype"})
    if decision in ("accept", "alias") and not canonical:
        return JSONResponse(status_code=400, content={"error": "accept/alias decision requires canonical_subtype"})

    suspected_key = suspected.lower()
    affected_rows = 0
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                if decision == "drop":
                    cur.execute("""
                        UPDATE entity_ldu_p
                           SET status = 'dropped',
                               reviewed_by = 'ldup_triage_ui',
                               reviewed_at = NOW(),
                               rejection_reason = COALESCE(rejection_reason || ' | ', '')
                                                || 'triage_drop'
                         WHERE tenant_id = %s
                           AND status = 'pending'
                           AND LOWER(suspected_type) = %s
                    """, (auth.tenant_id, suspected_key))
                elif decision == "route":
                    cur.execute("""
                        UPDATE entity_ldu_p
                           SET rejection_reason = %s
                         WHERE tenant_id = %s
                           AND status = 'pending'
                           AND LOWER(suspected_type) = %s
                    """, (f"route:{target_cat}.{target_sub}", auth.tenant_id, suspected_key))
                else:
                    # accept / alias — clear rejection_reason and let the
                    # promoter re-validate (vocab edit below makes it
                    # admittable). Also normalize suspected_type to the
                    # canonical form so admit_entity stamps it correctly.
                    cur.execute("""
                        UPDATE entity_ldu_p
                           SET suspected_type = %s,
                               rejection_reason = NULL
                         WHERE tenant_id = %s
                           AND status = 'pending'
                           AND LOWER(suspected_type) = %s
                    """, (canonical, auth.tenant_id, suspected_key))
                affected_rows = cur.rowcount
            conn.commit()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"db update failed: {e}"})

    vocab_update = {"persisted": False}
    if persist:
        try:
            vocab_update = _persist_vocab_decision(
                decision=decision,
                suspected_type=suspected,
                target_cat=target_cat,
                target_sub=target_sub,
                canonical=canonical,
            )
            vocab_reload()
        except Exception as e:
            vocab_update = {"persisted": False, "error": str(e)}

    return {
        "decision": decision,
        "suspected_type": suspected,
        "affected_rows": affected_rows,
        "vocab": vocab_update,
    }


def _persist_vocab_decision(
    *,
    decision: str,
    suspected_type: str,
    target_cat: Optional[str],
    target_sub: Optional[str],
    canonical: Optional[str],
    decided_by: str = "ldup_triage_ui",
) -> dict:
    """Record the operator's decision in entityvocab_overrides. The
    validator merges overrides on top of the JSON-defined vocab on every
    reload (60s TTL), so the decision becomes effective without
    restart. Overrides table is append-only — most recent row per
    suspected_type wins."""
    from storage.postgres.db import get_connection
    key = suspected_type.strip().lower()
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO entityvocab_overrides
                        (suspected_type, decision,
                         target_category, target_subtype, canonical_subtype,
                         decided_by)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (key, decision, target_cat, target_sub, canonical, decided_by))
            conn.commit()
    except Exception as e:
        return {"persisted": False, "error": f"db insert failed: {e}"}

    if decision == "route":
        change = f"added '{key}' -> {target_cat}/{target_sub}"
    elif decision == "drop":
        change = f"added '{key}' to drop_types"
    elif decision == "alias":
        change = f"added '{key}' as alias of '{canonical}'"
    else:
        change = f"recorded accept '{key}' -> {canonical}"
    return {"persisted": True, "store": "entityvocab_overrides", "change": change}


@router.get("/collapse-stats")
async def collapse_stats(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Headline node-collapse metrics: total admission decisions broken down
    by operation, collapse rate, plus chain-state freshness. Powers the
    /governance page hero strip.
    """
    from storage.postgres.db import get_connection
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT operation, COUNT(*)::bigint
                      FROM gdu_admission_log
                     WHERE tenant_id = %s
                     GROUP BY operation
                    """,
                    (auth.tenant_id,),
                )
                by_op = {row[0]: int(row[1]) for row in cur.fetchall()}
                total = sum(by_op.values())

                cur.execute(
                    """
                    SELECT COUNT(*)::bigint, COUNT(DISTINCT operation)
                      FROM gdu_admission_log
                     WHERE tenant_id = %s
                       AND created_at > NOW() - INTERVAL '1 hour'
                    """,
                    (auth.tenant_id,),
                )
                row = cur.fetchone()
                last_hour, _ = int(row[0] or 0), int(row[1] or 0)

                cur.execute(
                    """
                    SELECT COUNT(*)::bigint FROM entities WHERE tenant_id = %s;
                    """,
                    (auth.tenant_id,),
                )
                entities_total = int(cur.fetchone()[0] or 0)

                cur.execute(
                    """
                    SELECT COUNT(*)::bigint FROM knowledge_edges WHERE project_id = %s OR project_id = 'default'
                    """,
                    (auth.tenant_id,),
                )
                edges_total = int(cur.fetchone()[0] or 0)

                cur.execute(
                    """
                    SELECT current_chain_hash, mutation_count, updated_at
                      FROM entity_chain_state WHERE tenant_id = %s
                    """,
                    (auth.tenant_id,),
                )
                cs = cur.fetchone()
        merged = (by_op.get("collapse", 0) + by_op.get("strengthen", 0))
        collapse_rate = (merged / total) if total else 0.0
        return {
            "total_admissions": total,
            "by_operation": by_op,
            "collapse_rate": round(collapse_rate, 4),
            "merged_count": merged,
            "last_hour_admissions": last_hour,
            "entities_total": entities_total,
            "edges_total": edges_total,
            "chain": {
                "hash": (cs[0][:16] + "…") if cs and cs[0] else None,
                "mutation_count": int(cs[1]) if cs else 0,
                "updated_at": cs[2].isoformat() if cs and cs[2] else None,
                "initialized": bool(cs),
            },
        }
    except Exception as e:
        return {"error": str(e)}


@router.get("/chain/state")
async def entity_chain_state_view(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Current entity chain hash and stats. Requires authentication."""
    from storage.postgres.db import get_connection
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT current_chain_hash, last_receipt_rid, mutation_count, updated_at
                    FROM entity_chain_state WHERE tenant_id = %s
                """, (auth.tenant_id,))
                row = cur.fetchone()
                if not row:
                    return {"chain_hash": None, "mutations": 0, "initialized": False}
                return {
                    "chain_hash": row[0], "last_receipt_rid": row[1],
                    "mutation_count": row[2],
                    "updated_at": row[3].isoformat() if row[3] else None,
                    "initialized": True,
                }
    except Exception as e:
        return {"error": str(e)}


@router.get("/chain/recent-events")
async def entity_chain_recent_events(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    limit: int = 20,
):
    """Recent entity mutation events. Requires authentication."""
    from storage.postgres.db import get_connection
    events = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT mutation_type, entity_ids, payload, source_pipeline,
                           commit_time, chain_hash
                    FROM entity_receipts
                    WHERE tenant_id = %s
                    ORDER BY commit_time DESC LIMIT %s
                """, (auth.tenant_id, min(limit, 100)))
                for r in cur.fetchall():
                    events.append({
                        "mutation_type": r[0], "entity_ids": r[1] or [],
                        "payload": r[2] or {}, "source_pipeline": r[3],
                        "commit_time": r[4].isoformat() if r[4] else None,
                        "chain_hash": (r[5][:16] + "...") if r[5] else None,
                    })
    except Exception as e:
        return {"events": [], "error": str(e)}
    return {"events": events}


@router.get("/merge-candidates")
async def list_merge_candidates(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    limit: int = Query(20, le=100),
):
    """Find entity pairs that may be duplicates based on name similarity."""
    from storage.postgres.db import get_connection

    candidates = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Use pg_trgm similarity to find close matches
                cur.execute("""
                    SELECT a.entity_id, a.canonical_name, a.canonical_type, a.signal_count,
                           b.entity_id, b.canonical_name, b.canonical_type, b.signal_count,
                           similarity(a.normalized_name, b.normalized_name) AS sim
                    FROM entities a
                    JOIN entities b ON a.entity_id < b.entity_id
                         AND a.tenant_id = b.tenant_id
                         AND a.canonical_type = b.canonical_type
                         AND similarity(a.normalized_name, b.normalized_name) >= 0.55
                    WHERE a.tenant_id = %s
                    ORDER BY sim DESC
                    LIMIT %s
                """, (auth.tenant_id, limit))
                for r in cur.fetchall():
                    candidates.append({
                        "entity_a": {"entity_id": r[0], "name": r[1], "type": r[2], "signal_count": r[3]},
                        "entity_b": {"entity_id": r[4], "name": r[5], "type": r[6], "signal_count": r[7]},
                        "similarity": round(float(r[8]), 3),
                    })
    except Exception as e:
        return {"candidates": [], "error": str(e)}
    return {"candidates": candidates, "count": len(candidates)}


@router.post("/merge")
async def merge_entities(
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Merge two entities — keep_id absorbs merge_id. Creates redirect."""
    from storage.postgres.db import get_connection

    body = await request.json()
    keep_id = body.get("keep_id")
    merge_id = body.get("merge_id")

    if not keep_id or not merge_id or keep_id == merge_id:
        return JSONResponse(status_code=400, content={"error": "Invalid keep_id/merge_id"})

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Get both entities
                cur.execute(
                    "SELECT entity_id, canonical_name, signal_count, aliases FROM entities WHERE entity_id = %s AND tenant_id = %s",
                    (keep_id, auth.tenant_id),
                )
                keep = cur.fetchone()
                cur.execute(
                    "SELECT entity_id, canonical_name, signal_count, aliases FROM entities WHERE entity_id = %s AND tenant_id = %s",
                    (merge_id, auth.tenant_id),
                )
                merge = cur.fetchone()

                if not keep or not merge:
                    return JSONResponse(status_code=404, content={"error": "Entity not found"})

                # Combine signal counts
                combined_count = (keep[2] or 0) + (merge[2] or 0)
                # Combine aliases (unique)
                keep_aliases = keep[3] or []
                merge_aliases = merge[3] or []
                combined_aliases = list(set(keep_aliases + merge_aliases + [merge[1]]))

                # Update keep entity
                cur.execute("""
                    UPDATE entities SET
                        signal_count = %s,
                        aliases = %s,
                        updated_at = NOW()
                    WHERE entity_id = %s
                """, (combined_count, combined_aliases, keep_id))

                # Create redirect (old → new)
                cur.execute("""
                    INSERT INTO entity_redirects (old_entity_id, new_entity_id, tenant_id, created_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT DO NOTHING
                """, (merge_id, keep_id, auth.tenant_id))

                # Update knowledge_nodes that point to merged entity
                cur.execute("""
                    UPDATE knowledge_nodes SET
                        master_entity_id = %s, updated_at = NOW()
                    WHERE master_entity_id = %s AND tenant_id = %s
                """, (keep_id, merge_id, auth.tenant_id))

                # Update knowledge_edges
                cur.execute("""
                    UPDATE knowledge_edges SET
                        source_node_id = REPLACE(source_node_id, %s, %s)
                    WHERE source_node_id LIKE %s
                """, (f"entity:{merge_id}", f"entity:{keep_id}", f"entity:{merge_id}%"))
                cur.execute("""
                    UPDATE knowledge_edges SET
                        target_node_id = REPLACE(target_node_id, %s, %s)
                    WHERE target_node_id LIKE %s
                """, (f"entity:{merge_id}", f"entity:{keep_id}", f"entity:{merge_id}%"))

                conn.commit()

        return {
            "merged": True,
            "keep_id": keep_id,
            "merge_id": merge_id,
            "combined_signal_count": combined_count,
            "combined_aliases": combined_aliases,
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ─── Predicate review queue (predicate_dictionary auto-extender) ──────────

@router.get("/predicate-queue")
def predicate_queue(
    status: str = Query("pending"),
    limit: int = Query(50, ge=1, le=200),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """List entries in the predicate_review_queue.

    The predicate_extender worker writes here whenever it can't auto-promote
    a verb candidate to predicate_dictionary at high confidence. Operator
    reviews + approves/rejects/remaps via the /governance UI.
    """
    from storage.postgres.db import get_connection
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT queue_id, candidate_lemma, occurrences,
                           sample_statement_ids,
                           proposed_edge_type, proposed_confidence,
                           proposed_by, evidence, status,
                           reviewed_by, reviewed_at, applied_at,
                           final_edge_type, created_at
                      FROM predicate_review_queue
                     WHERE status = %s
                     ORDER BY occurrences DESC, created_at DESC
                     LIMIT %s
                    """,
                    (status, limit),
                )
                rows = []
                for r in cur.fetchall():
                    rows.append({
                        "queue_id": r[0],
                        "candidate_lemma": r[1],
                        "occurrences": r[2],
                        "sample_statement_ids": r[3] or [],
                        "proposed_edge_type": r[4],
                        "proposed_confidence": r[5],
                        "proposed_by": r[6],
                        "evidence": r[7] or {},
                        "status": r[8],
                        "reviewed_by": r[9],
                        "reviewed_at": r[10].isoformat() if r[10] else None,
                        "applied_at": r[11].isoformat() if r[11] else None,
                        "final_edge_type": r[12],
                        "created_at": r[13].isoformat() if r[13] else None,
                    })
        return {"items": rows, "count": len(rows), "status_filter": status}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/predicate-queue/edge-types")
def predicate_queue_edge_types(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Available edge_types — used by the UI when remapping a proposal."""
    from storage.postgres.db import get_connection
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT edge_type, COUNT(*) AS n
                      FROM predicate_dictionary
                     GROUP BY edge_type
                     ORDER BY n DESC
                    """
                )
                return {"edge_types": [{"edge_type": r[0], "usage": r[1]} for r in cur.fetchall()]}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.post("/predicate-queue/{queue_id}/approve")
def predicate_queue_approve(
    queue_id: int,
    body: dict = Body(default_factory=dict),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Approve a queued predicate proposal — writes the mapping into
    predicate_dictionary and re-pends any rejected statements with this
    lemma. Optional body: {"edge_type": "..."} to override the LLM's
    proposed edge_type (i.e. remap).
    """
    from storage.postgres.db import get_connection
    from core.gdu.address import assign_address

    override_edge = (body or {}).get("edge_type")
    reviewer = (body or {}).get("reviewer") or "operator"

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT candidate_lemma, occurrences, proposed_edge_type,
                           proposed_confidence, status
                      FROM predicate_review_queue
                     WHERE queue_id = %s
                    """,
                    (queue_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="queue_id not found")
                lemma, occurrences, proposed_edge, proposed_conf, status = row
                if status not in ("pending",):
                    return JSONResponse(
                        status_code=409,
                        content={"error": f"queue row is {status}, can only approve pending"},
                    )
                final_edge = override_edge or proposed_edge
                if not final_edge:
                    raise HTTPException(
                        status_code=400,
                        detail="no edge_type to apply — supply {'edge_type': '...'} in body",
                    )

                # Idempotent insert into predicate_dictionary
                cur.execute(
                    "SELECT 1 FROM predicate_dictionary WHERE canonical_lemma=%s AND edge_type=%s",
                    (lemma, final_edge),
                )
                is_new = cur.fetchone() is None
                if is_new:
                    addr = assign_address(conn, "form", "verb")
                    cur.execute(
                        """
                        INSERT INTO predicate_dictionary
                            (canonical_lemma, edge_type, base60_address,
                             confidence, observation_count, source)
                        VALUES (%s, %s, %s, 1.00, %s, 'human')
                        """,
                        (lemma, final_edge, addr, occurrences),
                    )

                # Re-pend any rejected statements with this lemma
                cur.execute(
                    """
                    UPDATE statements
                       SET validation_status='pending', validation_reason=NULL
                     WHERE predicate_lemma=%s
                       AND validation_status='rejected'
                       AND validation_reason LIKE 'no_predicate_mapping:%%'
                    """,
                    (lemma,),
                )
                repended = cur.rowcount or 0

                cur.execute(
                    """
                    UPDATE predicate_review_queue
                       SET status='approved',
                           reviewed_by=%s,
                           reviewed_at=NOW(),
                           applied_at=NOW(),
                           final_edge_type=%s,
                           updated_at=NOW()
                     WHERE queue_id=%s
                    """,
                    (reviewer, final_edge, queue_id),
                )
                conn.commit()
        return {
            "approved": True, "queue_id": queue_id,
            "lemma": lemma, "edge_type": final_edge,
            "is_new_dict_entry": is_new,
            "statements_repended": repended,
        }
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.post("/predicate-queue/{queue_id}/reject")
def predicate_queue_reject(
    queue_id: int,
    body: dict = Body(default_factory=dict),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Reject a queued predicate proposal. Statements stay rejected,
    queue row marked rejected so the lemma won't re-queue."""
    from storage.postgres.db import get_connection
    reviewer = (body or {}).get("reviewer") or "operator"
    note = (body or {}).get("note") or ""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE predicate_review_queue
                       SET status='rejected',
                           reviewed_by=%s,
                           reviewed_at=NOW(),
                           evidence = COALESCE(evidence,'{}'::jsonb) || jsonb_build_object('reject_note', %s::text),
                           updated_at=NOW()
                     WHERE queue_id=%s
                       AND status='pending'
                    RETURNING candidate_lemma
                    """,
                    (reviewer, note, queue_id),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="queue_id not pending")
                conn.commit()
        return {"rejected": True, "queue_id": queue_id, "lemma": row[0]}
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/predicate-queue/{queue_id}/samples")
def predicate_queue_samples(
    queue_id: int,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Pull the sample statements referenced by this queue row so the
    operator can read context before approving."""
    from storage.postgres.db import get_connection
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT sample_statement_ids FROM predicate_review_queue WHERE queue_id=%s",
                    (queue_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="not found")
                stmt_ids = row[0] or []
                if not stmt_ids:
                    return {"samples": []}
                cur.execute(
                    """
                    SELECT s.statement_id, s.raw_text,
                           se.canonical_name AS subj, se.canonical_type AS subj_type,
                           oe.canonical_name AS obj,  oe.canonical_type AS obj_type,
                           s.signal_rid, s.document_id
                      FROM statements s
                      LEFT JOIN entities se ON se.entity_id = s.speaker_entity_id
                      LEFT JOIN entities oe ON oe.entity_id = s.object_entity_id
                     WHERE s.statement_id = ANY(%s)
                    """,
                    (stmt_ids,),
                )
                samples = []
                for r in cur.fetchall():
                    samples.append({
                        "statement_id": r[0],
                        "raw_text": r[1],
                        "subject": r[2], "subject_type": r[3],
                        "object": r[4], "object_type": r[5],
                        "signal_rid": r[6], "document_id": r[7],
                    })
        return {"samples": samples}
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ─── Federation handshake (peer compatibility check) ──────────────────────

@router.get("/active-set/verify")
def active_set_verify(version: Optional[str] = None,
                      pack_hash: Optional[str] = None):
    """Federation handshake — peer asks this node 'are we on the same GDU?'

    Returns whether peer's (active_set_version, pack_hash) match this
    node's. Used by the work-claim coordinator (Sprint 5) to gate which
    nodes can contribute compute to which library runs.

    Peers with mismatched substrate can still subscribe to a library's
    consensus graph (read-only). They just can't submit results until
    they upgrade to the matching active_set.
    """
    # Direct read from the canonical state — keep this endpoint
    # self-contained so peers can hit it with one HTTP GET.
    state = {"version": None, "pack_hash": None, "pack_version": None}
    try:
        from services.api.src.routes.governance import state as gov_state
        s = gov_state()
        state["version"] = s.get("active_set_version") or s.get("verification", {}).get("active_set_version")
        state["pack_version"] = s.get("verification", {}).get("pack_version")
        # Pack hash — compute from active pack registry if not in state
        try:
            from core.gdu.pack_registry import get_pack_hash
            state["pack_hash"] = get_pack_hash()
        except Exception:
            state["pack_hash"] = None
    except Exception as e:
        log.warning("active-set/verify own read failed: %s", e)

    compatible: Optional[bool] = None
    reasons: list[str] = []
    if version is not None or pack_hash is not None:
        compatible = True
        if version is not None and state["version"] and version != state["version"]:
            compatible = False
            reasons.append(f"version_mismatch: peer={version} self={state['version']}")
        if pack_hash is not None and state["pack_hash"] and pack_hash != state["pack_hash"]:
            compatible = False
            reasons.append(f"pack_hash_mismatch: peer={pack_hash[:12]}... self={state['pack_hash'][:12]}...")

    return {
        "self": state,
        "peer_advertised": {"version": version, "pack_hash": pack_hash},
        "compatible": compatible,
        "reasons": reasons,
    }


@router.get("/active-set/fingerprint")
def active_set_fingerprint():
    """One-line identity card a peer publishes when registering with this
    node. The fingerprint = sha256(active_set_version + pack_hash + …)."""
    import hashlib
    state = active_set_verify()
    parts = [
        str(state["self"].get("version") or ""),
        str(state["self"].get("pack_hash") or ""),
        str(state["self"].get("pack_version") or ""),
    ]
    fp = hashlib.sha256(("|".join(parts)).encode()).hexdigest()
    return {"fingerprint": fp, **state["self"]}
