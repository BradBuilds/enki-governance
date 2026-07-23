"""
ENKI Spine - Active Definition Set

The Active Set declares which definition packs are permitted to load.

Key Capabilities:
1. Empty set = spine-only mode (reject all domain packs)
2. Explicit allow-list (fail-closed by default)
3. Version constraints (optional semver matching)
4. Required vs optional packs

Governance:
- If a pack is not in the active set, it MUST be rejected
- If a required pack fails to load, the system MUST halt
- If an optional pack fails to load, emit warning but continue

Error Codes (LOCKED - GDU Technical Build Plan v2.4.0):
- STRICT_MODE_REQUIRES_ACTIVE_SET
- PACK_NOT_IN_ACTIVE_SET
- PACK_VERSION_MISMATCH
- PACK_HASH_MISMATCH
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
import jsonschema


@dataclass
class PackDeclaration:
    """
    Declaration of a pack in the active set (pin-style).
    
    Minimal structure with exact version pinning.
    Version constraints and required/optional flags deferred to future policy layer.
    """
    pack_id: str
    version: str
    content_hash: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict) -> "PackDeclaration":
        """Create from JSON dictionary."""
        return cls(
            pack_id=data["pack_id"],
            version=data["version"],
            content_hash=data.get("content_hash")
        )

    def to_dict(self) -> Dict:
        """Convert to JSON dictionary."""
        result = {
            "pack_id": self.pack_id,
            "version": self.version
        }
        if self.content_hash:
            result["content_hash"] = self.content_hash
        return result


@dataclass
class ActiveSet:
    """
    Active Definition Set - declares which packs are allowed (pin-style).
    
    Minimal structure with exact version pins. Empty packs = spine-only mode.
    
    Governance Invariant (SPINE-00):
    If packs array is empty, loader MUST reject all packs with 
    PACK_NOT_IN_ACTIVE_SET and MUST still function for spine services.
    """
    version: str
    packs: List[PackDeclaration] = field(default_factory=list)
    metadata: Optional[Dict] = None

    @classmethod
    def from_dict(cls, data: Dict) -> "ActiveSet":
        """Create from JSON dictionary."""
        return cls(
            version=data["version"],
            packs=[
                PackDeclaration.from_dict(p) for p in data.get("packs", [])
            ],
            metadata=data.get("metadata")
        )

    @classmethod
    def from_file(cls, path: Path) -> "ActiveSet":
        """Load from JSON file."""
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return cls.from_dict(data)

    @classmethod
    def empty(cls, version: str = "0.1.0") -> "ActiveSet":
        """
        Create an empty active set (spine-only mode).
        
        Spine-only mode MUST reject all domain packs and MUST still function.
        """
        return cls(version=version, packs=[])

    def to_dict(self) -> Dict:
        """Convert to JSON dictionary with sorted keys for determinism."""
        result = {
            "version": self.version,
            "packs": [p.to_dict() for p in self.packs]
        }
        if self.metadata:
            result["metadata"] = self.metadata
        return result

    def to_file(self, path: Path) -> None:
        """Save to JSON file."""
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, indent=2, sort_keys=True)

    def is_pack_allowed(self, pack_id: str) -> bool:
        """
        Check if a pack is allowed to load.
        
        Returns:
            True if pack_id is in packs list, False otherwise
        """
        return any(p.pack_id == pack_id for p in self.packs)

    def get_pack_declaration(self, pack_id: str) -> Optional[PackDeclaration]:
        """Get the declaration for a specific pack."""
        for pack_decl in self.packs:
            if pack_decl.pack_id == pack_id:
                return pack_decl
        return None

    def is_spine_only(self) -> bool:
        """
        Check if this is spine-only mode (no domain packs allowed).
        
        Spine-only mode is the foundational proof that ENKI can function
        without domain data.
        """
        return len(self.packs) == 0


def validate_active_set(data: Dict, schema_path: Optional[Path] = None) -> None:
    """
    Validate active set data against schema.
    
    Args:
        data: Active set dictionary
        schema_path: Path to active_set.schema.json (optional)
        
    Raises:
        jsonschema.ValidationError: If validation fails
        FileNotFoundError: If schema file not found
    """
    if schema_path is None:
        # Default: schemas/active_set.schema.json relative to project root
        schema_path = Path(__file__).parent.parent.parent / "schemas" / "active_set.schema.json"
    
    if not schema_path.exists():
        raise FileNotFoundError(f"Active set schema not found: {schema_path}")
    
    with open(schema_path, 'r', encoding='utf-8') as f:
        schema = json.load(f)
    
    jsonschema.validate(instance=data, schema=schema)


def load_active_set(path: Path, validate: bool = True) -> ActiveSet:
    """
    Load and optionally validate an active set from file.
    
    Args:
        path: Path to active set JSON file
        validate: Whether to validate against schema (default: True)
        
    Returns:
        ActiveSet instance
        
    Raises:
        FileNotFoundError: If file not found
        json.JSONDecodeError: If invalid JSON
        jsonschema.ValidationError: If validation fails
    """
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    if validate:
        validate_active_set(data)
    
    return ActiveSet.from_dict(data)


def load_active_set_strict(path: Path) -> ActiveSet:
    """
    Load active set with STRICT validation (GDU governance mode).
    
    Enforces:
    - Schema compliance (always)
    - Deterministic pack ordering (by pack_id, lexicographic)
    - All content_hash fields MUST be present (no optionals)
    - Version format validation via semver parser
    
    Args:
        path: Path to active set JSON file
        
    Returns:
        ActiveSet with normalized pack ordering
        
    Raises:
        FileNotFoundError: If file not found
        json.JSONDecodeError: If invalid JSON
        jsonschema.ValidationError: If schema validation fails
        ValueError: If content_hash missing or semver invalid
        
    Authority: GDU Technical Build Plan v2.4.0 §1.1
    """
    from .semver import SemanticVersion
    
    # Load with schema validation (always strict)
    active_set = load_active_set(path, validate=True)
    
    # Verify ALL packs have content_hash (no optionals in strict mode)
    for pack_decl in active_set.packs:
        if not pack_decl.content_hash:
            raise ValueError(
                f"Strict mode: pack {pack_decl.pack_id} missing required content_hash"
            )
        
        # Verify content_hash format
        if not pack_decl.content_hash.startswith("sha256:"):
            raise ValueError(
                f"Strict mode: pack {pack_decl.pack_id} has invalid content_hash format "
                f"(must be 'sha256:<64-hex-chars>')"
            )
        
        # Verify version is valid semver
        try:
            SemanticVersion.parse(pack_decl.version)
        except ValueError as e:
            raise ValueError(
                f"Strict mode: pack {pack_decl.pack_id} has invalid version: {e}"
            ) from e
    
    # Normalize pack ordering (sort by pack_id, lexicographic)
    # This is NOT an error - just normalization for determinism
    active_set.packs = sorted(active_set.packs, key=lambda p: p.pack_id)
    
    return active_set


def verify_pack_allowed(
    active_set: ActiveSet,
    pack_id: str,
    version: str,
    actual_hash: str
) -> Tuple[bool, Optional[str], Dict]:
    """
    Verify pack is allowed AND matches declared hash/version.
    
    This is the AUTHORITATIVE check for active set compliance.
    Used by PackLoader(strict=True) as single verification point.
    
    Args:
        active_set: Active set to check against
        pack_id: Pack identifier to verify
        version: Pack version from pack.json
        actual_hash: Computed content hash of pack
        
    Returns:
        Tuple of (ok, error_code, context):
        - ok: True only if ALL checks pass
        - error_code: One of PACK_NOT_IN_ACTIVE_SET, PACK_VERSION_MISMATCH, 
                      PACK_HASH_MISMATCH, or None if ok=True
        - context: Dict with error details (declared/actual values)
        
    Example:
        >>> ok, code, ctx = verify_pack_allowed(active_set, "language", "0.1.0", hash)
        >>> if not ok:
        ...     result.add_error(code, get_error_message(code), ctx)
        
    Authority: GDU Technical Build Plan v2.4.0 §1.1 Error Codes (LOCKED)
    """
    # Check 1: Pack in active set?
    pack_decl = active_set.get_pack_declaration(pack_id)
    if pack_decl is None:
        return (
            False,
            "PACK_NOT_IN_ACTIVE_SET",
            {
                "pack_id": pack_id,
                "reason": "Pack not declared in active set"
            }
        )
    
    # Check 2: Version matches?
    if pack_decl.version != version:
        return (
            False,
            "PACK_VERSION_MISMATCH",
            {
                "pack_id": pack_id,
                "declared_version": pack_decl.version,
                "actual_version": version,
                "reason": "Pack version does not match active set declaration"
            }
        )
    
    # Check 3: Hash matches?
    if pack_decl.content_hash != actual_hash:
        return (
            False,
            "PACK_HASH_MISMATCH",
            {
                "pack_id": pack_id,
                "declared_hash": pack_decl.content_hash,
                "actual_hash": actual_hash,
                "reason": "Pack content hash does not match active set declaration"
            }
        )
    
    # All checks passed
    return (True, None, {"pack_id": pack_id, "verified": True})

