"""
Phase 3 Milestone 1: Read path and chain verification queries.

Key design decisions:
- Use integer RID ordering (ORDER BY rid ASC, not Base-60 string)
- Use integer cursors for pagination (no conversion overhead)
- Handle nullable previous_chain_hash for migration safety
- Tenant isolation enforced at all boundaries
- TODO: Migrate to stable transaction IDs (txid) in future milestone
"""

from typing import Optional, List, Dict, Any
from storage.postgres.db import get_connection
from storage.ids.rid import encode_rid, decode_rid


def get_receipt(
    tenant_id: str,
    rid: str,  # TODO: Change to txid: str in future milestone
    dsn: str
) -> Optional[Dict[str, Any]]:
    """
    Retrieve a single receipt by receipt RID (NOT audit event RID, NOT stable txid).
    
    CRITICAL: This is the receipt's database PK (receipts.rid), which is a separate
    IDENTITY sequence from audit_events.rid. Do not confuse receipt ordering with
    event ordering.
    
    TODO: Future milestone will use stable transaction ID (txid) instead of receipt RID.
    
    Args:
        tenant_id: Tenant identifier (isolation boundary)
        rid: Base-60 encoded RECEIPT ID (receipts.rid, temporary until txid implemented)
        dsn: PostgreSQL connection string
        
    Returns:
        Receipt dict with RID, chain_hash, receipt_digest, timestamp, or None if not found
    """
    rid_int = decode_rid(rid)
    
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT rid, chain_hash, receipt_digest, commit_time, previous_chain_hash
                FROM receipts
                WHERE tenant_id = %s AND rid = %s
            """, (tenant_id, rid_int))
            
            row = cur.fetchone()
            if not row:
                return None
            
            return {
                "rid": encode_rid(row[0]),
                "chain_hash": row[1],
                "receipt_digest": row[2],
                "commit_time": row[3].isoformat(),
                "previous_chain_hash": row[4]  # May be None for migrated rows
            }


def list_audit_events(
    tenant_id: str,
    dsn: str,
    limit: int = 100,
    cursor: Optional[int] = None  # Integer cursor (not Base-60 string)
) -> Dict[str, Any]:
    """
    List audit events with integer cursor-based pagination.
    
    Uses integer RID ordering for correct chronological sequence.
    Returns integer next_cursor for efficiency (no Base-60 conversion).
    
    Args:
        tenant_id: Tenant identifier (isolation boundary)
        dsn: PostgreSQL connection string
        limit: Maximum events to return (default 100)
        cursor: Integer RID to start after (for pagination)
        
    Returns:
        Dict with "events" list and optional "next_cursor" integer
    """
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            if cursor is None:
                # First page: start from beginning
                cur.execute("""
                    SELECT rid, event_type, timestamp, payload_json
                    FROM audit_events
                    WHERE tenant_id = %s
                    ORDER BY rid ASC
                    LIMIT %s
                """, (tenant_id, limit + 1))  # Fetch limit+1 to detect next page
            else:
                # Subsequent page: start after cursor
                cur.execute("""
                    SELECT rid, event_type, timestamp, payload_json
                    FROM audit_events
                    WHERE tenant_id = %s AND rid > %s
                    ORDER BY rid ASC
                    LIMIT %s
                """, (tenant_id, cursor, limit + 1))
            
            rows = cur.fetchall()
            
            # Check if there's a next page
            has_next = len(rows) > limit
            if has_next:
                rows = rows[:limit]
            
            events = []
            for row in rows:
                rid_int, event_type, timestamp, payload_json = row
                events.append({
                    "rid": encode_rid(rid_int),
                    "event_type": event_type,
                    "timestamp": timestamp.isoformat() if timestamp else None,
                    **payload_json  # Unpack full event payload
                })
            
            result = {"events": events}
            
            # If there's a next page, return the last RID as integer cursor
            if has_next and events:
                # Use the integer RID from the last row
                last_rid_int = rows[-1][0]
                result["next_cursor"] = last_rid_int
            
            return result


def verify_chain(
    tenant_id: str,
    dsn: str,
    start_rid: Optional[str] = None,  # TODO: Change to txid in future
    end_rid: Optional[str] = None      # TODO: Change to txid in future
) -> Dict[str, Any]:
    """
    Verify chain hash integrity for a tenant's receipt chain.
    
    Handles nullable previous_chain_hash for migration safety:
    - If previous_chain_hash is NULL, skips backward link check
    - Recomputes chain hash from (previous_chain_hash, receipt_digest)
    - Compares with stored chain_hash
    
    TODO: Migrate to stable transaction IDs (txid) for range parameters.
    
    Args:
        tenant_id: Tenant identifier (isolation boundary)
        dsn: PostgreSQL connection string
        start_rid: Base-60 RID to start verification (None = from beginning)
        end_rid: Base-60 RID to end verification (None = to current head)
        
    Returns:
        Dict with:
        - valid: bool (True if all hashes match)
        - verified_count: int (number of receipts verified)
        - first_error: Optional dict with rid, expected, actual if mismatch found
    """
    from storage.crypto.chain import compute_chain_hash
    
    start_rid_int = decode_rid(start_rid) if start_rid else None
    end_rid_int = decode_rid(end_rid) if end_rid else None
    
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Build query with optional RID range
            query = """
                SELECT rid, chain_hash, receipt_digest, previous_chain_hash
                FROM receipts
                WHERE tenant_id = %s
            """
            params: List[Any] = [tenant_id]
            
            if start_rid_int is not None:
                query += " AND rid >= %s"
                params.append(start_rid_int)
            
            if end_rid_int is not None:
                query += " AND rid <= %s"
                params.append(end_rid_int)
            
            query += " ORDER BY rid ASC"
            
            cur.execute(query, params)
            rows = cur.fetchall()
            
            if not rows:
                return {
                    "valid": True,
                    "verified_count": 0,
                    "first_error": None
                }
            
            verified_count = 0
            prev_hash = None
            
            for idx, row in enumerate(rows):
                rid_int, stored_chain_hash, receipt_digest, prev_chain_hash_from_db = row
                
                # Handle nullable previous_chain_hash (for migrated rows)
                if prev_chain_hash_from_db is not None:
                    # New rows have column populated
                    effective_prev_hash = prev_chain_hash_from_db
                elif idx == 0:
                    # First receipt in range with NULL prev_hash: compute genesis or fetch prior
                    if start_rid_int is None or start_rid_int == 1:
                        # True genesis (first receipt for tenant)
                        from storage.crypto.chain import get_genesis_hash
                        effective_prev_hash = get_genesis_hash(tenant_id)
                    else:
                        # Mid-chain start: fetch previous receipt's chain_hash
                        cur.execute("""
                            SELECT chain_hash FROM receipts
                            WHERE tenant_id = %s AND rid < %s
                            ORDER BY rid DESC LIMIT 1
                        """, (tenant_id, rid_int))
                        prev_row = cur.fetchone()
                        effective_prev_hash = prev_row[0] if prev_row else get_genesis_hash(tenant_id)
                else:
                    # Subsequent receipt with NULL: use previous receipt's chain_hash from iteration
                    effective_prev_hash = prev_hash
                
                # Recompute chain hash
                expected_chain_hash = compute_chain_hash(effective_prev_hash, receipt_digest)
                
                # Compare
                if expected_chain_hash != stored_chain_hash:
                    return {
                        "valid": False,
                        "verified_count": verified_count,
                        "first_error": {
                            "rid": encode_rid(rid_int),
                            "expected": expected_chain_hash,
                            "actual": stored_chain_hash
                        }
                    }
                
                verified_count += 1
                prev_hash = stored_chain_hash
            
            return {
                "valid": True,
                "verified_count": verified_count,
                "first_error": None
            }
