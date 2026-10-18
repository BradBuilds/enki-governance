"""
ENKI Spine S1.2 — Namespace Registry & Ownership

Validates namespace ownership claims and prevents semantic collisions.
Per SPINE-00 v1.0.1, SPINE-EXECUTION-PLAN S1.2

Authority: SPINE-00 v1.0.1, SPINE-ROADMAP v1.1.0
"""

import re
from typing import List, Dict, Set, Tuple, Optional, Any
from dataclasses import dataclass


# Locked regex pattern (S1.2)
NAMESPACE_PATTERN = re.compile(r'^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9]*)*$')


@dataclass
class NamespaceError:
    """Namespace validation or collision error."""
    code: str
    message: str
    context: Dict[str, Any]


def validate_namespace_declarations(
    pack_id: str,
    namespaces_owned: Optional[List[str]]
) -> List[NamespaceError]:
    """
    Validate namespace declarations for a single pack.
    
    Checks:
    - Namespace format (lowercase dot-path)
    - Regex pattern compliance
    
    Args:
        pack_id: Pack identifier
        namespaces_owned: List of namespace prefixes claimed by pack (may be None)
    
    Returns:
        List of NamespaceError objects (empty if valid)
    """
    errors = []
    
    # None or empty list is valid (pack claims no namespaces)
    if not namespaces_owned:
        return errors
    
    for namespace in namespaces_owned:
        if not isinstance(namespace, str):
            errors.append(NamespaceError(
                code="NAMESPACE_INVALID_FORMAT",
                message=f"Namespace must be string, got {type(namespace).__name__}",
                context={
                    "pack_id": pack_id,
                    "namespace": str(namespace),
                    "pattern": NAMESPACE_PATTERN.pattern
                }
            ))
            continue
        
        if not NAMESPACE_PATTERN.match(namespace):
            errors.append(NamespaceError(
                code="NAMESPACE_INVALID_FORMAT",
                message=f"Namespace '{namespace}' does not match required pattern",
                context={
                    "pack_id": pack_id,
                    "namespace": namespace,
                    "pattern": NAMESPACE_PATTERN.pattern
                }
            ))
    
    return errors


def detect_namespace_collisions(
    packs_namespaces: Dict[str, List[str]]
) -> List[NamespaceError]:
    """
    Detect namespace collisions across multiple packs.
    
    Collision rules:
    - Exact match: Pack A claims "audit", Pack B claims "audit"
    - Prefix containment: Pack A claims "audit", Pack B claims "audit.event"
    - Reverse prefix: Pack A claims "audit.event", Pack B claims "audit"
    
    Args:
        packs_namespaces: Dict mapping pack_id -> list of owned namespaces
    
    Returns:
        List of NamespaceError objects for all collisions found
    """
    errors = []
    pack_ids = sorted(packs_namespaces.keys())  # Deterministic ordering
    
    # Compare each pack pair
    for i, pack_a in enumerate(pack_ids):
        namespaces_a = packs_namespaces.get(pack_a, [])
        if not namespaces_a:
            continue
            
        for pack_b in pack_ids[i+1:]:
            namespaces_b = packs_namespaces.get(pack_b, [])
            if not namespaces_b:
                continue
            
            # Check each namespace pair for overlap
            for ns_a in namespaces_a:
                for ns_b in namespaces_b:
                    if _namespaces_overlap(ns_a, ns_b):
                        errors.append(NamespaceError(
                            code="NAMESPACE_COLLISION",
                            message=f"Namespace collision between packs",
                            context={
                                "pack_id": pack_a,
                                "namespace": ns_a,
                                "conflicting_pack_id": pack_b,
                                "conflicting_namespace": ns_b
                            }
                        ))
    
    # Sort errors deterministically
    errors.sort(key=_namespace_error_sort_key)
    return errors


def verify_definition_ownership(
    pack_id: str,
    namespaces_owned: Optional[List[str]],
    definition_ids: List[str]
) -> List[NamespaceError]:
    """
    Verify that all definitions belong to pack's owned namespaces.
    
    Rules:
    - If pack owns no namespaces but has definitions → error
    - If definition doesn't match any owned namespace → error
    - Matching uses prefix rules: "audit.alert" matches namespace "audit"
    
    Args:
        pack_id: Pack identifier
        namespaces_owned: List of namespace prefixes owned (may be None/empty)
        definition_ids: List of definition IDs from pack
    
    Returns:
        List of NamespaceError objects for ownership violations
    """
    errors = []
    
    # No definitions means no problem
    if not definition_ids:
        return errors
    
    # Has definitions but no namespaces claimed
    if not namespaces_owned:
        for def_id in definition_ids:
            errors.append(NamespaceError(
                code="DEFINITION_WITHOUT_NAMESPACE",
                message=f"Pack defines '{def_id}' but owns no namespaces",
                context={
                    "pack_id": pack_id,
                    "definition_id": def_id
                }
            ))
        errors.sort(key=_namespace_error_sort_key)
        return errors
    
    # Check each definition matches at least one owned namespace
    for def_id in definition_ids:
        if not _definition_matches_namespace(def_id, namespaces_owned):
            errors.append(NamespaceError(
                code="NAMESPACE_OWNERSHIP_VIOLATION",
                message=f"Definition '{def_id}' not in owned namespaces",
                context={
                    "pack_id": pack_id,
                    "definition_id": def_id,
                    "owned_namespaces": namespaces_owned
                }
            ))
    
    # Sort errors deterministically
    errors.sort(key=_namespace_error_sort_key)
    return errors


# === Helper Functions ===

def _namespaces_overlap(ns_a: str, ns_b: str) -> bool:
    """
    Check if two namespaces overlap (collision condition).
    
    Overlapping cases:
    - Exact match: "audit" == "audit"
    - Prefix: "audit" contains "audit.event"
    - Reverse prefix: "audit.event" contained by "audit"
    """
    if ns_a == ns_b:
        return True
    
    # Check if one is a prefix of the other
    # "audit" is prefix of "audit.event" (with dot separator)
    if ns_a.startswith(ns_b + ".") or ns_b.startswith(ns_a + "."):
        return True
    
    return False


def _definition_matches_namespace(def_id: str, namespaces: List[str]) -> bool:
    """
    Check if definition ID matches any owned namespace (prefix matching).
    
    Examples:
    - "audit.alert" matches namespace "audit"
    - "identity.actor.user" matches namespace "identity.actor"
    - "identity.actor.user" matches namespace "identity"
    """
    for namespace in namespaces:
        # Exact match
        if def_id == namespace:
            return True
        # Prefix match (definition starts with namespace + dot)
        if def_id.startswith(namespace + "."):
            return True
    
    return False


def _namespace_error_sort_key(error: NamespaceError) -> Tuple:
    """
    Deterministic sort key for namespace errors.
    
    Sort by: (error_code, pack_id, namespace_or_definition_id, message)
    """
    ctx = error.context
    pack_id = ctx.get("pack_id", "")
    
    # Primary reference: namespace or definition_id
    primary_ref = ctx.get("namespace", ctx.get("definition_id", ""))
    
    return (error.code, pack_id, primary_ref, error.message)
