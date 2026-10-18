"""
LDU-W Validators

Validates workable definitions:
- Schema conformance
- Scope enforcement (all 4 fields required, non-empty)
- Lineage to LDU-T (source IDs must exist)
- GDU shape declaration (not auto-detected)
- No GDU authority claims

Note: No semantic rejection. Shape and lineage only.
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
    scope_errors: List[str] = field(default_factory=list)
    lineage_errors: List[str] = field(default_factory=list)
    gdu_conformance_errors: List[str] = field(default_factory=list)
    gdu_authority_errors: List[str] = field(default_factory=list)
    semantic_warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            'is_valid': self.is_valid,
            'schema_errors': self.schema_errors,
            'scope_errors': self.scope_errors,
            'lineage_errors': self.lineage_errors,
            'gdu_conformance_errors': self.gdu_conformance_errors,
            'gdu_authority_errors': self.gdu_authority_errors,
            'semantic_warnings': self.semantic_warnings
        }


def load_ldu_w_schema() -> Dict[str, Any]:
    """Load LDU-W schema from file."""
    try:
        from pathlib import Path
        schema_path = Path(__file__).parent.parent.parent / 'schemas' / 'ldu_w_definition.schema.json'
        with open(schema_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        raise FileNotFoundError(
            "schemas/ldu_w_definition.schema.json not found. "
            "This file is required for LDU-W validation."
        )
    except json.JSONDecodeError:
        raise ValueError("schemas/ldu_w_definition.schema.json is not valid JSON")


def validate_ldu_w_entry_schema(entry: Dict[str, Any]) -> ValidationResult:
    """
    Validate entry against LDU-W JSON schema.
    
    Args:
        entry: Entry dict to validate
    
    Returns:
        ValidationResult with schema_errors populated if invalid
    """
    result = ValidationResult(is_valid=True)
    
    try:
        schema = load_ldu_w_schema()
        validator = Draft7Validator(schema)
        
        errors = sorted(validator.iter_errors(entry), key=lambda e: e.message)
        if errors:
            result.is_valid = False
            result.schema_errors = [f"{e.json_path}: {e.message}" for e in errors]
    except Exception as e:
        result.is_valid = False
        result.schema_errors.append(f"Schema validation error: {str(e)}")
    
    return result


def validate_ldu_w_scope(entry: Dict[str, Any]) -> ValidationResult:
    """
    Validate scope fields (all required, non-empty, normalized).
    
    Args:
        entry: Entry dict to validate
    
    Returns:
        ValidationResult with scope_errors populated if invalid
    """
    result = ValidationResult(is_valid=True)
    scope = entry.get('scope')
    
    # Check scope exists
    if not scope or not isinstance(scope, dict):
        result.is_valid = False
        result.scope_errors.append("scope object is required")
        return result
    
    required_scope_fields = ['domain', 'culture', 'community', 'time_period']
    
    for field_name in required_scope_fields:
        value = scope.get(field_name)
        
        # Check exists
        if value is None:
            result.is_valid = False
            result.scope_errors.append(f"scope.{field_name} is required (cannot be null)")
            continue
        
        # Check is string
        if not isinstance(value, str):
            result.is_valid = False
            result.scope_errors.append(f"scope.{field_name} must be a string, got {type(value).__name__}")
            continue
        
        # Check non-empty
        if not value.strip():
            result.is_valid = False
            result.scope_errors.append(f"scope.{field_name} cannot be empty")
            continue
        
        # Check NFC normalized (no combining characters)
        try:
            import unicodedata
            nfc_value = unicodedata.normalize('NFC', value)
            if nfc_value != value:
                result.scope_errors.append(f"scope.{field_name} should be NFC-normalized (has combining characters)")
        except Exception as e:
            result.scope_errors.append(f"scope.{field_name} normalization error: {str(e)}")
    
    return result


def validate_ldu_w_source_refs(entry: Dict[str, Any], db_connection) -> ValidationResult:
    """
    Verify all source_ldu_t_ids exist in database.
    
    Args:
        entry: Entry dict to validate
        db_connection: PostgreSQL connection object
    
    Returns:
        ValidationResult with lineage_errors populated if invalid
    """
    result = ValidationResult(is_valid=True)
    
    source_ids = entry.get('source_ldu_t_ids', [])
    
    if not source_ids:
        result.is_valid = False
        result.lineage_errors.append("source_ldu_t_ids cannot be empty")
        return result
    
    if not isinstance(source_ids, list):
        result.is_valid = False
        result.lineage_errors.append(f"source_ldu_t_ids must be array, got {type(source_ids).__name__}")
        return result
    
    try:
        with db_connection.cursor() as cur:
            # Check each ID exists
            for source_id in source_ids:
                if not isinstance(source_id, str):
                    result.is_valid = False
                    result.lineage_errors.append(f"source_ldu_t_ids contains non-string: {source_id}")
                    continue
                
                cur.execute(
                    "SELECT id FROM ldu_t_entries WHERE id = %s",
                    (source_id,)
                )
                
                if not cur.fetchone():
                    result.is_valid = False
                    result.lineage_errors.append(f"source_ldu_t_ids references non-existent entry: {source_id}")
    except Exception as e:
        result.is_valid = False
        result.lineage_errors.append(f"Error checking lineage: {str(e)}")
    
    return result


def validate_ldu_w_gdu_conformance(entry: Dict[str, Any]) -> ValidationResult:
    """
    Check that definition matches declared GDU shape.
    
    Args:
        entry: Entry dict to validate
    
    Returns:
        ValidationResult with gdu_conformance_errors populated if invalid
    """
    result = ValidationResult(is_valid=True)
    
    gdu_shape = entry.get('gdu_shape', {})
    
    # Check structure is declared
    structure = gdu_shape.get('structure')
    if not structure:
        result.is_valid = False
        result.gdu_conformance_errors.append("gdu_shape.structure is required (simple_definition, hierarchical, parametric, contextual)")
    elif structure not in ['simple_definition', 'hierarchical', 'parametric', 'contextual']:
        result.is_valid = False
        result.gdu_conformance_errors.append(f"gdu_shape.structure '{structure}' not recognized")
    
    # Check target_universe is GDU
    target = gdu_shape.get('target_universe')
    if target != 'GDU':
        result.is_valid = False
        result.gdu_conformance_errors.append(f"gdu_shape.target_universe must be 'GDU', got '{target}'")
    
    # Validate claim exists (not just null)
    claim = entry.get('claim')
    if not claim:
        result.is_valid = False
        result.gdu_conformance_errors.append("claim object is required (the workable definition)")
    elif isinstance(claim, dict):
        claim_text = claim.get('text')
        if not claim_text:
            result.is_valid = False
            result.gdu_conformance_errors.append("claim.text is required (actual definition)")
    
    return result


def validate_ldu_w_no_gdu_write(entry: Dict[str, Any]) -> ValidationResult:
    """
    HARD RULE: Reject any entry claiming GDU authority.
    
    LDU-W is local and workable, NEVER canonical or promoted.
    
    Args:
        entry: Entry dict to validate
    
    Returns:
        ValidationResult with gdu_authority_errors populated if invalid
    """
    result = ValidationResult(is_valid=True)
    
    # Check for "promoted" claim
    if entry.get('promoted') is True:
        result.is_valid = False
        result.gdu_authority_errors.append("LDU-W definitions cannot be marked as promoted (GDU-only)")
    
    # Check for "admitted_to_gdu" claim
    if entry.get('admitted_to_gdu') is True:
        result.is_valid = False
        result.gdu_authority_errors.append("LDU-W definitions cannot claim GDU admission (local scope only)")
    
    # Check for "canonical_status" claim
    canonical_status = entry.get('canonical_status')
    if canonical_status and canonical_status != 'local':
        result.is_valid = False
        result.gdu_authority_errors.append(f"LDU-W definitions cannot have canonical_status='{canonical_status}' (local only)")
    
    # Check for signature field
    if entry.get('signature') or entry.get('signed_by'):
        result.is_valid = False
        result.gdu_authority_errors.append("LDU-W definitions cannot include cryptographic signatures (not authority-backed)")
    
    # Check that canonical_resolution is null or missing
    canonical_res = entry.get('canonical_resolution')
    if canonical_res is not None:
        result.is_valid = False
        result.gdu_authority_errors.append("LDU-W canonical_resolution must be null (never touched at this layer)")
    
    # Check for "resolution" field (should not exist in LDU-W)
    if 'resolution' in entry and entry.get('resolution') is not None:
        result.is_valid = False
        result.gdu_authority_errors.append("LDU-W should not have a non-null resolution field (workable claim only)")
    
    return result


def validate_ldu_w_full(entry: Dict[str, Any], db_connection) -> ValidationResult:
    """
    Run all validation checks in sequence.
    
    Args:
        entry: Entry dict to validate
        db_connection: PostgreSQL connection object
    
    Returns:
        Combined ValidationResult
    """
    # Schema validation (first gate)
    schema_result = validate_ldu_w_entry_schema(entry)
    if not schema_result.is_valid:
        return schema_result
    
    # Scope validation
    scope_result = validate_ldu_w_scope(entry)
    if not scope_result.is_valid:
        return scope_result
    
    # Lineage validation (requires DB)
    lineage_result = validate_ldu_w_source_refs(entry, db_connection)
    if not lineage_result.is_valid:
        return lineage_result
    
    # GDU conformance
    gdu_conform_result = validate_ldu_w_gdu_conformance(entry)
    if not gdu_conform_result.is_valid:
        return gdu_conform_result
    
    # GDU authority block (hard rule)
    gdu_auth_result = validate_ldu_w_no_gdu_write(entry)
    if not gdu_auth_result.is_valid:
        return gdu_auth_result
    
    # All checks passed
    result = ValidationResult(is_valid=True)
    return result
