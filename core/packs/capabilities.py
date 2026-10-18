"""
ENKI Spine S1.1 — Pack Capability Validation & Resolution

Validates capability declarations and resolves requirements.
Per SPINE-01: exact version matching, deterministic errors.

Authority: SPINE-00 v1.0.1, SPINE-01, SPINE-ROADMAP v1.1.0
"""

import re
from typing import List, Dict, Set, Tuple, Optional, Any
from dataclasses import dataclass
from core.packs.types import PackManifest, PackError, ErrorSeverity
from core.packs import errors


# Locked regex patterns (SPINE-01)
CAPABILITY_NAME_PATTERN = re.compile(r'^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9]*)*$')
VERSION_PATTERN = re.compile(r'^\d+\.\d+\.\d+$')


@dataclass
class ValidationError:
    """Validation error (single-pack concern)."""
    code: str
    message: str
    context: Dict[str, Any]  # Must include: pack_id, field


@dataclass
class ResolutionError:
    """Resolution error (cross-pack concern)."""
    code: str
    message: str
    context: Dict[str, Any]  # Must include: consumer_pack_id, capability


def validate_capabilities(manifests: List[PackManifest]) -> List[ValidationError]:
    """
    Validate capability declarations (collect-all).
    
    Checks:
    - Shape validation (lists, required keys, types)
    - Regex patterns (lowercase dot-path, semver)
    - Duplicate (capability, version) within same pack
    
    Behavior (LOCKED):
    Collects ALL validation failures across ALL manifests.
    Does NOT halt on first failure.
    
    Returns errors sorted by: pack_id, field, index/capability
    """
    errors_list = []
    
    for manifest in manifests:
        # Validate capabilities_provided
        errors_list.extend(_validate_field(
            manifest, 
            "capabilities_provided",
            manifest.capabilities_provided
        ))
        
        # Validate capabilities_required
        errors_list.extend(_validate_field(
            manifest,
            "capabilities_required", 
            manifest.capabilities_required
        ))
    
    # Sort deterministically (LOCKED ordering)
    errors_list.sort(key=_validation_error_sort_key)
    return errors_list


def resolve_capabilities(manifests: List[PackManifest]) -> List[ResolutionError]:
    """
    Resolve capability requirements (exact version match).
    
    Checks:
    - Missing capability (0 providers)
    - Ambiguous capability (>1 provider for same tuple)
    - Version mismatch (capability exists, wrong version)
    
    Returns errors with consumer_pack_id in context.
    """
    # Build provider index: (capability, version) -> [provider_pack_ids]
    provider_index = _build_provider_index(manifests)
    
    resolution_errors = []
    
    for manifest in manifests:
        if not manifest.capabilities_required:
            continue
            
        for req in manifest.capabilities_required:
            # Skip if not valid dict (validation already caught this)
            if not isinstance(req, dict):
                continue
                
            capability = req.get("capability", "")
            version = req.get("version", "")
            
            if not capability or not version:
                continue  # Validation error, skip
            
            key = (capability, version)
            providers = provider_index.get(key, [])
            
            if len(providers) == 0:
                # Check if capability exists with different version
                available = _get_available_versions(provider_index, capability)
                
                if available:
                    resolution_errors.append(ResolutionError(
                        code=errors.CAPABILITY_VERSION_MISMATCH,
                        message=f"Capability '{capability}' version {version} required by '{manifest.pack_id}' but no matching version found",
                        context={
                            "consumer_pack_id": manifest.pack_id,
                            "capability": capability,
                            "required_version": version,
                            "available_versions": available
                        }
                    ))
                else:
                    resolution_errors.append(ResolutionError(
                        code=errors.MISSING_CAPABILITY,
                        message=f"Capability '{capability}' version {version} required by '{manifest.pack_id}' but no provider found",
                        context={
                            "consumer_pack_id": manifest.pack_id,
                            "capability": capability,
                            "version": version
                        }
                    ))
            
            elif len(providers) > 1:
                resolution_errors.append(ResolutionError(
                    code=errors.AMBIGUOUS_CAPABILITY,
                    message=f"Capability '{capability}' version {version} required by '{manifest.pack_id}' has multiple providers",
                    context={
                        "consumer_pack_id": manifest.pack_id,
                        "capability": capability,
                        "version": version,
                        "providers": providers  # Already sorted
                    }
                ))
    
    return resolution_errors


def _validate_field(manifest: PackManifest, field_name: str, field_value: Any) -> List[ValidationError]:
    """Validate single capability field."""
    errors_list = []
    
    # Check if it's a list
    if not isinstance(field_value, list):
        errors_list.append(ValidationError(
            code=errors.INVALID_CAPABILITY_DECLARATION,
            message=f"Field '{field_name}' must be a list",
            context={
                "pack_id": manifest.pack_id,
                "field": field_name,
                "reason": "Field is not a list"
            }
        ))
        return errors_list
    
    # Track duplicates
    seen_tuples: Set[Tuple[str, str]] = set()
    
    for index, entry in enumerate(field_value):
        # Check if entry is a dict
        if not isinstance(entry, dict):
            errors_list.append(ValidationError(
                code=errors.INVALID_CAPABILITY_DECLARATION,
                message=f"Entry at index {index} in '{field_name}' is not an object",
                context={
                    "pack_id": manifest.pack_id,
                    "field": field_name,
                    "index": index,
                    "reason": "Entry is not an object"
                }
            ))
            continue
        
        # Check required fields
        capability = entry.get("capability")
        version = entry.get("version")
        
        if capability is None:
            errors_list.append(ValidationError(
                code=errors.INVALID_CAPABILITY_DECLARATION,
                message=f"Missing 'capability' field at index {index} in '{field_name}'",
                context={
                    "pack_id": manifest.pack_id,
                    "field": field_name,
                    "index": index,
                    "reason": "Missing required field 'capability'"
                }
            ))
            continue
        
        if version is None:
            errors_list.append(ValidationError(
                code=errors.INVALID_CAPABILITY_DECLARATION,
                message=f"Missing 'version' field at index {index} in '{field_name}'",
                context={
                    "pack_id": manifest.pack_id,
                    "field": field_name,
                    "index": index,
                    "reason": "Missing required field 'version'",
                    "capability": capability if isinstance(capability, str) else ""
                }
            ))
            continue
        
        # Check types
        if not isinstance(capability, str):
            errors_list.append(ValidationError(
                code=errors.INVALID_CAPABILITY_DECLARATION,
                message=f"Field 'capability' at index {index} must be a string",
                context={
                    "pack_id": manifest.pack_id,
                    "field": field_name,
                    "index": index,
                    "reason": "Field 'capability' is not a string"
                }
            ))
            continue
        
        if not isinstance(version, str):
            errors_list.append(ValidationError(
                code=errors.INVALID_CAPABILITY_DECLARATION,
                message=f"Field 'version' at index {index} must be a string",
                context={
                    "pack_id": manifest.pack_id,
                    "field": field_name,
                    "index": index,
                    "reason": "Field 'version' is not a string",
                    "capability": capability
                }
            ))
            continue
        
        # Validate capability pattern
        if not CAPABILITY_NAME_PATTERN.match(capability):
            errors_list.append(ValidationError(
                code=errors.INVALID_CAPABILITY_DECLARATION,
                message=f"Invalid capability name '{capability}' at index {index}",
                context={
                    "pack_id": manifest.pack_id,
                    "field": field_name,
                    "index": index,
                    "reason": "Capability name does not match pattern ^[a-z][a-z0-9]*(?:\\.[a-z][a-z0-9]*)*$",
                    "capability": capability,
                    "version": version
                }
            ))
            continue
        
        # Validate version pattern
        if not VERSION_PATTERN.match(version):
            errors_list.append(ValidationError(
                code=errors.INVALID_CAPABILITY_DECLARATION,
                message=f"Invalid version '{version}' at index {index}",
                context={
                    "pack_id": manifest.pack_id,
                    "field": field_name,
                    "index": index,
                    "reason": "Version does not match pattern ^\\d+\\.\\d+\\.\\d+$",
                    "capability": capability,
                    "version": version
                }
            ))
            continue
        
        # Check for duplicates
        key = (capability, version)
        if key in seen_tuples:
            errors_list.append(ValidationError(
                code=errors.DUPLICATE_CAPABILITY_DECLARATION,
                message=f"Duplicate capability '{capability}' version {version} in '{field_name}'",
                context={
                    "pack_id": manifest.pack_id,
                    "field": field_name,
                    "capability": capability,
                    "version": version
                }
            ))
        else:
            seen_tuples.add(key)
    
    return errors_list


def _build_provider_index(manifests: List[PackManifest]) -> Dict[Tuple[str, str], List[str]]:
    """
    Build (capability, version) -> [provider_ids] index.
    
    Uses set-based indexing internally for immunity to duplicates (defense in depth).
    Returns deterministically sorted lists.
    """
    # Use set-based indexing (immune to accidental duplicates)
    provider_index_set: Dict[Tuple[str, str], Set[str]] = {}
    
    for manifest in manifests:
        if not manifest.capabilities_provided:
            continue
            
        for entry in manifest.capabilities_provided:
            # Skip invalid entries (validation already caught these)
            if not isinstance(entry, dict):
                continue
            
            capability = entry.get("capability")
            version = entry.get("version")
            
            if not capability or not version:
                continue
            if not isinstance(capability, str) or not isinstance(version, str):
                continue
            
            key = (capability, version)
            if key not in provider_index_set:
                provider_index_set[key] = set()
            provider_index_set[key].add(manifest.pack_id)
    
    # Convert to sorted lists (deterministic)
    provider_index: Dict[Tuple[str, str], List[str]] = {}
    for key, providers_set in provider_index_set.items():
        provider_index[key] = sorted(providers_set)
    
    return provider_index


def _get_available_versions(provider_index: Dict[Tuple[str, str], List[str]], 
                            capability: str) -> List[Dict[str, str]]:
    """
    Get all available versions for a capability.
    
    Returns list sorted by (pack_id, version) for determinism.
    """
    available = []
    
    for (cap, ver), providers in provider_index.items():
        if cap == capability:
            for provider in providers:
                available.append({"pack_id": provider, "version": ver})
    
    # Sort by (pack_id, version)
    available.sort(key=lambda x: (x["pack_id"], x["version"]))
    return available


def _validation_error_sort_key(err: ValidationError) -> tuple:
    """
    Deterministic sort key for validation errors.
    
    Order (LOCKED):
    1. pack_id
    2. field
    3. index (for INVALID) or (capability, version) (for DUPLICATE)
    """
    pack_id = err.context.get("pack_id", "")
    field = err.context.get("field", "")
    
    # For INVALID errors: use index
    if "index" in err.context:
        return (pack_id, field, err.context["index"], "", "")
    
    # For DUPLICATE errors: use (capability, version)
    capability = err.context.get("capability", "")
    version = err.context.get("version", "")
    return (pack_id, field, 999999, capability, version)  # High index to sort after INVALID
