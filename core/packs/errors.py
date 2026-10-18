"""
ENKI Definition Pack Loader - Error Codes

Explicit error codes for pack validation and loading.
All errors are machine-readable and testable.
"""

# Manifest-level errors
PACK_MANIFEST_MISSING = "PACK_MANIFEST_MISSING"
PACK_MANIFEST_INVALID_JSON = "PACK_MANIFEST_INVALID_JSON"
PACK_MANIFEST_INVALID_SCHEMA = "PACK_MANIFEST_INVALID_SCHEMA"
PACK_PATH_NOT_FOUND = "PACK_PATH_NOT_FOUND"
PACK_PATH_NOT_DIRECTORY = "PACK_PATH_NOT_DIRECTORY"

# Definition-level errors
DEFINITION_FILE_MISSING = "DEFINITION_FILE_MISSING"
DEFINITION_INVALID_JSON = "DEFINITION_INVALID_JSON"
DEFINITION_SCHEMA_INVALID = "DEFINITION_SCHEMA_INVALID"
DEFINITION_MISSING_REQUIRED_FIELD = "DEFINITION_MISSING_REQUIRED_FIELD"

# Semantic validation errors
DUPLICATE_DEFINITION_ID = "DUPLICATE_DEFINITION_ID"
DUPLICATE_DEFINITION_NAME = "DUPLICATE_DEFINITION_NAME"
PACK_EMPTY = "PACK_EMPTY"
INVALID_STATUS_VALUE = "INVALID_STATUS_VALUE"

# Spine-level errors (S1.0)
PACK_NOT_IN_ACTIVE_SET = "PACK_NOT_IN_ACTIVE_SET"

# GDU Strict Mode Errors (LOCKED - GDU Technical Build Plan v2.4.0)
STRICT_MODE_REQUIRES_ACTIVE_SET = "STRICT_MODE_REQUIRES_ACTIVE_SET"
PACK_VERSION_MISMATCH = "PACK_VERSION_MISMATCH"
PACK_HASH_MISMATCH = "PACK_HASH_MISMATCH"

# Spine S1.1 - Capability System
CAPABILITY_RESOLUTION_PACK_ID = "__spine_capability_resolution__"

# Resolution errors (cross-pack, use consumer_pack_id)
MISSING_CAPABILITY = "MISSING_CAPABILITY"
AMBIGUOUS_CAPABILITY = "AMBIGUOUS_CAPABILITY"
CAPABILITY_VERSION_MISMATCH = "CAPABILITY_VERSION_MISMATCH"

# Validation errors (single-pack, use pack_id)
INVALID_CAPABILITY_DECLARATION = "INVALID_CAPABILITY_DECLARATION"
DUPLICATE_CAPABILITY_DECLARATION = "DUPLICATE_CAPABILITY_DECLARATION"

# Hash verification (pack_id only)
PACK_HASH_MISMATCH = "PACK_HASH_MISMATCH"

# Spine S1.2 - Namespace Registry
NAMESPACE_COLLISION = "NAMESPACE_COLLISION"
NAMESPACE_OWNERSHIP_VIOLATION = "NAMESPACE_OWNERSHIP_VIOLATION"
NAMESPACE_INVALID_FORMAT = "NAMESPACE_INVALID_FORMAT"
DEFINITION_WITHOUT_NAMESPACE = "DEFINITION_WITHOUT_NAMESPACE"

# Spine S1.5 - Activation Gate
ACTIVATION_REQUIRED = "ACTIVATION_REQUIRED"
ACTIVATION_TRANSCRIPT_MISMATCH = "ACTIVATION_TRANSCRIPT_MISMATCH"

# Schema loading errors
SCHEMA_FILE_MISSING = "SCHEMA_FILE_MISSING"
SCHEMA_INVALID_JSON = "SCHEMA_INVALID_JSON"

# Warning codes (non-fatal)
DEPRECATED_DEFINITION = "DEPRECATED_DEFINITION"
EMPTY_REFERENCES = "EMPTY_REFERENCES"
MISSING_DESCRIPTION = "MISSING_DESCRIPTION"


# Error messages
ERROR_MESSAGES = {
    PACK_MANIFEST_MISSING: "pack.json manifest file not found",
    PACK_MANIFEST_INVALID_JSON: "pack.json is not valid JSON",
    PACK_MANIFEST_INVALID_SCHEMA: "pack.json does not match required schema",
    PACK_PATH_NOT_FOUND: "Pack path does not exist",
    PACK_PATH_NOT_DIRECTORY: "Pack path is not a directory",
    
    DEFINITION_FILE_MISSING: "Definition file not found",
    DEFINITION_INVALID_JSON: "Definition file is not valid JSON",
    DEFINITION_SCHEMA_INVALID: "Definition does not match schema",
    DEFINITION_MISSING_REQUIRED_FIELD: "Definition missing required field",
    
    DUPLICATE_DEFINITION_ID: "Duplicate definition ID found",
    DUPLICATE_DEFINITION_NAME: "Duplicate definition name found",
    PACK_EMPTY: "Pack contains no definitions",
    INVALID_STATUS_VALUE: "Invalid status value in definition",
    
    PACK_NOT_IN_ACTIVE_SET: "Pack not permitted by active set (spine enforcement)",
    STRICT_MODE_REQUIRES_ACTIVE_SET: "Strict mode requires active set",
    PACK_VERSION_MISMATCH: "Pack version does not match active set declaration",
    PACK_HASH_MISMATCH: "Pack content hash does not match active set declaration",
    
    MISSING_CAPABILITY: "Required capability not found",
    AMBIGUOUS_CAPABILITY: "Multiple providers for capability",
    CAPABILITY_VERSION_MISMATCH: "Capability version mismatch",
    INVALID_CAPABILITY_DECLARATION: "Invalid capability declaration",
    DUPLICATE_CAPABILITY_DECLARATION: "Duplicate capability declaration",
    PACK_HASH_MISMATCH: "Pack hash mismatch",
    
    NAMESPACE_COLLISION: "Namespace collision between packs",
    NAMESPACE_OWNERSHIP_VIOLATION: "Definition outside owned namespace",
    NAMESPACE_INVALID_FORMAT: "Invalid namespace format",
    DEFINITION_WITHOUT_NAMESPACE: "Definition requires namespace ownership",
    
    ACTIVATION_REQUIRED: "Activation record required",
    ACTIVATION_TRANSCRIPT_MISMATCH: "Activation transcript hash mismatch",
    
    SCHEMA_FILE_MISSING: "Schema file not found",
    SCHEMA_INVALID_JSON: "Schema file is not valid JSON",
    
    DEPRECATED_DEFINITION: "Definition is marked as deprecated",
    EMPTY_REFERENCES: "Definition has empty references list",
    MISSING_DESCRIPTION: "Definition missing description field"
}


def get_error_message(code: str) -> str:
    """Get human-readable message for error code."""
    return ERROR_MESSAGES.get(code, f"Unknown error code: {code}")
