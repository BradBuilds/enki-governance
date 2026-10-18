from __future__ import annotations

from typing import Optional, List
from pydantic import BaseModel


class ErrorEnvelope(BaseModel):
    error_code: str
    detail: str


class Receipt(BaseModel):
    rid: str
    chain_hash: str
    receipt_digest: str
    commit_time: str
    previous_chain_hash: Optional[str] = None


class AuditEvent(BaseModel):
    rid: str
    event_type: str
    timestamp: Optional[str] = None

    class Config:
        extra = "allow"


class AuditEventsResponse(BaseModel):
    events: List[AuditEvent]
    next_cursor: Optional[int] = None


class ChainError(BaseModel):
    rid: str
    expected: str
    actual: str


class ChainVerifyResponse(BaseModel):
    valid: bool
    verified_count: int
    first_error: Optional[ChainError] = None


class AssertionsResponse(BaseModel):
    events: List[AuditEvent]


class ArtifactMetadata(BaseModel):
    cid: str
    byte_length: int
    content_type: Optional[str] = None
