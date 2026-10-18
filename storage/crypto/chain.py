"""
Chain hash computation per Phase 1 storage contract.
"""

import hashlib
from canonicaljson import encode_canonical_json


def get_genesis_hash(tenant_id: str) -> str:
    """
    Compute genesis chain hash per Phase 1 spec.
    
    Genesis: SHA256("ENKI_GENESIS_" || tenant_id)
    
    Args:
        tenant_id: Tenant identifier
    
    Returns:
        64-character lowercase hex SHA-256 hash
    """
    genesis_string = f"ENKI_GENESIS_{tenant_id}"
    return hashlib.sha256(genesis_string.encode('utf-8')).hexdigest()


def compute_receipt_digest(receipt_core: dict) -> str:
    """
    Compute receipt digest from canonical receipt core.
    
    IMPORTANT: receipt_core MUST exclude:
    - chain_hash (computed AFTER digest)
    - previous_chain_hash (not part of receipt)
    - receipt_id (DB-assigned)
    - any RIDs (DB-assigned, not content)
    - commit_time (metadata, not canonical content)
    
    Args:
        receipt_core: Canonical receipt dict (submission_time, counts, CIDs, reasons)
    
    Returns:
        64-character lowercase hex SHA-256 hash
    """
    canonical_bytes = encode_canonical_json(receipt_core)
    return hashlib.sha256(canonical_bytes).hexdigest()


def compute_chain_hash(prev_hash: str, receipt_digest: str) -> str:
    """
    Compute next chain hash per Phase 1 spec.
    
    chain_hash_n = SHA256(chain_hash_{n-1} || receipt_digest)
    
    Args:
        prev_hash: Previous chain hash (or genesis hash)
        receipt_digest: Current receipt digest
    
    Returns:
        64-character lowercase hex SHA-256 hash
    """
    combined = prev_hash + receipt_digest
    return hashlib.sha256(combined.encode('utf-8')).hexdigest()
