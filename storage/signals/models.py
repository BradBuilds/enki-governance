"""
Pydantic models for the signal pipeline.

These models validate data BEFORE it touches the database.
They are the Python-side contract that matches schema_v2.sql.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field


class LduTSignal(BaseModel):
    """Raw truth entry. Immutable once created. Never rejected."""
    ldu_t_id: str
    tenant_id: str
    node_id: str
    device_id: str
    source_type: str
    raw_bytes: bytes
    raw_sha256: str
    raw_byte_length: int
    content_type: Optional[str] = None
    format_detected: Optional[str] = None
    detection_confidence: Optional[float] = None


class Signal(BaseModel):
    """Normalized signal (LDU-W level). Validated against GDU vocabulary."""
    signal_id: str
    tenant_id: str
    node_id: str
    device_id: str
    identity_id: Optional[str] = None
    signal_type: str                              # from signalvocab
    source_domain: str                            # from signal_sources
    severity: str = "informational"               # from signal_severity
    confidence: str = "unverified"                # from signal_confidence
    timestamp: datetime                           # event time (from source)
    server_time: Optional[datetime] = None        # set by server on insert
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    altitude: Optional[float] = None
    accuracy: Optional[float] = None
    payload: dict                                 # full parsed content
    observables: list = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    ldu_t_id: str                                 # lineage to raw truth
    ldu_p_id: Optional[str] = None                # lineage to LDU-P if staged
    cid: str                                      # SHA-256 content hash
    origin_node_id: str                           # where signal originated
    federation_id: Optional[str] = None
    route_path: list = Field(default_factory=list)
    device_signature: Optional[str] = None


class LduPEntry(BaseModel):
    """Provisional entry. Unknown format staged for review."""
    ldu_p_id: str
    tenant_id: str
    ldu_t_id: str
    format_detected: Optional[str] = None
    detection_confidence: Optional[float] = None
    parser_used: Optional[str] = None
    extracted_structure: dict
    proposed_mapping: dict
    proposed_signal_type: Optional[str] = None
    vocabulary_gaps: list = Field(default_factory=list)
    resolution: str = "pending_review"
    resolution_path: Optional[str] = None
    resolved_by: Optional[str] = None
    resolved_at: Optional[datetime] = None
    promoted_signal_rid: Optional[int] = None


class SignalReceipt(BaseModel):
    """Batch receipt for signal chain (FD-1)."""
    tenant_id: str
    node_id: str
    batch_size: int
    signal_rids: list[int]
    receipt_digest: str
    chain_hash: str
    previous_chain_hash: str


class Observable(BaseModel):
    """An identifier extracted from a signal."""
    observable_type: str              # from observable_types vocabulary
    observable_value: str             # raw value
    normalized_value: str             # canonical form for matching
    source_field: Optional[str] = None  # which field in the payload
