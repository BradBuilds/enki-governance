"""
LDU-T Validators

Validates that raw ingestion maintains mechanical constraints:
- Schema conformance
- Provenance requirement
- Resolution = null enforcement
- No semantic interference
"""

import json
from datetime import datetime
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field

try:
    import jsonschema
    from jsonschema import Draft7Validator, ValidationError
except ImportError:
    raise ImportError(
        "jsonschema library required. Install with: pip install jsonschema"
    )


@dataclass
class ValidationResult:
    """Result of validation check."""
    is_valid: bool
    schema_errors: List[str] = field(default_factory=list)
    constraint_errors: List[str] = field(default_factory=list)
    provenance_errors: List[str] = field(default_factory=list)
    semantic_warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            'is_valid': self.is_valid,
            'schema_errors': self.schema_errors,
            'constraint_errors': self.constraint_errors,
            'provenance_errors': self.provenance_errors,
            'semantic_warnings': self.semantic_warnings
        }


def load_ldu_t_schema() -> Dict[str, Any]:
    """
    Load LDU-T schema from file.
    
    ACTUAL REQUIREMENT: The file schemas/ldu_t_entry.schema.json must exist
    and be valid JSON Schema (draft-07).
    
    Raises:
        FileNotFoundError: If schema file doesn't exist
        ValueError: If schema JSON is invalid
    """
    try:
        from pathlib import Path
        schema_path = Path(__file__).parent.parent.parent / 'schemas' / 'ldu_t_entry.schema.json'
        with open(schema_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        raise FileNotFoundError(
            "schemas/ldu_t_entry.schema.json not found. "
            "This file is required for LDU-T validation. "
            "See PHASE_1_LDU_T_BUILD_GUIDE.md for schema definition."
        )
    except json.JSONDecodeError:
        raise ValueError("schemas/ldu_t_entry.schema.json is not valid JSON")


def validate_ldu_t_entry_schema(entry: Dict[str, Any]) -> ValidationResult:
    """
    Validate entry against LDU-T JSON schema.
    
    Args:
        entry: Entry dict to validate
    
    Returns:
        ValidationResult with schema_errors populated if invalid
    """
    result = ValidationResult(is_valid=True)
    
    try:
        schema = load_ldu_t_schema()
        validator = Draft7Validator(schema)
        
        errors = list(validator.iter_errors(entry))
        if errors:
            result.is_valid = False
            result.schema_errors = [
                f"$.{'.'.join(str(p) for p in err.absolute_path)}: {err.message}"
                for err in errors
            ]
    except Exception as e:
        result.is_valid = False
        result.schema_errors = [f"Schema validation error: {str(e)}"]
    
    return result


def validate_ldu_t_constraints(entry: Dict[str, Any]) -> ValidationResult:
    """
    Validate hard constraints:
    - universe must be "LDU-T"
    - resolution must be null
    - source_type must be in enum
    - ambiguity_flags must exist
    
    Args:
        entry: Entry dict to validate
    
    Returns:
        ValidationResult with constraint_errors populated if invalid
    """
    result = ValidationResult(is_valid=True)
    
    # Check universe
    if entry.get('universe') != 'LDU-T':
        result.is_valid = False
        result.constraint_errors.append(
            f"universe must be 'LDU-T', got {entry.get('universe')}"
        )
    
    # Check resolution is null
    if entry.get('resolution') is not None:
        result.is_valid = False
        result.constraint_errors.append(
            f"resolution must be null (no meaning decided at LDU-T), got {entry.get('resolution')}"
        )
    
    # Check source_type
    valid_source_types = ['book', 'speech', 'api_event', 'corpus', 'other']
    source_type = entry.get('source', {}).get('type')
    if source_type not in valid_source_types:
        result.is_valid = False
        result.constraint_errors.append(
            f"source.type must be one of {valid_source_types}, got {source_type}"
        )
    
    # Check ambiguity_flags exists
    if 'ambiguity_flags' not in entry:
        result.is_valid = False
        result.constraint_errors.append("ambiguity_flags is required")
    elif not isinstance(entry.get('ambiguity_flags'), list):
        result.is_valid = False
        result.constraint_errors.append("ambiguity_flags must be an array")
    
    return result


def validate_ldu_t_provenance(provenance: Dict[str, Any]) -> ValidationResult:
    """
    Validate provenance structure:
    - ingestion_timestamp must be valid ISO-8601
    - ingestion_method must be non-empty string
    - ingestion_actor must be non-empty string
    
    Args:
        provenance: Provenance dict from entry
    
    Returns:
        ValidationResult with provenance_errors populated if invalid
    """
    result = ValidationResult(is_valid=True)
    
    # Check timestamp
    timestamp = provenance.get('ingestion_timestamp')
    if not timestamp:
        result.is_valid = False
        result.provenance_errors.append("ingestion_timestamp is required")
    else:
        try:
            datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            result.is_valid = False
            result.provenance_errors.append(
                f"ingestion_timestamp must be ISO-8601, got {timestamp}"
            )
    
    # Check method
    method = provenance.get('ingestion_method')
    if not method or not isinstance(method, str):
        result.is_valid = False
        result.provenance_errors.append("ingestion_method must be non-empty string")
    
    # Check actor
    actor = provenance.get('ingestion_actor')
    if not actor or not isinstance(actor, str):
        result.is_valid = False
        result.provenance_errors.append("ingestion_actor must be non-empty string")
    
    return result


def validate_ldu_t_no_semantic_interference(entry: Dict[str, Any]) -> ValidationResult:
    """
    Semantic Non-Interference Check:
    - Warn if entry contains any resolved semantic meaning
    - Warn if entry references GDU definitions
    - DO NOT REJECT (accept anyway), but flag for review
    
    Args:
        entry: Entry dict to check
    
    Returns:
        ValidationResult with semantic_warnings (but is_valid = True)
    """
    result = ValidationResult(is_valid=True)
    
    # Check for semantic relations (should only be in LDU-W, not LDU-T)
    if 'semantic_relations' in entry:
        result.semantic_warnings.append(
            "Entry contains semantic_relations (should be in LDU-W, not LDU-T). "
            "Entry will be accepted, but flagged for review."
        )
    
    # Check for GDU references
    content_str = json.dumps(entry.get('content', {}))
    if 'language.semantics' in content_str or 'language.pragmatics' in content_str:
        result.semantic_warnings.append(
            "Entry content references GDU definitions. "
            "Ensure this is correct (example analysis, not interpretation). "
            "Entry will be accepted."
        )
    
    # Check for confidence/meaning resolution
    if 'meaning' in entry or 'interpretation' in entry:
        result.semantic_warnings.append(
            "Entry contains 'meaning' or 'interpretation' field. "
            "Ensure this is preserved ambiguity, not resolved meaning. "
            "Entry will be accepted."
        )
    
    return result


def validate_ldu_t_full(entry: Dict[str, Any]) -> ValidationResult:
    """
    Run all validation checks in sequence.
    
    Args:
        entry: Entry dict to validate
    
    Returns:
        Combined ValidationResult
    """
    # Schema validation (first gate)
    schema_result = validate_ldu_t_entry_schema(entry)
    if not schema_result.is_valid:
        return schema_result
    
    # Constraint validation
    constraint_result = validate_ldu_t_constraints(entry)
    if not constraint_result.is_valid:
        return constraint_result
    
    # Provenance validation
    provenance_result = validate_ldu_t_provenance(entry.get('provenance', {}))
    if not provenance_result.is_valid:
        return provenance_result
    
    # Semantic non-interference check (warnings only, doesn't fail)
    semantic_result = validate_ldu_t_no_semantic_interference(entry)
    
    # Combine results
    result = ValidationResult(is_valid=True)
    if semantic_result.semantic_warnings:
        result.semantic_warnings = semantic_result.semantic_warnings
    
    return result
