"""
Main ingestion pipeline for ObservabilityEvent JSONL to storage.

Pipeline stages:
1. Parse JSONL safely (no crashes on malformed input)
2. Map ObservabilityEvent → AuditEvent (Appendix A rules)
3. Validate with Phase 0 validator
4. Store accepted events append-only
5. Track rejected items with reasons
6. Generate receipt with digest
7. Compute chain hash
8. Update chain state (UPSERT allowed for head tracking)
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any

from storage.postgres.db import get_connection
from storage.ingest.mapping import map_observability_to_audit
from storage.crypto.cid import compute_cid
from storage.crypto.chain import get_genesis_hash, compute_receipt_digest, compute_chain_hash
from storage.ids.rid import encode_rid
from core.validation.validate_audit_events import validate_audit_event_instance
from core.packs import load_pack


def _get_node_id() -> str:
    """Get node_id for audit event stamping (FD-2).
    Falls back to 'local' if ENKI_NODE_ID not set (dev/test mode)."""
    return os.environ.get("ENKI_NODE_ID", "local")


# Resolve repository root for pack loading (hardware-portable)
def _get_repo_root() -> Path:
    """Get repository root directory (portable across environments)."""
    current = Path(__file__).resolve()
    # storage/ingest/ingestion.py -> walk up to repo root
    repo_root = current.parent.parent.parent
    return repo_root


def ingest_jsonl(jsonl_lines: List[str], tenant_id: str) -> Dict[str, Any]:
    """
    Ingest JSONL ObservabilityEvent lines into PostgreSQL storage.
    
    This is the main entry point for Phase 3 Milestone 0.
    
    Pipeline:
    1. Parse JSONL safely (no crashes on malformed JSON)
    2. Map ObservabilityEvent → AuditEvent per Appendix A
    3. Validate with Phase 0 validator (validate_audit_event_instance)
    4. Store accepted events to audit_events (append-only)
    5. Track rejected items in receipt
    6. Compute receipt_digest from receipt_core
    7. Fetch/compute previous chain_hash
    8. Compute new chain_hash = SHA256(prev || digest)
    9. Store receipt (append-only)
    10. Update chain_state (UPSERT for head tracking)
    
    Args:
        jsonl_lines: List of JSONL strings (one ObservabilityEvent per line)
        tenant_id: Tenant context for ingestion
    
    Returns:
        Receipt dict with:
        - receipt_id: DB-assigned ID
        - outcome: FULL_ACCEPT | PARTIAL_ACCEPT | FULL_REJECT
        - accepted_count: Number of events stored
        - rejected_count: Number of events rejected
        - chain_hash: New chain head
        - receipt_digest: Hash of receipt core
        - accepted_rids: List of Base-60 RIDs for accepted events
        - rejected_items: List of rejection details
    """
    # Load vocabulary packs for Phase 0 validation (use absolute paths)
    repo_root = _get_repo_root()
    audit_vocab = load_pack(repo_root / "libraries" / "auditvocab")
    identity_pack = load_pack(repo_root / "libraries" / "identity")
    
    if not audit_vocab.ok or not identity_pack.ok:
        raise RuntimeError(f"Failed to load required vocabulary packs. "
                          f"audit_vocab.ok={audit_vocab.ok} (errors: {audit_vocab.errors}), "
                          f"identity_pack.ok={identity_pack.ok} (errors: {identity_pack.errors})")
    
    accepted_items = []
    rejected_items = []
    
    # ===== PHASE 1: Parse, Map, Validate =====
    for idx, line in enumerate(jsonl_lines):
        line = line.strip()
        if not line:
            continue
        
        # Parse JSONL safely - malformed JSON goes to rejected
        try:
            obs_event = json.loads(line)
        except json.JSONDecodeError as e:
            rejected_items.append({
                'line_number': idx + 1,
                'reason': 'parse_error',
                'error': str(e),
                'raw_line': line[:100]  # Truncate for safety
            })
            continue
        
        # Map ObservabilityEvent → AuditEvent
        try:
            audit_event, event_tenant_id = map_observability_to_audit(obs_event, tenant_id)
        except (KeyError, TypeError, ValueError) as e:
            rejected_items.append({
                'line_number': idx + 1,
                'reason': 'mapping_error',
                'error': str(e),
                'obs_event_id': obs_event.get('event_id', 'unknown')
            })
            continue
        
        # Validate with Phase 0 validator
        validation_result = validate_audit_event_instance(
            audit_event,
            emit_events=False  # Don't emit during ingestion
        )
        
        if validation_result.valid:
            accepted_items.append({
                'audit_event': audit_event,
                'obs_event_id': obs_event.get('event_id', 'unknown'),
                'tenant_id': event_tenant_id  # Store tenant_id as context
            })
        else:
            rejected_items.append({
                'line_number': idx + 1,
                'reason': 'validation_failed',
                'obs_event_id': obs_event.get('event_id', 'unknown'),
                'errors': [str(e) for e in validation_result.errors]
            })
    
    # ===== PHASE 2: Store + Generate Receipt + Chain Hash =====
    with get_connection() as conn:
        with conn.cursor() as cur:
            stored_rids = []
            stored_cids = []
            
            
            # Store each accepted event (append-only)
            for item in accepted_items:
                audit_event = item['audit_event']
                item_tenant_id = item['tenant_id']  # Use tenant_id from context
                
                # Compute CID (content hash)
                cid = compute_cid(audit_event)
                
                # Insert into audit_events (append-only ledger)
                # FD-2: node_id stamped on every audit event
                cur.execute("""
                    INSERT INTO audit_events
                    (tenant_id, cid, payload_json, event_type, timestamp, node_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, cid) DO NOTHING
                    RETURNING rid
                """, (
                    item_tenant_id,  # Use extracted tenant_id, not from payload
                    cid,
                    json.dumps(audit_event),
                    audit_event.get('event_type'),
                    audit_event.get('timestamp'),
                    _get_node_id(),
                ))
                
                result = cur.fetchone()
                if result:
                    rid_int = result[0]
                    rid_str = encode_rid(rid_int)
                    stored_rids.append(rid_str)
                    stored_cids.append(cid)
                else:
                    pass
            
            # Build receipt_core (canonical, hashable)
            # EXCLUDES: chain_hash, receipt_id, RIDs, commit_time
            receipt_core = {
                'tenant_id': tenant_id,
                'submission_time': datetime.now(timezone.utc).isoformat(),
                'accepted_count': len(stored_cids),  # Use actual stored count
                'rejected_count': len(rejected_items),
                'accepted_cids': stored_cids,  # CIDs are content-based, stable
                'rejected_reasons': [r['reason'] for r in rejected_items]
            }
            
            # Compute receipt digest from canonical core
            receipt_digest = compute_receipt_digest(receipt_core)
            
            # Get previous chain hash (or genesis if first receipt)
            cur.execute("""
                SELECT current_chain_hash 
                FROM chain_state 
                WHERE tenant_id = %s
            """, (tenant_id,))
            
            row = cur.fetchone()
            if row:
                prev_chain_hash = row[0]
            else:
                # First receipt for this tenant - use genesis
                prev_chain_hash = get_genesis_hash(tenant_id)
            
            # Compute new chain hash
            new_chain_hash = compute_chain_hash(prev_chain_hash, receipt_digest)
            
            # Determine outcome
            if len(rejected_items) == 0:
                outcome = 'FULL_ACCEPT'
            elif len(stored_cids) == 0:
                outcome = 'FULL_REJECT'
            else:
                outcome = 'PARTIAL_ACCEPT'
            
            # Build receipt_full (includes RIDs and chain context for convenience)
            receipt_full = {
                **receipt_core,
                'outcome': outcome,
                'receipt_digest': receipt_digest,
                'chain_hash': new_chain_hash,
                'accepted_rids': stored_rids,
                'rejected_items': rejected_items
            }
            
            # Store receipt (append-only ledger)
            cur.execute("""
                INSERT INTO receipts 
                (tenant_id, outcome, accepted_count, rejected_count, 
                 receipt_core_json, receipt_digest, chain_hash, previous_chain_hash, receipt_full_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING rid
            """, (
                tenant_id,
                outcome,
                len(stored_cids),
                len(rejected_items),
                json.dumps(receipt_core),
                receipt_digest,
                new_chain_hash,
                prev_chain_hash,  # Store for Milestone 1 verification
                json.dumps(receipt_full)
            ))
            
            receipt_id = cur.fetchone()[0]
            
            # Update chain_state (UPSERT allowed - this is mutable head state)
            cur.execute("""
                INSERT INTO chain_state (tenant_id, current_chain_hash, last_receipt_rid)
                VALUES (%s, %s, %s)
                ON CONFLICT (tenant_id) 
                DO UPDATE SET 
                    current_chain_hash = EXCLUDED.current_chain_hash,
                    last_receipt_rid = EXCLUDED.last_receipt_rid,
                    updated_at = NOW()
            """, (tenant_id, new_chain_hash, receipt_id))
            
            conn.commit()
            
            # Return receipt with DB-assigned ID (Base-60 encoded for RID contract)
            receipt_full['receipt_id'] = encode_rid(receipt_id)
            # Also return numeric txid for backward compatibility (Atlas uses receipt_id)
            receipt_full['txid'] = encode_rid(receipt_id)
            return receipt_full
