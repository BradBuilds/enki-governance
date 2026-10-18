"""
Phase 3 Storage Implementation - Milestone 0

Provides ingestion of ObservabilityEvent JSONL into PostgreSQL storage
with validation, receipts, and chain hash computation.
"""

from storage.ingest.ingestion import ingest_jsonl

__all__ = ['ingest_jsonl']
