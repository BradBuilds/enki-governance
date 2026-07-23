"""
ENKI Definition Pack Loader

Loads, validates, and indexes ENKI definition packs.
This is the single gateway through which all libraries enter the system.

Rules:
- Definitions are canonical
- Deterministic behavior (same inputs → same outputs)
- Fail fast, fail loud
- No silent correction
- Active set gates all pack loading (spine-level enforcement)
"""

import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Set
import jsonschema

from .types import (
    Definition,
    DefinitionIndex,
    PackLoadResult,
    PackManifest
)
from . import errors

# Import capability validation and resolution (spine S1.1)
from .capabilities import validate_capabilities, resolve_capabilities

# Import active set if available (spine S1.0)
try:
    from core.spine.active_set import ActiveSet
    SPINE_ACTIVE_SET_AVAILABLE = True
except ImportError:
    SPINE_ACTIVE_SET_AVAILABLE = False
    ActiveSet = None

# PHASE 4.2: Production enforcement flag
# When ENFORCE_GDU_CONTEXT=true, direct PackLoader usage is FORBIDDEN in production
# Authority: GDU Technical Build Plan v2.4.0 Phase 4.2
ENFORCE_GDU_CONTEXT = os.getenv("ENFORCE_GDU_CONTEXT", "false").lower() == "true"


class PackLoader:
    """Loads and validates ENKI definition packs."""

    def __init__(
        self,
        schema_dir: Optional[Path] = None,
        active_set: Optional["ActiveSet"] = None,
        strict: bool = False
    ):
        """
        Initialize the pack loader.

        Args:
            schema_dir: Directory containing JSON schemas. If None, uses default.
            active_set: Active definition set (spine S1.0). If provided, only
                       packs in the active set will be allowed to load.
            strict: If True, ALL packs MUST be in active set AND content hash verified.
                   Strict mode is REQUIRED for canonical computation (GDU).
                   Non-strict mode is for inspection/tooling only.
                   
        Authority: GDU Technical Build Plan v2.4.0 §1.2
        
        PHASE 4.2 ENFORCEMENT:
        If ENFORCE_GDU_CONTEXT=true, raises RuntimeError unless called from
        test context. Production code MUST use core.gdu.bootstrap.get_gdu().
        """
        # Check if enforcement is active
        if ENFORCE_GDU_CONTEXT and not self._is_test_context():
            # PHASE 4.3: Log bypass attempt before failing
            self._log_bypass_attempt()
            
            raise RuntimeError(
                "Direct PackLoader usage is forbidden in production when ENFORCE_GDU_CONTEXT=true. "
                "Use GDUContext via core.gdu.bootstrap.get_gdu(). "
                "This is Phase 4.2 fail-closed enforcement - no bypass paths allowed. "
                "Authority: GDU Technical Build Plan v2.4.0 Phase 4.2"
            )
        
        self.schema_dir = schema_dir
        self.active_set = active_set
        self.strict = strict
        self._definition_schema: Optional[Dict] = None
    
    @staticmethod
    def _log_bypass_attempt():
        """
        Log bypass attempt for telemetry (Phase 4.3.4).
        
        This logs when PackLoader is instantiated directly in production,
        which indicates:
        - Misconfigured service
        - Rogue code path
        - Integration mistake
        - Attempted bypass (malicious or accidental)
        
        Does not prevent the error - just provides visibility.
        """
        import logging
        import traceback
        
        logger = logging.getLogger("enki.gdu.bypass")
        
        # Capture caller stack
        stack = traceback.extract_stack()
        caller_frames = []
        for frame in stack[-6:-1]:  # Exclude this function
            if 'loader.py' not in frame.filename:
                caller_frames.append(f"{frame.filename}:{frame.lineno} in {frame.name}")
        
        logger.warning(
            "GOVERNANCE BYPASS ATTEMPT: Direct PackLoader instantiation blocked",
            extra={
                "event_type": "gdu.bypass_attempt",
                "enforcement": "ENFORCE_GDU_CONTEXT=true",
                "caller_stack": caller_frames,
                "phase": "4.3.4_telemetry",
                "severity": "warning",
                "action_taken": "RuntimeError raised"
            }
        )
        
        # Optional: Emit metric for monitoring
        try:
            from core.events import emit_event, EventTypes, Severity, Scope
            emit_event(
                event_type="gdu.bypass_blocked",
                severity=Severity.WARNING,
                scope=Scope.NODE,
                metadata={
                    "enforcement_active": True,
                    "caller_info": caller_frames[0] if caller_frames else "unknown"
                }
            )
        except Exception:
            # Don't fail if event emission fails
            pass
    
    @staticmethod
    def _is_test_context() -> bool:
        """
        Check if we're running in test context.
        
        Returns True if:
        - pytest is running (PYTEST_CURRENT_TEST env var set)
        - or if __file__ indicates test file in call stack
        
        This allows tests to use PackLoader directly.
        """
        import sys
        import traceback
        
        # Check pytest environment variable
        if os.getenv("PYTEST_CURRENT_TEST"):
            return True
        
        # Check if any frame in stack is a test file
        for frame_info in traceback.extract_stack():
            if "test_" in frame_info.filename or frame_info.filename.endswith("_test.py"):
                return True
        
        return False

    def load_pack(self, pack_path: str | Path) -> PackLoadResult:
        """
        Load a definition pack from the filesystem.

        Args:
            pack_path: Path to the pack directory (e.g., "libraries/base60")

        Returns:
            PackLoadResult with loaded definitions or errors

        Example:
            >>> loader = PackLoader()
            >>> result = loader.load_pack("libraries/base60")
            >>> if result.ok:
            ...     print(f"Loaded {result.definition_count()} definitions")
        """
        pack_path = Path(pack_path)

        # Initialize result
        result = PackLoadResult(
            ok=True,
            pack_id="",
            version={}
        )

        # Validate pack path exists
        if not pack_path.exists():
            result.add_error(
                errors.PACK_PATH_NOT_FOUND,
                errors.get_error_message(errors.PACK_PATH_NOT_FOUND),
                {"path": str(pack_path)}
            )
            return result

        if not pack_path.is_dir():
            result.add_error(
                errors.PACK_PATH_NOT_DIRECTORY,
                errors.get_error_message(errors.PACK_PATH_NOT_DIRECTORY),
                {"path": str(pack_path)}
            )
            return result

        # Load manifest
        manifest_path = pack_path / "pack.json"
        if not manifest_path.exists():
            result.add_error(
                errors.PACK_MANIFEST_MISSING,
                errors.get_error_message(errors.PACK_MANIFEST_MISSING),
                {"path": str(manifest_path)}
            )
            return result

        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest_data = json.load(f)
        except json.JSONDecodeError as e:
            result.add_error(
                errors.PACK_MANIFEST_INVALID_JSON,
                errors.get_error_message(errors.PACK_MANIFEST_INVALID_JSON),
                {"path": str(manifest_path), "error": str(e)}
            )
            return result

        # Parse manifest
        try:
            manifest = PackManifest.from_dict(manifest_data)
        except (KeyError, TypeError) as e:
            result.add_error(
                errors.PACK_MANIFEST_INVALID_SCHEMA,
                errors.get_error_message(errors.PACK_MANIFEST_INVALID_SCHEMA),
                {"error": str(e)}
            )
            return result

        # Set pack metadata
        result.pack_id = manifest.pack_id
        result.version = {"version": manifest.version, "name": manifest.name}

        # STRICT MODE ENFORCEMENT (GDU Technical Build Plan v2.4.0 §1.2)
        # Strict mode requires active set AND verifies hash
        if self.strict:
            # Check 1: Active set MUST be provided in strict mode
            if self.active_set is None:
                result.add_error(
                    errors.STRICT_MODE_REQUIRES_ACTIVE_SET,
                    errors.get_error_message(errors.STRICT_MODE_REQUIRES_ACTIVE_SET),
                    {"pack_id": manifest.pack_id}
                )
                return result
            
            # Check 2: Pack MUST be in active set (use verify_pack_allowed for all checks)
            from core.spine.active_set import verify_pack_allowed
            from core.spine.content_hash import compute_pack_hash
            
            # Compute actual content hash
            actual_hash = compute_pack_hash(str(pack_path))
            
            # Verify pack is allowed (single verification point)
            ok, error_code, context = verify_pack_allowed(
                self.active_set,
                manifest.pack_id,
                manifest.version,
                actual_hash
            )
            
            if not ok:
                result.add_error(
                    error_code,
                    errors.get_error_message(error_code),
                    context
                )
                return result
            
            # All strict checks passed - mark as canonical
            result.canonical = True
            result.verification = {
                "strict_active_set": True,
                "hash_verified": True,
                "version_verified": True
            }
        
        # NON-STRICT MODE: Legacy active set check (warnings only, not enforced)
        elif self.active_set is not None:
            # Check if pack is in active set (but don't fail, just mark non-canonical)
            if not self.active_set.is_pack_allowed(manifest.pack_id):
                result.add_error(
                    errors.PACK_NOT_IN_ACTIVE_SET,
                    errors.get_error_message(errors.PACK_NOT_IN_ACTIVE_SET),
                    {
                        "pack_id": manifest.pack_id,
                        "active_set_mode": "spine-only" if self.active_set.is_spine_only() else "restricted"
                    }
                )
                return result
            # In active set but non-strict - still not canonical
            result.canonical = False
            result.verification = {"strict_active_set": False}
        else:
            # No active set at all - inspection mode only
            result.canonical = False
            result.verification = {"strict_active_set": False}

        # Load definition schema
        definition_schema = self._load_definition_schema()
        if definition_schema is None:
            result.add_error(
                errors.SCHEMA_FILE_MISSING,
                errors.get_error_message(errors.SCHEMA_FILE_MISSING),
                {"schema": "definition.schema.json"}
            )
            return result

        # Load all definitions
        definitions: List[Definition] = []
        seen_ids: Set[str] = set()

        # Sort definition entries for deterministic ordering
        sorted_def_entries = sorted(manifest.definitions, key=lambda d: d["id"])

        for def_entry in sorted_def_entries:
            def_id = def_entry["id"]
            def_path = pack_path / def_entry["path"]

            # Load definition file
            if not def_path.exists():
                result.add_error(
                    errors.DEFINITION_FILE_MISSING,
                    errors.get_error_message(errors.DEFINITION_FILE_MISSING),
                    {"id": def_id, "path": str(def_path)}
                )
                continue

            try:
                with open(def_path, 'r', encoding='utf-8') as f:
                    def_data = json.load(f)
            except json.JSONDecodeError as e:
                result.add_error(
                    errors.DEFINITION_INVALID_JSON,
                    errors.get_error_message(errors.DEFINITION_INVALID_JSON),
                    {"id": def_id, "path": str(def_path), "error": str(e)}
                )
                continue

            # Validate against schema
            try:
                jsonschema.validate(instance=def_data, schema=definition_schema)
            except jsonschema.ValidationError as e:
                result.add_error(
                    errors.DEFINITION_SCHEMA_INVALID,
                    errors.get_error_message(errors.DEFINITION_SCHEMA_INVALID),
                    {"id": def_id, "error": e.message}
                )
                continue

            # Check for duplicate ID
            if def_id in seen_ids:
                result.add_error(
                    errors.DUPLICATE_DEFINITION_ID,
                    errors.get_error_message(errors.DUPLICATE_DEFINITION_ID),
                    {"id": def_id}
                )
                continue

            seen_ids.add(def_id)

            # Create Definition object
            try:
                definition = Definition.from_dict(def_data)
                definitions.append(definition)

                # Check for warnings
                if definition.metadata and definition.metadata.get("deprecated"):
                    result.add_warning(
                        errors.DEPRECATED_DEFINITION,
                        errors.get_error_message(errors.DEPRECATED_DEFINITION),
                        {"id": def_id}
                    )

                if not definition.description:
                    result.add_warning(
                        errors.MISSING_DESCRIPTION,
                        errors.get_error_message(errors.MISSING_DESCRIPTION),
                        {"id": def_id}
                    )

            except (KeyError, TypeError) as e:
                result.add_error(
                    errors.DEFINITION_MISSING_REQUIRED_FIELD,
                    errors.get_error_message(errors.DEFINITION_MISSING_REQUIRED_FIELD),
                    {"id": def_id, "error": str(e)}
                )
                continue

        # Check if pack is empty
        if len(definitions) == 0:
            result.add_error(
                errors.PACK_EMPTY,
                errors.get_error_message(errors.PACK_EMPTY),
                {"pack_id": manifest.pack_id}
            )
            return result

        # If we have errors, return without building index
        if result.has_errors():
            return result

        # Build indexes (deterministic sorting)
        result.definitions = sorted(definitions, key=lambda d: d.id)
        result.index = self._build_index(result.definitions)
        result.ok = True

        return result

    def _load_definition_schema(self) -> Optional[Dict]:
        """Load the definition.schema.json file."""
        if self._definition_schema is not None:
            return self._definition_schema

        # Determine schema directory
        if self.schema_dir is None:
            # Default: schemas/ relative to project root
            # Assumes loader is in core/packs/
            schema_dir = Path(__file__).parent.parent.parent / "schemas"
        else:
            schema_dir = Path(self.schema_dir)

        schema_path = schema_dir / "definition.schema.json"

        if not schema_path.exists():
            return None

        try:
            with open(schema_path, 'r', encoding='utf-8') as f:
                self._definition_schema = json.load(f)
            return self._definition_schema
        except (json.JSONDecodeError, IOError):
            return None

    def _build_index(self, definitions: List[Definition]) -> DefinitionIndex:
        """
        Build indexes for fast definition lookup.

        Args:
            definitions: List of validated definitions

        Returns:
            DefinitionIndex with by_id and by_type mappings
        """
        index = DefinitionIndex()

        for definition in definitions:
            # Index by ID
            index.by_id[definition.id] = definition

            # Index by type
            if definition.type not in index.by_type:
                index.by_type[definition.type] = []
            index.by_type[definition.type].append(definition)

        # Sort type lists for determinism
        for def_type in index.by_type:
            index.by_type[def_type] = sorted(
                index.by_type[def_type],
                key=lambda d: d.id
            )

        return index


    def validate_capability_declarations(self, manifests: List[PackManifest]) -> Optional[PackLoadResult]:
        """
        Validate capability declarations for all manifests.
        
        Checks for:
        - Malformed declarations (INVALID_CAPABILITY_DECLARATION)
        - Duplicate (capability, version) within same pack (DUPLICATE_CAPABILITY_DECLARATION)
        
        Behavior (LOCKED):
        The helper collects all validation failures across all manifests and returns a single
        PackLoadResult(pack_id=errors.CAPABILITY_RESOLUTION_PACK_ID, halted=True) containing 
        all errors in deterministic order. Does NOT halt on first failure.
        
        Returns:
            None if all declarations valid
            PackLoadResult with halted=True if any validation failures
        """
        validation_errors = validate_capabilities(manifests)
        
        if not validation_errors:
            return None
        
        # Create HALT result with all validation errors
        halt_result = PackLoadResult(
            ok=False,
            pack_id=errors.CAPABILITY_RESOLUTION_PACK_ID,
            version={}
        )
        
        for err in validation_errors:
            halt_result.add_error(err.code, err.message, err.context)
        
        return halt_result


    def resolve_capability_requirements(self, manifests: List[PackManifest]) -> Optional[PackLoadResult]:
        """
        Resolve capability requirements across loaded packs.
        
        Args:
            manifests: List of successfully loaded PackManifest objects
        
        Returns:
            None if resolution succeeded (no HALTs)
            PackLoadResult with halted=True if any HALT condition
        
        HALT Conditions (SPINE-01):
        - MISSING_CAPABILITY: Required capability not provided by any pack
        - AMBIGUOUS_CAPABILITY: Multiple packs provide same (capability, version)
        - CAPABILITY_VERSION_MISMATCH: Capability exists but version mismatch
        """
        # Check if any manifest has capability requirements
        has_requirements = any(
            manifest.capabilities_required 
            for manifest in manifests
        )
        
        if not has_requirements:
            return None  # No capabilities to resolve
        
        # Resolve capabilities
        resolution_errors = resolve_capabilities(manifests)
        
        if not resolution_errors:
            return None  # All capabilities resolved successfully
        
        # Create HALT result with all resolution errors
        halt_result = PackLoadResult(
            ok=False,
            pack_id=errors.CAPABILITY_RESOLUTION_PACK_ID,
            version={}
        )
        
        for err in resolution_errors:
            halt_result.add_error(err.code, err.message, err.context)
        
        return halt_result


def load_pack(pack_path: str | Path) -> PackLoadResult:
    """
    Convenience function to load a pack.

    Args:
        pack_path: Path to the pack directory

    Returns:
        PackLoadResult

    Example:
        >>> from core.packs.loader import load_pack
        >>> result = load_pack("libraries/base60")
        >>> if result.ok:
        ...     print(f"Loaded {result.pack_id} v{result.version['version']}")
    """
    loader = PackLoader()
    return loader.load_pack(pack_path)
