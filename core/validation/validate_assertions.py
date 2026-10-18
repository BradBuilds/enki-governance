"""
Assertion Instance Validator

Validates assertion instances against:
- assertion_instance.schema.json
- Identity Pack vocabulary (scopes)
- Assertion Vocabulary Pack (predicates, object kinds)

This module provides structure and validation only.
No storage, conflict resolution, or enforcement.

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
    """Result of assertion validation"""
    valid: bool
    errors: List[ValidationError]
    
    @property
    def sorted_errors(self) -> List[ValidationError]:
        """Return errors in deterministic sorted order"""
        return sorted(self.errors)


class AssertionValidator:
    """Validates assertion instances against canonical vocabulary"""
    
    def __init__(self, identity_pack, assertion_vocab_pack):
        """
        Initialize validator with required vocabulary packs
        
        Args:
            identity_pack: PackLoadResult from GDU (must be ok=True)
            assertion_vocab_pack: PackLoadResult from GDU (must be ok=True)
        """
        self.identity_pack = identity_pack
        self.assertion_vocab_pack = assertion_vocab_pack
        
        # Load schema
        schema_path = Path(__file__).parent.parent.parent / "schemas" / "assertion_instance.schema.json"
        with open(schema_path, 'r') as f:
            self.schema = json.load(f)
        self.validator = Draft7Validator(self.schema)
        
        # Extract vocabulary
        self._load_vocabularies()
    
    def _load_vocabularies(self):
        """Extract vocabulary from loaded packs"""
        # Get scopes from Identity Pack
        scopes_def = self.identity_pack.index.by_id.get("identity.scopes")
        if scopes_def:
            scope_levels = scopes_def.content.get("scope_levels", [])
            self.valid_scopes = {scope["name"] for scope in scope_levels}
        else:
            self.valid_scopes = {"local", "shared", "global"}  # Fallback
        
        # Get predicates from Assertion Vocabulary Pack
        predicates_def = self.assertion_vocab_pack.index.by_id.get("assertion_vocab.predicates")
        if predicates_def:
            predicates = predicates_def.content.get("predicates", [])
            self.valid_predicates = {pred["predicate_id"]: pred for pred in predicates}
        else:
            self.valid_predicates = {}
        
        # Get object kinds from Assertion Vocabulary Pack
        object_kinds_def = self.assertion_vocab_pack.index.by_id.get("assertion_vocab.object_kinds")
        if object_kinds_def:
            object_kinds = object_kinds_def.content.get("object_kinds", [])
            self.valid_object_kinds = {kind["kind_id"] for kind in object_kinds}
        else:
            self.valid_object_kinds = set()
    
    def validate_assertion(self, assertion: Dict[str, Any], emit_events: bool = True) -> ValidationResult:
        """
        Validate a single assertion instance
        
        Args:
            assertion: Assertion instance dictionary
            emit_events: Whether to emit audit events (default True for Phase 2)
            
        Returns:
            ValidationResult with valid flag and any errors
        """
        errors = []
        assertion_id = assertion.get("assertion_id", "unknown")
        
        # 1. Schema validation
        schema_errors = list(self.validator.iter_errors(assertion))
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
                    event_type=EventTypes.ASSERTION_REJECTED,
                    severity=Severity.WARNING,
                    scope=Scope.LOCAL,
                    actor_id="validator",
                    assertion_id=assertion_id,
                    metadata={
                        "reason": "schema_validation_failed",
                        "error_count": len(errors),
                        "first_error": errors[0].message if errors else None
                    }
                )
            return ValidationResult(valid=False, errors=errors)
        
        # 2. Predicate validation
        predicate = assertion.get("predicate")
        if predicate not in self.valid_predicates:
            errors.append(ValidationError(
                code="INVALID_PREDICATE",
                message=f"Predicate '{predicate}' not found in assertion vocabulary",
                field="predicate"
            ))
        
        # 3. Object kind validation
        obj = assertion.get("object", {})
        obj_kind = obj.get("kind")
        if obj_kind not in self.valid_object_kinds:
            errors.append(ValidationError(
                code="INVALID_OBJECT_KIND",
                message=f"Object kind '{obj_kind}' not found in assertion vocabulary",
                field="object.kind"
            ))
        
        # 4. Predicate-object kind compatibility
        if predicate in self.valid_predicates and obj_kind in self.valid_object_kinds:
            predicate_def = self.valid_predicates[predicate]
            allowed_kinds = predicate_def.get("allowed_object_kinds", [])
            if obj_kind not in allowed_kinds:
                errors.append(ValidationError(
                    code="OBJECT_KIND_NOT_ALLOWED",
                    message=f"Predicate '{predicate}' does not allow object kind '{obj_kind}'. Allowed: {', '.join(allowed_kinds)}",
                    field="object.kind"
                ))
        
        # Emit appropriate event
        if emit_events:
            if errors:
                emit_event(
                    event_type=EventTypes.ASSERTION_REJECTED,
                    severity=Severity.WARNING,
                    scope=Scope.LOCAL,
                    actor_id="validator",
                    assertion_id=assertion_id,
                    metadata={
                        "reason": "validation_failed",
                        "error_count": len(errors),
                        "error_codes": [e.code for e in errors]
                    }
                )
            else:
                emit_event(
                    event_type=EventTypes.ASSERTION_VALIDATED,
                    severity=Severity.INFO,
                    scope=Scope.LOCAL,
                    actor_id="validator",
                    assertion_id=assertion_id,
                    metadata={
                        "predicate": predicate,
                        "object_kind": obj_kind
                    }
                )
        
        return ValidationResult(valid=len(errors) == 0, errors=errors)
    
    def validate_batch(self, assertions: List[Dict[str, Any]], emit_events: bool = True) -> ValidationResult:
        """
        Validate multiple assertion instances
        
        Args:
            assertions: List of assertion instance dictionaries
            emit_events: Whether to emit audit events (default True for Phase 2)
            
        Returns:
            ValidationResult with aggregated results
        """
        all_errors = []
        for idx, assertion in enumerate(assertions):
            result = self.validate_assertion(assertion, emit_events=False)  # Don't emit per-assertion events in batch mode
            # Add assertion index to error messages for batch context
            for error in result.errors:
                error.message = f"Assertion {idx}: {error.message}"
            all_errors.extend(result.errors)
        
        if emit_events:
            valid_count = len([a for a in assertions if len(self.validate_assertion(a, emit_events=False).errors) == 0])
            invalid_count = len(assertions) - valid_count
            emit_event(
                event_type=EventTypes.VALIDATION_COMPLETED,
                severity=Severity.INFO,
                scope=Scope.LOCAL,
                actor_id="validator",
                metadata={
                    "validation_type": "assertion_batch",
                    "total_count": len(assertions),
                    "valid_count": valid_count,
                    "invalid_count": invalid_count
                }
            )
        
        return ValidationResult(valid=len(all_errors) == 0, errors=all_errors)


# Module-level convenience functions
def validate_assertion_instance(assertion: Dict[str, Any], emit_events: bool = True) -> ValidationResult:
    """
    Validate a single assertion instance (convenience function)
    
    Args:
        assertion: Assertion instance dictionary
        emit_events: Whether to emit audit events (default True)
        
    Returns:
        ValidationResult with valid flag and any errors
    """
    gdu = get_gdu()
    identity_result = gdu.load_pack("identity", Path("libraries/identity"))
    if not identity_result.ok:
        raise RuntimeError(f"Failed to load identity pack: {identity_result.error}")
    
    assertion_vocab_result = gdu.load_pack("assertionvocab", Path("libraries/assertionvocab"))
    if not assertion_vocab_result.ok:
        raise RuntimeError(f"Failed to load assertionvocab pack: {assertion_vocab_result.error}")
    
    validator = AssertionValidator(identity_result, assertion_vocab_result)
    return validator.validate_assertion(assertion, emit_events)


def validate_assertion_batch(assertions: List[Dict[str, Any]], emit_events: bool = True) -> ValidationResult:
    """
    Validate multiple assertion instances (convenience function)
    
    Args:
        assertions: List of assertion instance dictionaries
        emit_events: Whether to emit audit events (default True)
        
    Returns:
        ValidationResult with aggregated results
    """
    gdu = get_gdu()
    identity_result = gdu.load_pack("identity", Path("libraries/identity"))
    if not identity_result.ok:
        raise RuntimeError(f"Failed to load identity pack: {identity_result.error}")
    
    assertion_vocab_result = gdu.load_pack("assertionvocab", Path("libraries/assertionvocab"))
    if not assertion_vocab_result.ok:
        raise RuntimeError(f"Failed to load assertionvocab pack: {assertion_vocab_result.error}")
    
    validator = AssertionValidator(identity_result, assertion_vocab_result)
    return validator.validate_batch(assertions, emit_events)
