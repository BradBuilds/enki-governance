"""
ENKI Event Emission Module

Emits observability events from validators and tools to structured output.

Phase 2 Requirements (Option B):
- Emit ObservabilityEvent objects as JSON lines to stdout
- Events are for observability only (NOT AuditEvent instances)
- Deterministic event ordering within process
- No storage, no files, no network dependencies
- Schema conformance deferred to Phase 3 ingestion/mapping

This module provides observability WITHOUT requiring storage.
ObservabilityEvent ≠ AuditEvent (mapping happens in Phase 3).
"""

import json
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, asdict


@dataclass
class ObservabilityEvent:
    """
    Phase 2 observability event (NOT an AuditEvent instance)
    
    Emitted for observability purposes only. Schema conformance
    to audit_event_instance.schema.json is deferred to Phase 3.
    
    Required fields (MUST):
        event_id, event_type, timestamp, sequence, severity, scope, metadata
    
    Recommended fields (SHOULD):
        tenant_id, actor_id
    
    Optional fields (MAY):
        assertion_id, pack_id, node_id, target_id, and others
    """
    # MUST fields
    event_id: str
    event_type: str  # Dot notation: assertion.validated, pack.loaded, etc.
    timestamp: str  # ISO 8601 with timezone
    sequence: int  # Monotonically increasing counter
    severity: str  # info, warning, error, critical
    scope: str  # local, shared, global
    metadata: Optional[Dict[str, Any]] = None  # Arbitrary structured data
    
    # SHOULD fields
    tenant_id: Optional[str] = None
    actor_id: Optional[str] = None
    
    # MAY fields (context)
    assertion_id: Optional[str] = None
    pack_id: Optional[str] = None
    node_id: Optional[str] = None
    target_id: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary, excluding None values except for required fields"""
        data = asdict(self)
        # Keep all MUST fields even if None
        required_fields = {'event_id', 'event_type', 'timestamp', 'sequence', 'severity', 'scope', 'metadata'}
        return {k: v for k, v in data.items() if v is not None or k in required_fields}


class EventEmitter:
    """
    Emits observability events to structured output
    
    Phase 2: Emits ObservabilityEvent objects as JSON lines to stdout
    Phase 3: Will map to AuditEvent during ingestion
    """
    
    def __init__(self, output_stream=None):
        """
        Initialize event emitter
        
        Args:
            output_stream: Output stream (None = resolve sys.stdout at emit time)
        """
        self.output_stream = output_stream  # None means dynamic resolution
        self._event_counter = 0
    
    def emit(self, event: ObservabilityEvent) -> None:
        """
        Emit a single observability event
        
        Args:
            event: ObservabilityEvent to emit
            
        Raises:
            ValueError: If MUST fields are null (except metadata which MAY be null)
        """
        # Validate MUST fields are non-null (except metadata)
        must_non_null = ['event_id', 'event_type', 'timestamp', 'sequence', 'severity', 'scope']
        for field in must_non_null:
            value = getattr(event, field, None)
            if value is None:
                raise ValueError(f"ObservabilityEvent MUST field '{field}' cannot be null")
        
        # Serialize as JSON line (field order is implementation-defined)
        event_json = json.dumps(event.to_dict())
        
        # Resolve output stream (None = current sys.stdout)
        stream = self.output_stream if self.output_stream is not None else sys.stdout
        stream.write(event_json + '\n')
        stream.flush()
        
        self._event_counter += 1
    
    def emit_raw(
        self,
        event_type: str,
        severity: str,
        scope: str = "local",
        actor_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> None:
        """
        Emit an observability event with automatic timestamp, sequence, and ID generation
        
        Args:
            event_type: Dot notation event type (e.g., 'assertion.validated')
            severity: Event severity (info, warning, error, critical)
            scope: Visibility scope (local, shared, global)
            actor_id: Optional actor identifier
            metadata: Optional structured event metadata
            **kwargs: Additional event fields
        """
        # Increment counter and capture sequence
        self._event_counter += 1
        sequence = self._event_counter
        
        # Generate event ID
        event_id = self._generate_event_id_with_sequence(sequence)
        
        # Generate ISO 8601 timestamp with timezone
        timestamp = datetime.now(timezone.utc).isoformat()
        
        # Create event
        event = ObservabilityEvent(
            event_id=event_id,
            event_type=event_type,
            timestamp=timestamp,
            sequence=sequence,
            severity=severity,
            scope=scope,
            actor_id=actor_id,
            metadata=metadata,
            **kwargs
        )
        
        # Emit the event
        self.emit(event)
    
    def _generate_event_id_with_sequence(self, sequence: int) -> str:
        """
        Generate a unique event ID
        
        Format: event_{sequence}_{timestamp_ms}
        Note: This is a simple implementation for Phase 2.
        Phase 3 will use proper Base-60 IDs from storage.
        """
        timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        return f"event_{sequence}_{timestamp_ms}"
    
    def get_event_count(self) -> int:
        """Return number of events emitted"""
        return self._event_counter


# Global default emitter (lazy initialization)
_default_emitter = None


def _get_default_emitter() -> EventEmitter:
    """Get or create the default emitter (lazy singleton)"""
    global _default_emitter
    if _default_emitter is None:
        _default_emitter = EventEmitter()
    return _default_emitter


def emit_event(
    event_type: str,
    severity: str,
    scope: str = "local",
    actor_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    **kwargs
) -> None:
    """
    Convenience function to emit an observability event using the default emitter
    
    Phase 2: Emits ObservabilityEvent (not AuditEvent)
    
    Args:
        event_type: Dot notation event type
        severity: Event severity (info, warning, error, critical)
        scope: Visibility scope (local, shared, global)
        actor_id: Optional actor identifier
        metadata: Optional structured event metadata
        **kwargs: Additional event fields
    """
    _get_default_emitter().emit_raw(
        event_type=event_type,
        severity=severity,
        scope=scope,
        actor_id=actor_id,
        metadata=metadata,
        **kwargs
    )


def get_default_emitter() -> EventEmitter:
    """Get the default event emitter"""
    return _default_emitter


def set_default_emitter(emitter: EventEmitter) -> None:
    """Set the default event emitter (useful for testing)"""
    global _default_emitter
    _default_emitter = emitter


# Event type constants for common events
class EventTypes:
    """Standard ENKI event types (dot notation)"""
    
    # Assertion events
    ASSERTION_VALIDATED = "assertion.validated"
    ASSERTION_REJECTED = "assertion.rejected"
    ASSERTION_PROPOSED = "assertion.proposed"
    
    # Audit events
    AUDIT_VALIDATED = "audit.validated"
    AUDIT_REJECTED = "audit.rejected"
    
    # Pack events
    PACK_LOADED = "pack.loaded"
    PACK_VALIDATION_FAILED = "pack.validation_failed"
    PACK_LOAD_ERROR = "pack.load_error"
    
    # Usage/metering events
    USAGE_METER_TICK = "usage.meter_tick"
    
    # Validation events
    VALIDATION_STARTED = "validation.started"
    VALIDATION_COMPLETED = "validation.completed"
    VALIDATION_ERROR = "validation.error"


# Severity constants
class Severity:
    """Standard severity levels"""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


# Scope constants
class Scope:
    """Standard visibility scopes"""
    LOCAL = "local"
    SHARED = "shared"
    GLOBAL = "global"
