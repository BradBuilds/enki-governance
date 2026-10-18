"""
Content ID (CID) computation per Phase 1 storage contract.
"""

import hashlib
from canonicaljson import encode_canonical_json


def compute_cid(audit_event: dict) -> str:
    """
    Compute Content ID per Phase 1 rules.
    
    Process:
    1. Remove storage-assigned fields (rid, commit_time, receipt_id, chain_hash)
    2. Canonicalize JSON per RFC 8785 (using canonicaljson library)
    3. SHA-256 hash of canonical bytes
    4. Return lowercase hexadecimal (64 characters)
    
    Args:
        audit_event: AuditEvent payload dict
    
    Returns:
        64-character lowercase hex SHA-256 hash
    """
    # Create copy without storage-assigned fields
    event_copy = dict(audit_event)
    storage_fields = ['rid', 'commit_time', 'receipt_id', 'chain_hash']
    for field in storage_fields:
        event_copy.pop(field, None)
    
    # Canonicalize using RFC 8785 (JSON Canonicalization Scheme)
    canonical_bytes = encode_canonical_json(event_copy)
    
    # SHA-256 hash
    cid_hash = hashlib.sha256(canonical_bytes).hexdigest()
    
    return cid_hash
