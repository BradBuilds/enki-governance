"""
Ceremony Gates - Fail-Closed Validation

Implements the 4 gates required for deterministic, secure pack admission.

Gate A: Pack Integrity (per-pack validation)
Gate B: Ceremony Determinism (transcript validation)
Gate C: Admission Compatibility (cross-pack validation)
Gate D: Runtime enforcement (handled by PackLoader strict=True)

Authority: GDU Technical Build Plan v2.4.0 §Phase 2.2
"""

from typing import List, Optional, Dict, Any
from pathlib import Path
import re

from core.spine.semver import SemanticVersion
from core.spine.validation_transcript import ValidationTranscript
from core.spine.active_set import ActiveSet


class GateError:
    """Structured gate error."""
    
    def __init__(self, gate: str, code: str, message: str, context: Dict[str, Any] = None):
        self.gate = gate
        self.code = code
        self.message = message
        self.context = context or {}
    
    def __repr__(self):
        return f"GateError(gate={self.gate}, code={self.code}, message={self.message})"
    
    def to_dict(self):
        return {
            "gate": self.gate,
            "code": self.code,
            "message": self.message,
            "context": self.context
        }


def gate_a_pack_integrity(pack_paths: Dict[str, Path]) -> List[GateError]:
    """
    Gate A: Pack Integrity
    
    Validates each pack's internal structure:
    - pack.json exists and is valid JSON
    - definitions/ directory exists
    - namespaces_owned is declared
    - No volatile fields (hostnames, temp paths, PIDs)
    
    Args:
        pack_paths: Dict mapping pack_id to filesystem path
    
    Returns:
        List of GateError objects (empty = pass)
    """
    import json
    
    errors = []
    
    for pack_id, pack_path in pack_paths.items():
        # Check pack.json exists
        pack_json_path = pack_path / "pack.json"
        if not pack_json_path.exists():
            errors.append(GateError(
                gate="A",
                code="PACK_JSON_MISSING",
                message=f"pack.json not found",
                context={"pack_id": pack_id, "path": str(pack_path)}
            ))
            continue
        
        # Load pack.json
        try:
            with open(pack_json_path, 'r', encoding='utf-8') as f:
                pack_data = json.load(f)
        except Exception as e:
            errors.append(GateError(
                gate="A",
                code="PACK_JSON_INVALID",
                message=f"pack.json is invalid JSON: {e}",
                context={"pack_id": pack_id}
            ))
            continue
        
        # Check required fields
        if "pack_id" not in pack_data:
            errors.append(GateError(
                gate="A",
                code="PACK_ID_MISSING",
                message="pack_id field missing",
                context={"pack_id": pack_id}
            ))
        
        if "version" not in pack_data:
            errors.append(GateError(
                gate="A",
                code="VERSION_MISSING",
                message="version field missing",
                context={"pack_id": pack_id}
            ))
        
        if "namespaces_owned" not in pack_data:
            errors.append(GateError(
                gate="A",
                code="NAMESPACES_OWNED_MISSING",
                message="namespaces_owned field missing (required for Gate A)",
                context={"pack_id": pack_id}
            ))
        
        # Check definitions directory exists
        definitions_dir = pack_path / "definitions"
        if not definitions_dir.exists() or not definitions_dir.is_dir():
            errors.append(GateError(
                gate="A",
                code="DEFINITIONS_DIR_MISSING",
                message="definitions/ directory not found",
                context={"pack_id": pack_id}
            ))
        
        # Check for volatile fields (hostnames, temp paths)
        pack_json_str = json.dumps(pack_data)
        volatile_patterns = [
            r'/tmp/',
            r'C:\\Users\\',
            r'/home/[^/]+/',
            r'hostname',
            r'process_id',
            r'pid:'
        ]
        for pattern in volatile_patterns:
            if re.search(pattern, pack_json_str, re.IGNORECASE):
                errors.append(GateError(
                    gate="A",
                    code="VOLATILE_FIELD_DETECTED",
                    message=f"Volatile data detected: {pattern}",
                    context={"pack_id": pack_id, "pattern": pattern}
                ))
    
    return errors


def gate_b_ceremony_determinism(transcript: ValidationTranscript) -> List[GateError]:
    """
    Gate B: Ceremony Determinism
    
    Validates transcript for deterministic properties:
    - Entries are sequentially ordered
    - No machine-specific data in context
    - Context keys are sorted (checked via export_canonical)
    - No credentials or sensitive data
    
    Args:
        transcript: Validation transcript to check
    
    Returns:
        List of GateError objects (empty = pass)
    """
    errors = []
    
    # Check sequence ordering
    for i, entry in enumerate(transcript.entries):
        if entry.sequence_number != i + 1:
            errors.append(GateError(
                gate="B",
                code="SEQUENCE_OUT_OF_ORDER",
                message=f"Entry sequence {entry.sequence_number} expected {i + 1}",
                context={"actual": entry.sequence_number, "expected": i + 1}
            ))
    
    # Check for machine-specific data in context
    volatile_patterns = [
        r'/tmp/',
        r'C:\\Users\\',
        r'C:\\Windows\\',
        r'/home/[^/]+/',
        r'hostname',
        r'127\.0\.0\.1',
        r'localhost',
        r'process_id',
        r'pid:'
    ]
    
    for entry in transcript.entries:
        if entry.context:
            context_str = str(entry.context)
            for pattern in volatile_patterns:
                if re.search(pattern, context_str, re.IGNORECASE):
                    errors.append(GateError(
                        gate="B",
                        code="MACHINE_SPECIFIC_DATA",
                        message=f"Machine-specific data in transcript: {pattern}",
                        context={
                            "pack_id": entry.pack_id,
                            "step_type": entry.step_type,
                            "pattern": pattern
                        }
                    ))
    
    # Check for credentials
    credential_patterns = [
        r'password',
        r'api_key',
        r'secret',
        r'token',
        r'auth',
        r'credential'
    ]
    
    for entry in transcript.entries:
        if entry.context:
            context_keys = [k.lower() for k in entry.context.keys()]
            for pattern in credential_patterns:
                if any(pattern in key for key in context_keys):
                    errors.append(GateError(
                        gate="B",
                        code="CREDENTIAL_IN_TRANSCRIPT",
                        message=f"Potential credential field: {pattern}",
                        context={
                            "pack_id": entry.pack_id,
                            "step_type": entry.step_type
                        }
                    ))
    
    return errors


def gate_c_admission_compatibility(
    old_active_set: Optional[ActiveSet],
    new_packs: List[Dict[str, str]],
    packs_namespaces: Dict[str, List[str]]
) -> List[GateError]:
    """
    Gate C: Admission Compatibility
    
    Validates cross-pack compatibility:
    - No namespace collisions between packs
    - Version monotonicity: new version > old version (V' > V strictly)
    - No version downgrades
    
    Args:
        old_active_set: Previous active set (None if first admission)
        new_packs: List of pack entries [{"pack_id": str, "version": str}]
        packs_namespaces: Dict mapping pack_id to list of owned namespaces
    
    Returns:
        List of GateError objects (empty = pass)
    """
    errors = []
    
    # Part 1: Namespace collision detection
    namespace_owners = {}
    for pack_id, namespaces in packs_namespaces.items():
        for namespace in namespaces:
            if namespace in namespace_owners:
                errors.append(GateError(
                    gate="C",
                    code="NAMESPACE_COLLISION",
                    message=f"Namespace '{namespace}' claimed by multiple packs",
                    context={
                        "namespace": namespace,
                        "packs": [namespace_owners[namespace], pack_id]
                    }
                ))
            else:
                namespace_owners[namespace] = pack_id
    
    # Part 2: Version monotonicity (V' > V)
    if old_active_set:
        for pack_entry in new_packs:
            pack_id = pack_entry["pack_id"]
            new_version_str = pack_entry["version"]
            
            # Find old declaration
            old_decl = None
            for old_pack in old_active_set.packs:
                if old_pack.pack_id == pack_id:
                    old_decl = old_pack
                    break
            
            if old_decl:
                try:
                    new_ver = SemanticVersion.parse(new_version_str)
                    old_ver = SemanticVersion.parse(old_decl.version)
                    
                    if new_ver <= old_ver:
                        errors.append(GateError(
                            gate="C",
                            code="VERSION_MONOTONICITY_VIOLATION",
                            message=f"Version must increase: {old_ver} -> {new_ver}",
                            context={
                                "pack_id": pack_id,
                                "old_version": str(old_ver),
                                "new_version": str(new_ver)
                            }
                        ))
                except Exception as e:
                    errors.append(GateError(
                        gate="C",
                        code="VERSION_PARSE_ERROR",
                        message=f"Failed to parse version: {e}",
                        context={
                            "pack_id": pack_id,
                            "version": new_version_str
                        }
                    ))
    
    return errors


def run_all_gates(
    pack_paths: Dict[str, Path],
    transcript: ValidationTranscript,
    old_active_set: Optional[ActiveSet],
    new_packs: List[Dict[str, str]],
    packs_namespaces: Dict[str, List[str]]
) -> Dict[str, Any]:
    """
    Run all ceremony gates in sequence.
    
    Args:
        pack_paths: Dict mapping pack_id to filesystem path
        transcript: Validation transcript
        old_active_set: Previous active set (None if first)
        new_packs: List of new pack entries
        packs_namespaces: Dict mapping pack_id to namespaces
    
    Returns:
        Dict with gate results:
        {
            "passed": bool,
            "gates": {"A": bool, "B": bool, "C": bool},
            "errors": [GateError, ...]
        }
    """
    all_errors = []
    gate_results = {}
    
    # Gate A: Pack Integrity
    gate_a_errors = gate_a_pack_integrity(pack_paths)
    gate_results["A"] = len(gate_a_errors) == 0
    all_errors.extend(gate_a_errors)
    
    # Gate B: Ceremony Determinism
    gate_b_errors = gate_b_ceremony_determinism(transcript)
    gate_results["B"] = len(gate_b_errors) == 0
    all_errors.extend(gate_b_errors)
    
    # Gate C: Admission Compatibility
    gate_c_errors = gate_c_admission_compatibility(old_active_set, new_packs, packs_namespaces)
    gate_results["C"] = len(gate_c_errors) == 0
    all_errors.extend(gate_c_errors)
    
    passed = len(all_errors) == 0
    
    return {
        "passed": passed,
        "gates": gate_results,
        "errors": [e.to_dict() for e in all_errors]
    }
