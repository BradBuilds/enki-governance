"""
ENKI Schema Validation

Validates JSON files against ENKI schemas.

This module provides schema validation for:
- Definitions
- Assertions
- Audit events
- Registry packs

Author: ENKI System
Version: 0.1.0
"""

import json
import os
from pathlib import Path
from typing import Dict, List, Tuple
import jsonschema
from jsonschema import validate, ValidationError


def load_schema(schema_name: str, schemas_dir: str = None) -> Dict:
    """
    Load a JSON schema from the schemas directory.
    
    Args:
        schema_name: Name of schema file (e.g., "definition.schema.json")
        schemas_dir: Path to schemas directory (auto-detected if None)
        
    Returns:
        Schema dictionary
        
    Raises:
        FileNotFoundError: If schema file not found
        json.JSONDecodeError: If schema is invalid JSON
    """
    if schemas_dir is None:
        # Auto-detect schemas directory
        current_dir = Path(__file__).parent.parent.parent
        schemas_dir = current_dir / "schemas"
    else:
        schemas_dir = Path(schemas_dir)
    
    schema_path = schemas_dir / schema_name
    
    if not schema_path.exists():
        raise FileNotFoundError(f"Schema not found: {schema_path}")
    
    with open(schema_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def validate_json_file(file_path: str, schema: Dict) -> Tuple[bool, List[str]]:
    """
    Validate a JSON file against a schema.
    
    Args:
        file_path: Path to JSON file to validate
        schema: JSON schema dictionary
        
    Returns:
        Tuple of (success: bool, errors: List[str])
    """
    errors = []
    
    # Check file exists
    if not os.path.exists(file_path):
        return False, [f"File not found: {file_path}"]
    
    # Load JSON file
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return False, [f"Invalid JSON: {e}"]
    except Exception as e:
        return False, [f"Error reading file: {e}"]
    
    # Validate against schema
    try:
        validate(instance=data, schema=schema)
        return True, []
    except ValidationError as e:
        return False, [f"Schema validation failed: {e.message}"]
    except Exception as e:
        return False, [f"Validation error: {e}"]


def validate_definition(file_path: str) -> Dict:
    """
    Validate a definition file.
    
    Args:
        file_path: Path to definition JSON file
        
    Returns:
        Dictionary with ok, errors, and optional warnings
    """
    schema = load_schema("definition.schema.json")
    ok, errors = validate_json_file(file_path, schema)
    
    return {
        "ok": ok,
        "errors": errors,
        "warnings": []
    }


def validate_registry_pack(pack_path: str) -> Dict:
    """
    Validate a registry pack and all its definitions.
    
    Args:
        pack_path: Path to pack.json file or directory containing it
        
    Returns:
        Dictionary with ok, errors, and warnings
    """
    errors = []
    warnings = []
    
    # Determine pack file path
    pack_path = Path(pack_path)
    if pack_path.is_dir():
        pack_file = pack_path / "pack.json"
    else:
        pack_file = pack_path
        pack_path = pack_file.parent
    
    # Validate pack.json exists
    if not pack_file.exists():
        return {
            "ok": False,
            "errors": [f"pack.json not found at {pack_file}"],
            "warnings": []
        }
    
    # Load and validate pack.json schema
    pack_schema = load_schema("registry_pack.schema.json")
    ok, pack_errors = validate_json_file(str(pack_file), pack_schema)
    if not ok:
        errors.extend(pack_errors)
        return {"ok": False, "errors": errors, "warnings": warnings}
    
    # Load pack data
    with open(pack_file, 'r', encoding='utf-8') as f:
        pack_data = json.load(f)
    
    # Track definition IDs for duplicate checking
    definition_ids = set()
    
    # Validate each definition file
    definition_schema = load_schema("definition.schema.json")
    for def_entry in pack_data.get("definitions", []):
        def_id = def_entry.get("id")
        def_path = def_entry.get("path")
        
        # Check for duplicate IDs
        if def_id in definition_ids:
            errors.append(f"DUPLICATE_ID: Definition ID '{def_id}' appears multiple times")
        else:
            definition_ids.add(def_id)
        
        # Validate definition file exists
        full_def_path = pack_path / def_path
        if not full_def_path.exists():
            errors.append(f"MISSING_DEFINITION: {def_path} not found")
            continue
        
        # Validate definition file
        ok, def_errors = validate_json_file(str(full_def_path), definition_schema)
        if not ok:
            for err in def_errors:
                errors.append(f"{def_id}: {err}")
        else:
            # Check that definition ID matches
            with open(full_def_path, 'r', encoding='utf-8') as f:
                def_data = json.load(f)
                file_id = def_data.get("id")
                if file_id != def_id:
                    warnings.append(f"{def_id}: Definition file has ID '{file_id}' but pack references '{def_id}'")
    
    # Validate version format (should be caught by schema, but double-check)
    version = pack_data.get("version", "")
    if not _is_valid_semver(version):
        errors.append(f"VERSION_FORMAT: Invalid version '{version}' (must be semantic version like 1.0.0)")
    
    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings
    }


def _is_valid_semver(version: str) -> bool:
    """Check if version string is valid semantic version (major.minor.patch)."""
    import re
    pattern = r'^\d+\.\d+\.\d+$'
    return bool(re.match(pattern, version))


if __name__ == "__main__":
    # Demonstration
    print("ENKI Schema Validation Demo\n")
    
    # Try to validate the base60 pack
    base_dir = Path(__file__).parent.parent.parent
    pack_dir = base_dir / "libraries" / "base60"
    
    if pack_dir.exists():
        print(f"Validating Base-60 pack at: {pack_dir}\n")
        result = validate_registry_pack(str(pack_dir))
        
        print(f"Validation: {'✓ PASSED' if result['ok'] else '✗ FAILED'}")
        
        if result['errors']:
            print("\nErrors:")
            for err in result['errors']:
                print(f"  - {err}")
        
        if result['warnings']:
            print("\nWarnings:")
            for warn in result['warnings']:
                print(f"  - {warn}")
        
        if result['ok'] and not result['warnings']:
            print("\nAll validations passed! ✓")
    else:
        print(f"Base-60 pack not found at {pack_dir}")
