"""
Audit Event Instance Validator

Validates audit event instances against:
- audit_event_instance.schema.json
- Audit Event Vocabulary Pack (event types, severity)
- Identity Pack vocabulary (scopes)

This module provides structure and validation only.
No storage, processing, or side effects.

Phase 2: Emits audit events on validation outcomes.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass

import jsonschema
from jsonschema import Draft7Validator

from core.gdu.bootstrap import get_gdu
from core.events import emit_event, EventTypes, Severity, Scope


@dataclass
class ValidationError:
    """Represents a validation error"""
    code: str
    message: str
    field: Optional[str] = None
    
    def __lt__(self, other):
        """Enable sorting for deterministic output"""
        return (self.code, self.field or "", self.message) < (other.code, other.field or "", other.message)


@dataclass
class ValidationResult:
    """Result of audit event validation"""
    valid: bool
    errors: List[ValidationError]
    
    @property
    def sorted_errors(self) -> List[ValidationError]:
        """Return errors in deterministic sorted order"""
        return sorted(self.errors)


class AuditEventValidator:
    """Validates audit event instances against canonical vocabulary"""
    
    def __init__(self, audit_vocab_pack, identity_pack):
        """
        Initialize validator with required vocabulary packs
        
        Args:
            audit_vocab_pack: PackLoadResult from GDU (must be ok=True)
            identity_pack: PackLoadResult from GDU (must be ok=True)
        """
        self.audit_vocab_pack = audit_vocab_pack
        self.identity_pack = identity_pack
        
        # Load schema
        schema_path = Path(__file__).parent.parent.parent / "schemas" / "audit_event_instance.schema.json"
        with open(schema_path, 'r') as f:
            self.schema = json.load(f)
        self.validator = Draft7Validator(self.schema)
        
        # Extract vocabulary
        self._load_vocabularies()
    
    def _load_vocabularies(self):
        """Extract vocabulary from loaded packs"""
        # Get event types from Audit Vocabulary Pack
        event_types_def = self.audit_vocab_pack.index.by_id.get("audit_vocab.event_types")
        if event_types_def:
            event_types = event_types_def.content.get("event_types", [])
            self.valid_event_types = {evt["event_type_id"] for evt in event_types}
        else:
            self.valid_event_types = set()
        
        # Get severity levels from Audit Vocabulary Pack
        severity_def = self.audit_vocab_pack.index.by_id.get("audit_vocab.severity")
        if severity_def:
            severity_levels = severity_def.content.get("severity_levels", [])
            self.valid_severity_levels = {sev["severity_id"] for sev in severity_levels}
        else:
            self.valid_severity_levels = {"info", "warning", "error", "critical"}  # Fallback
        
        # Get scopes from Identity Pack
        scopes_def = self.identity_pack.index.by_id.get("identity.scopes")
        if scopes_def:
            scope_levels = scopes_def.content.get("scope_levels", [])
            self.valid_scopes = {scope["name"] for scope in scope_levels}
        else:
            self.valid_scopes = {"local", "shared", "global"}  # Fallback
    
    def validate_event(self, event: Dict[str, Any], emit_events: bool = True) -> ValidationResult:
        """
        Validate a single audit event instance
        
        Args:
            event: Audit event instance dictionary
            emit_events: Whether to emit audit events (default True for Phase 2)
            
        Returns:
            ValidationResult with valid flag and any errors
        """
        errors = []
        event_id = event.get("event_id", "unknown")
        
        # 1. Schema validation
        schema_errors = list(self.validator.iter_errors(event))
        for error in schema_errors:
            field = ".".join(str(p) for p in error.path) if error.path else None
            errors.append(ValidationError(
                code="SCHEMA_ERROR",
                message=error.message,
                field=field
            ))
        
        # If schema invalid, stop here (other checks may fail unpredictably)
        if errors:
            if emit_events:
                emit_event(
                    event_type=EventTypes.AUDIT_REJECTED,
                    severity=Severity.WARNING,
                    scope=Scope.LOCAL,
                    actor_id="validator",
                    metadata={
                        "rejected_event_id": event_id,
                        "reason": "schema_validation_failed",
                        "error_count": len(errors),
                        "first_error": errors[0].message if errors else None
                    }
                )
            return ValidationResult(valid=False, errors=errors)
        
        # 2. Event type validation
        event_type = event.get("event_type")
        if event_type not in self.valid_event_types:
            errors.append(ValidationError(
                code="INVALID_EVENT_TYPE",
                message=f"Event type '{event_type}' not found in audit vocabulary",
                field="event_type"
            ))
        
        # 3. Severity validation (already enforced by schema enum, but validate against vocab)
        severity = event.get("severity")
        if severity not in self.valid_severity_levels:
            errors.append(ValidationError(
                code="INVALID_SEVERITY",
                message=f"Severity '{severity}' not found in audit vocabulary. Valid: {', '.join(sorted(self.valid_severity_levels))}",
                field="severity"
            ))
        
        # 4. Scope validation (already enforced by schema enum, but validate against vocab)
        scope = event.get("scope")
        if scope not in self.valid_scopes:
            errors.append(ValidationError(
                code="INVALID_SCOPE",
                message=f"Scope '{scope}' not valid. Must be one of: {', '.join(sorted(self.valid_scopes))}",
                field="scope"
            ))
        
        # Emit appropriate event
        if emit_events:
            if errors:
                emit_event(
                    event_type=EventTypes.AUDIT_REJECTED,
                    severity=Severity.WARNING,
                    scope=Scope.LOCAL,
                    actor_id="validator",
                    metadata={
                        "rejected_event_id": event_id,
                        "reason": "validation_failed",
                        "error_count": len(errors),
                        "error_codes": [e.code for e in errors]
                    }
                )
            else:
                emit_event(
                    event_type=EventTypes.AUDIT_VALIDATED,
                    severity=Severity.INFO,
                    scope=Scope.LOCAL,
                    actor_id="validator",
                    metadata={
                        "validated_event_id": event_id,
                        "event_type": event_type
                    }
                )
        
        return ValidationResult(valid=len(errors) == 0, errors=errors)
    
    def validate_batch(self, events: List[Dict[str, Any]], emit_events: bool = True) -> ValidationResult:
        """
        Validate multiple audit event instances
        
        Args:
            events: List of audit event instance dictionaries
            emit_events: Whether to emit audit events (default True for Phase 2)
            
        Returns:
            ValidationResult with aggregated results
        """
        all_errors = []
        for idx, event in enumerate(events):
            result = self.validate_event(event, emit_events=False)  # Don't emit per-event events in batch mode
            # Add event index to error messages for batch context
            for error in result.errors:
                error.message = f"Event {idx}: {error.message}"
            all_errors.extend(result.errors)
        
        if emit_events:
            valid_count = len([e for e in events if len(self.validate_event(e, emit_events=False).errors) == 0])
            invalid_count = len(events) - valid_count
            emit_event(
                event_type=EventTypes.VALIDATION_COMPLETED,
                severity=Severity.INFO,
                scope=Scope.LOCAL,
                actor_id="validator",
                metadata={
                    "validation_type": "audit_event_batch",
                    "total_count": len(events),
                    "valid_count": valid_count,
                    "invalid_count": invalid_count
                }
            )
        
        return ValidationResult(valid=len(all_errors) == 0, errors=all_errors)


# Module-level convenience functions
def validate_audit_event_instance(event: Dict[str, Any], emit_events: bool = True) -> ValidationResult:
    """
    Validate a single audit event instance (convenience function)
    
    Loads packs from GDU automatically.
    
    Args:
        event: Audit event instance dictionary
        emit_events: Whether to emit audit events (default True)
        
    Returns:
        ValidationResult with valid flag and any errors
        
    Raises:
        RuntimeError: If required packs not in GDU
    """
    gdu = get_gdu()
    
    # Load required packs from GDU
    audit_vocab_result = gdu.load_pack("auditvocab", Path("libraries/auditvocab"))
    identity_result = gdu.load_pack("identity", Path("libraries/identity"))
    
    if not audit_vocab_result.ok:
        raise RuntimeError(f"Failed to load auditvocab pack: {audit_vocab_result.error}")
    if not identity_result.ok:
        raise RuntimeError(f"Failed to load identity pack: {identity_result.error}")
    
    validator = AuditEventValidator(audit_vocab_result, identity_result)
    return validator.validate_event(event, emit_events)


def validate_audit_event_batch(events: List[Dict[str, Any]], emit_events: bool = True) -> ValidationResult:
    """
    Validate multiple audit event instances (convenience function)
    
    Loads packs from GDU automatically.
    
    Args:
        events: List of audit event instance dictionaries
        emit_events: Whether to emit audit events (default True)
        
    Returns:
        ValidationResult with aggregated results
        
    Raises:
        RuntimeError: If required packs not in GDU
    """
    gdu = get_gdu()
    
    # Load required packs from GDU
    audit_vocab_result = gdu.load_pack("auditvocab", Path("libraries/auditvocab"))
    identity_result = gdu.load_pack("identity", Path("libraries/identity"))
    
    if not audit_vocab_result.ok:
        raise RuntimeError(f"Failed to load auditvocab pack: {audit_vocab_result.error}")
    if not identity_result.ok:
        raise RuntimeError(f"Failed to load identity pack: {identity_result.error}")
    
    validator = AuditEventValidator(audit_vocab_result, identity_result)
    return validator.validate_batch(events, emit_events)
