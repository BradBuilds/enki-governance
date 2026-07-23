"""
LDU-T Ingestion Pipeline

Main entry point for accepting raw linguistic data without interpretation.

Pipeline:
1. Parse input safely (no crashes on malformed JSON)
2. For each entry: validate schema, constraints, provenance
3. Add system provenance (timestamp, method, actor)
4. Batch insert accepted entries
5. Return receipt (what was accepted, what was rejected)

IMPORTANT: This module requires ENKI_DB_DSN environment variable.
See storage/config.py for details.
"""

import json
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from uuid import uuid4

from storage.ldu_t.adapter import LDUTAdapter
from storage.ldu_t.validators import (
    validate_ldu_t_full,
    ValidationResult
)


def ingest_ldu_t(
    raw_input: str,
    ingestion_method: str,
    ingestion_actor: str,
    tenant_id: str = "default"
) -> Dict[str, Any]:
    """
    Main entry point: ingest raw JSON or JSONL as LDU-T entries.
    
    ACTUAL REQUIREMENTS:
    - raw_input can be single JSON object or JSONL (multiple objects, newline-separated)
    - ingestion_method must be version-specific (e.g., 'book_parser_v1.2.0')
    - ingestion_actor must identify who/what triggered ingestion
    - tenant_id for multi-tenancy
    
    Args:
        raw_input: JSON or JSONL string
        ingestion_method: Tool/version that produced this ingestion
        ingestion_actor: User ID / service name / automation ID
        tenant_id: Multi-tenant identifier
    
    Returns:
        Receipt dict with detailed results
    """
    adapter = LDUTAdapter(tenant_id=tenant_id)
    
    # Parse input
    entries = []
    parse_errors = []
    
    lines = raw_input.strip().split('\n')
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        
        try:
            entry = json.loads(line)
            entries.append(entry)
        except json.JSONDecodeError as e:
            parse_errors.append({
                "line": idx,
                "error": str(e)
            })
    
    if parse_errors:
        return {
            "status": "parse_error",
            "parse_errors": parse_errors,
            "entries_attempted": len(entries),
            "entries_accepted": 0,
            "entries_rejected": len(entries) + len(parse_errors)
        }
    
    # Add provenance to each entry
    now = datetime.now(timezone.utc).isoformat()
    for entry in entries:
        if 'provenance' not in entry:
            entry['provenance'] = {}
        
        entry['provenance']['ingestion_timestamp'] = now
        entry['provenance']['ingestion_method'] = ingestion_method
        entry['provenance']['ingestion_actor'] = ingestion_actor
        entry['provenance']['tenant_id'] = tenant_id
    
    # Batch insert
    return adapter.batch_insert(
        entries,
        ingestion_method=ingestion_method,
        ingestion_actor=ingestion_actor,
        validate=True
    )


def ingest_book_text(
    text: str,
    title: str,
    author: str,
    page: int,
    **kwargs
) -> Dict[str, Any]:
    """
    Convenience function: ingest text from a book.
    
    Args:
        text: Raw text from book
        title: Book title
        author: Book author
        page: Page number
        **kwargs: Additional source metadata (isbn, chapter, edition, etc.)
                 Also: ingestion_method, ingestion_actor, tenant_id
    
    Returns:
        Receipt dict
    """
    entry = {
        "id": f"ldu_t_book_{kwargs.get('isbn', 'unknown')}_{page}_{str(uuid4())[:8]}",
        "universe": "LDU-T",
        "source": {
            "type": "book",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "book": {
                "title": title,
                "author": author,
                "page": page,
                **{k: v for k, v in kwargs.items() if k in ['isbn', 'chapter', 'edition']}
            }
        },
        "content": {
            "text": text,
            "language_detected": kwargs.get('language', 'en'),
            "encoding": "utf-8"
        },
        "ambiguity_flags": kwargs.get('ambiguity_flags', []),
        "resolution": None
    }
    
    return ingest_ldu_t(
        json.dumps(entry),
        ingestion_method=kwargs.get('ingestion_method', 'book_parser_v1.0.0'),
        ingestion_actor=kwargs.get('ingestion_actor', 'system'),
        tenant_id=kwargs.get('tenant_id', 'default')
    )


def ingest_speech_transcription(
    transcription: str,
    recording_id: str,
    speaker_id: str,
    **kwargs
) -> Dict[str, Any]:
    """
    Convenience function: ingest speech transcription.
    
    Args:
        transcription: Speech-to-text output
        recording_id: Unique recording identifier
        speaker_id: Speaker identifier
        **kwargs: Additional metadata (timestamp_start, timestamp_end, duration_ms, etc.)
                 Also: ingestion_method, ingestion_actor, tenant_id
    
    Returns:
        Receipt dict
    """
    entry = {
        "id": f"ldu_t_speech_{recording_id}_{speaker_id}_{str(uuid4())[:8]}",
        "universe": "LDU-T",
        "source": {
            "type": "speech",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "speech": {
                "recording_id": recording_id,
                "speaker_id": speaker_id,
                **{k: v for k, v in kwargs.items() 
                   if k in ['timestamp_start', 'timestamp_end', 'duration_ms', 
                           'transcription_method', 'transcription_confidence']}
            }
        },
        "content": {
            "transcription": transcription
        },
        "ambiguity_flags": kwargs.get('ambiguity_flags', []),
        "resolution": None
    }
    
    return ingest_ldu_t(
        json.dumps(entry),
        ingestion_method=kwargs.get('ingestion_method', 'speech_to_text_v2.1.0'),
        ingestion_actor=kwargs.get('ingestion_actor', 'system'),
        tenant_id=kwargs.get('tenant_id', 'default')
    )


def ingest_api_event(
    message: str,
    endpoint: str,
    method: str,
    request_id: str,
    requester_id: str,
    **kwargs
) -> Dict[str, Any]:
    """
    Convenience function: ingest API event message.
    
    Args:
        message: Text content of the message
        endpoint: API endpoint
        method: HTTP method (GET, POST, etc.)
        request_id: Unique request identifier
        requester_id: User/service making request
        **kwargs: Additional metadata (requester_role, etc.)
                 Also: ingestion_method, ingestion_actor, tenant_id
    
    Returns:
        Receipt dict
    """
    entry = {
        "id": f"ldu_t_api_{request_id}_{str(uuid4())[:8]}",
        "universe": "LDU-T",
        "source": {
            "type": "api_event",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "api_event": {
                "endpoint": endpoint,
                "method": method,
                "request_id": request_id,
                "requester_id": requester_id,
                **{k: v for k, v in kwargs.items() if k == 'requester_role'}
            }
        },
        "content": {
            "text": message
        },
        "ambiguity_flags": kwargs.get('ambiguity_flags', []),
        "resolution": None
    }
    
    return ingest_ldu_t(
        json.dumps(entry),
        ingestion_method=kwargs.get('ingestion_method', 'api_ingest_v1.0.0'),
        ingestion_actor=kwargs.get('ingestion_actor', 'api_gateway'),
        tenant_id=kwargs.get('tenant_id', 'default')
    )
