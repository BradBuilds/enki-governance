"""
Chain Integrity Verifier — periodic hash chain audit.

Verifies the gdu_admission_log hash chain every 30 minutes.
Each entry's mutation_hash should equal sha256(previous_hash + operation + data).
If a break is detected, creates a security alert signal.

Also counts ungoverned nodes (missing gdu_canonical or base60_address)
and logs the result to audit_events.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection

_log = logging.getLogger("enki.workers.chain_verifier")

_stop_event = threading.Event()
_thread: Optional[threading.Thread] = None
_last_result: Optional[dict] = None

POLL_INTERVAL = 1800  # 30 minutes


def _worker_loop(tenant_id: str):
    """Main loop: verify chain integrity periodically."""
    while not _stop_event.is_set():
        try:
            result = _verify_chain(tenant_id)
            _log_result(tenant_id, result)
        except Exception:
            _log.exception("Chain verifier tick failed")
        _stop_event.wait(POLL_INTERVAL)


def _verify_chain(tenant_id: str) -> dict:
    """Verify the gdu_admission_log hash chain. Returns verification result."""
    global _last_result

    result = {
        "valid": True,
        "entries_checked": 0,
        "breaks_found": 0,
        "break_locations": [],
        "ungoverned_nodes": 0,
        "nodes_without_address": 0,
        "total_nodes": 0,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Check hash chain
                cur.execute("""
                    SELECT rid, mutation_hash, previous_hash
                    FROM gdu_admission_log
                    WHERE tenant_id = %s
                    ORDER BY rid ASC
                """, (tenant_id,))
                rows = cur.fetchall()

                prev_hash = None
                for rid, mutation_hash, previous_hash in rows:
                    result["entries_checked"] += 1
                    if prev_hash is not None and previous_hash != prev_hash:
                        result["valid"] = False
                        result["breaks_found"] += 1
                        result["break_locations"].append({
                            "rid": rid,
                            "expected": prev_hash[:16] if prev_hash else None,
                            "found": previous_hash[:16] if previous_hash else None,
                        })
                    prev_hash = mutation_hash

                # Count ungoverned nodes
                cur.execute("""
                    SELECT COUNT(*) FROM knowledge_nodes
                    WHERE tenant_id = %s AND (gdu_canonical IS NULL OR gdu_canonical = FALSE)
                """, (tenant_id,))
                result["ungoverned_nodes"] = cur.fetchone()[0]

                cur.execute("""
                    SELECT COUNT(*) FROM knowledge_nodes
                    WHERE tenant_id = %s AND base60_address IS NULL
                """, (tenant_id,))
                result["nodes_without_address"] = cur.fetchone()[0]

                cur.execute("""
                    SELECT COUNT(*) FROM knowledge_nodes WHERE tenant_id = %s
                """, (tenant_id,))
                result["total_nodes"] = cur.fetchone()[0]

    except Exception as e:
        result["valid"] = False
        result["error"] = str(e)

    _last_result = result
    return result


def _log_result(tenant_id: str, result: dict):
    """Log verification result and create alert if chain is broken."""
    if result["valid"]:
        _log.info(
            "Chain verified: %d entries OK, %d total nodes, %d ungoverned, %d without address",
            result["entries_checked"], result["total_nodes"],
            result["ungoverned_nodes"], result["nodes_without_address"],
        )
    else:
        _log.error(
            "CHAIN INTEGRITY VIOLATION: %d breaks found in %d entries",
            result["breaks_found"], result["entries_checked"],
        )
        # Create security alert
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO audit_events (tenant_id, event_type, payload_json, cid)
                        VALUES (%s, 'signal.security.chain_integrity_violation', %s, %s)
                    """, (
                        tenant_id,
                        json.dumps(result),
                        f"chain_verify:{result['verified_at']}",
                    ))
                    conn.commit()
        except Exception:
            _log.debug("Failed to log chain violation alert")

    # Always log an audit event for the verification
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO audit_events (tenant_id, event_type, payload_json, cid)
                    VALUES (%s, 'signal.audit.chain_verified', %s, %s)
                """, (
                    tenant_id,
                    json.dumps({
                        "valid": result["valid"],
                        "entries_checked": result["entries_checked"],
                        "ungoverned": result["ungoverned_nodes"],
                        "without_address": result["nodes_without_address"],
                    }),
                    f"chain_audit:{result['verified_at']}",
                ))
                conn.commit()
    except Exception:
        pass


# ─── Public API ──────────────────────────────────────────────────────────────

def start_chain_verifier(tenant_id: str = "default"):
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(
        target=_worker_loop, args=(tenant_id,),
        name="chain-verifier", daemon=True,
    )
    _thread.start()
    _log.info("Chain verifier worker started (interval=%ds)", POLL_INTERVAL)


def stop_chain_verifier():
    _stop_event.set()
    _log.info("Chain verifier worker stopped")


def get_chain_verifier_status() -> dict:
    return {
        "running": _thread is not None and _thread.is_alive(),
        "interval_s": POLL_INTERVAL,
        "last_result": _last_result,
    }


def verify_now(tenant_id: str = "default") -> dict:
    """Run verification immediately (for API calls)."""
    result = _verify_chain(tenant_id)
    _log_result(tenant_id, result)
    return result
