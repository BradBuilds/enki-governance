"""
ENKI Definition Pack Loader - Type Definitions

Data structures for pack loading, validation, and indexing.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from enum import Enum


class ErrorSeverity(Enum):
    """Severity levels for pack validation issues."""
    ERROR = "error"
    WARNING = "warning"


@dataclass
class PackError:
    """Represents a validation error or warning."""
    code: str
    severity: ErrorSeverity
    message: str
    context: Optional[Dict[str, Any]] = None

    def __str__(self) -> str:
        ctx = f" ({self.context})" if self.context else ""
        return f"[{self.severity.value.upper()}] {self.code}: {self.message}{ctx}"


@dataclass
class Definition:
    """Represents a loaded and validated definition."""
    id: str
    type: str
    version: str
    description: Optional[str]
    content: Dict[str, Any]
    metadata: Optional[Dict[str, Any]] = None
    dependencies: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Definition":
        """Create Definition from JSON-loaded dictionary."""
        return cls(
            id=data["id"],
            type=data["type"],
            version=data["version"],
            description=data.get("description"),
            content=data["content"],
            metadata=data.get("metadata"),
            dependencies=data.get("dependencies", [])
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary (for JSON serialization)."""
        result = {
            "id": self.id,
            "type": self.type,
            "version": self.version,
            "content": self.content
        }
        if self.description:
            result["description"] = self.description
        if self.metadata:
            result["metadata"] = self.metadata
        if self.dependencies:
            result["dependencies"] = self.dependencies
        return result


@dataclass
class DefinitionIndex:
    """Indexes for fast definition lookup."""
    by_id: Dict[str, Definition] = field(default_factory=dict)
    by_type: Dict[str, List[Definition]] = field(default_factory=dict)

    def is_empty(self) -> bool:
        """Check if index is empty."""
        return len(self.by_id) == 0


@dataclass
class PackLoadResult:
    """
    Result of loading a definition pack.
    
    Attributes:
        canonical: True only if loaded via strict mode with active set verification.
                   Non-canonical packs are for inspection only, NEVER for computation.
        verification: Dict tracking verification steps performed:
                      - strict_active_set: bool (was active set enforced?)
                      - hash_verified: bool (was content hash verified?)
    """
    ok: bool
    pack_id: str
    version: Dict[str, Any]
    definitions: List[Definition] = field(default_factory=list)
    index: DefinitionIndex = field(default_factory=DefinitionIndex)
    errors: List[PackError] = field(default_factory=list)
    warnings: List[PackError] = field(default_factory=list)
    canonical: bool = False  # LOCKED: True only if strict mode + active set verified
    verification: Dict[str, bool] = field(default_factory=dict)  # Track verification steps

    def add_error(self, code: str, message: str, context: Optional[Dict[str, Any]] = None) -> None:
        """Add an error to the result."""
        self.errors.append(PackError(code, ErrorSeverity.ERROR, message, context))
        self.ok = False
        # Sort errors deterministically for stable output (SPINE-00 requirement)
        self._sort_errors()

    def add_warning(self, code: str, message: str, context: Optional[Dict[str, Any]] = None) -> None:
        """Add a warning to the result."""
        self.warnings.append(PackError(code, ErrorSeverity.WARNING, message, context))
        # Sort warnings deterministically for stable output
        self._sort_warnings()

    def _sort_errors(self) -> None:
        """Sort errors deterministically by (code, pack_id, context_pack_id)."""
        def error_sort_key(err: PackError) -> tuple:
            pack_id = err.context.get("pack_id", "") if err.context else ""
            return (err.code, pack_id)
        self.errors.sort(key=error_sort_key)

    def _sort_warnings(self) -> None:
        """Sort warnings deterministically by (code, pack_id, context_pack_id)."""
        def warning_sort_key(warn: PackError) -> tuple:
            pack_id = warn.context.get("pack_id", "") if warn.context else ""
            return (warn.code, pack_id)
        self.warnings.sort(key=warning_sort_key)

    def has_errors(self) -> bool:
        """Check if result has any errors."""
        return len(self.errors) > 0

    def has_warnings(self) -> bool:
        """Check if result has any warnings."""
        return len(self.warnings) > 0

    def definition_count(self) -> int:
        """Get count of loaded definitions."""
        return len(self.definitions)


@dataclass
class PackManifest:
    """Represents a pack.json manifest."""
    pack_id: str
    version: str
    name: str
    description: str
    definitions: List[Dict[str, str]]
    dependencies: List[str] = field(default_factory=list)
    metadata: Optional[Dict[str, Any]] = None
    capabilities_provided: List[Dict[str, str]] = field(default_factory=list)
    capabilities_required: List[Dict[str, str]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PackManifest":
        """Create PackManifest from JSON-loaded dictionary."""
        return cls(
            pack_id=data["pack_id"],
            version=data["version"],
            name=data["name"],
            description=data["description"],
            definitions=data["definitions"],
            dependencies=data.get("dependencies", []),
            metadata=data.get("metadata"),
            capabilities_provided=data.get("capabilities_provided", []),
            capabilities_required=data.get("capabilities_required", [])
        )
