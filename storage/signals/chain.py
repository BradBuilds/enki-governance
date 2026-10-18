"""
Signal chain operations (FD-1: separate from audit chain).

The signal chain tracks ingested signals with receipts and chain hashes.
Cross-chain anchor: after each signal batch, an audit event is emitted
into the AUDIT chain containing the signal chain hash. This anchors
signal integrity into the governance chain.

Chain computation uses the same algorithm as the audit chain:
    genesis = SHA256("ENKI_SIGNAL_GENESIS_" || tenant_id || "_" || node_id)
    chain_hash = SHA256(prev_chain_hash || receipt_digest)
"""

import hashlib
import json
from typing import Optional

from canonicaljson import encode_canonical_json

from storage.postgres.db import get_connection


def get_signal_genesis_hash(tenant_id: str, node_id: str) -> str:
    """Compute genesis hash for a signal chain."""
    genesis = f"ENKI_SIGNAL_GENESIS_{tenant_id}_{node_id}"
    return hashlib.sha256(genesis.encode("utf-8")).hexdigest()


def compute_signal_receipt_digest(receipt_core: dict) -> str:
    """Compute receipt digest from canonical receipt core."""
    canonical = encode_canonical_json(receipt_core)
    return hashlib.sha256(canonical).hexdigest()


def compute_signal_chain_hash(prev_hash: str, receipt_digest: str) -> str:
    """Compute next signal chain hash."""
    combined = prev_hash + receipt_digest
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def get_signal_chain_head(
    tenant_id: str,
    node_id: str,
    dsn: Optional[str] = None,
) -> tuple[str, int]:
    """Get current signal chain head hash and signal count.
    Returns (chain_hash, signal_count). Uses genesis if no chain exists."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT current_chain_hash, signal_count
                FROM signal_chain_state
                WHERE tenant_id = %s AND node_id = %s
            """, (tenant_id, node_id))
            row = cur.fetchone()
            if row:
                return row[0], row[1]
            return get_signal_genesis_hash(tenant_id, node_id), 0


def commit_signal_batch(
    tenant_id: str,
    node_id: str,
    signal_rids: list[int],
    signal_cids: list[str],
    dsn: Optional[str] = None,
) -> dict:
    """
    Commit a batch of signals to the signal chain.

    1. Build receipt core from signal CIDs
    2. Compute receipt digest
    3. Get previous chain hash (or genesis)
    4. Compute new chain hash
    5. Store signal receipt
    6. Update signal chain state

    Returns receipt dict with chain_hash and receipt info.
    """
    if not signal_rids:
        return {"batch_size": 0, "chain_hash": None}

    receipt_core = {
        "tenant_id": tenant_id,
        "node_id": node_id,
        "batch_size": len(signal_rids),
        "signal_cids": sorted(signal_cids),
    }

    receipt_digest = compute_signal_receipt_digest(receipt_core)

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Get previous chain hash
            cur.execute("""
                SELECT current_chain_hash, signal_count
                FROM signal_chain_state
                WHERE tenant_id = %s AND node_id = %s
            """, (tenant_id, node_id))
            row = cur.fetchone()
            if row:
                prev_hash = row[0]
                prev_count = row[1]
            else:
                prev_hash = get_signal_genesis_hash(tenant_id, node_id)
                prev_count = 0

            # Compute new chain hash
            new_chain_hash = compute_signal_chain_hash(prev_hash, receipt_digest)

            # Store receipt
            cur.execute("""
                INSERT INTO signal_receipts
                    (tenant_id, node_id, batch_size, signal_rids,
                     receipt_digest, chain_hash, previous_chain_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING rid
            """, (
                tenant_id, node_id, len(signal_rids),
                signal_rids, receipt_digest, new_chain_hash, prev_hash,
            ))
            receipt_rid = cur.fetchone()[0]

            # Update chain state
            new_count = prev_count + len(signal_rids)
            cur.execute("""
                INSERT INTO signal_chain_state
                    (tenant_id, node_id, current_chain_hash,
                     last_receipt_rid, signal_count)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, node_id)
                DO UPDATE SET
                    current_chain_hash = EXCLUDED.current_chain_hash,
                    last_receipt_rid = EXCLUDED.last_receipt_rid,
                    signal_count = EXCLUDED.signal_count,
                    updated_at = NOW()
            """, (tenant_id, node_id, new_chain_hash, receipt_rid, new_count))

            conn.commit()

            return {
                "receipt_rid": receipt_rid,
                "receipt_digest": receipt_digest,
                "chain_hash": new_chain_hash,
                "previous_chain_hash": prev_hash,
                "batch_size": len(signal_rids),
                "signal_count": new_count,
            }
