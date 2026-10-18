"""
ENKI Semantic Version Comparison

Single implementation: dataclass with comparison operators.
NO tuple utilities (prevents drift).

Authority: GDU Technical Build Plan v2.4.0 §SEMVER COMPARISON SPECIFICATION
"""

from dataclasses import dataclass
import re
from typing import Tuple


SEMVER_PATTERN = re.compile(r'^(\d+)\.(\d+)\.(\d+)$')


@dataclass(frozen=True)
class SemanticVersion:
    """
    Semantic version with strict comparison operators.
    
    Format: <major>.<minor>.<patch> (no pre-release or build metadata)
    
    Usage:
        >>> v1 = SemanticVersion.parse("1.2.3")
        >>> v2 = SemanticVersion.parse("2.0.0")
        >>> v1 < v2
        True
    """
    major: int
    minor: int
    patch: int
    
    @classmethod
    def parse(cls, version_str: str) -> "SemanticVersion":
        """
        Parse semantic version string.
        
        Args:
            version_str: Version string in format "major.minor.patch"
            
        Returns:
            SemanticVersion instance
            
        Raises:
            ValueError: If format invalid or contains pre-release/build metadata
            
        Examples:
            >>> SemanticVersion.parse("1.2.3")
            SemanticVersion(major=1, minor=2, patch=3)
            >>> SemanticVersion.parse("1.2.3-alpha")
            Traceback (most recent call last):
            ...
            ValueError: Invalid semver: 1.2.3-alpha (pre-release/build metadata not allowed)
        """
        if not version_str:
            raise ValueError("Version string cannot be empty")
        
        match = SEMVER_PATTERN.match(version_str)
        if not match:
            # Provide helpful error message
            if '-' in version_str or '+' in version_str:
                raise ValueError(
                    f"Invalid semver: {version_str} "
                    "(pre-release/build metadata not allowed)"
                )
            raise ValueError(
                f"Invalid semver: {version_str} "
                "(must be <major>.<minor>.<patch> with integers only)"
            )
        
        return cls(
            major=int(match.group(1)),
            minor=int(match.group(2)),
            patch=int(match.group(3))
        )
    
    def __str__(self) -> str:
        """Return string representation: 'major.minor.patch'."""
        return f"{self.major}.{self.minor}.{self.patch}"
    
    def to_tuple(self) -> Tuple[int, int, int]:
        """
        Convert to tuple for manual comparisons if needed.
        
        Returns:
            (major, minor, patch) tuple
        """
        return (self.major, self.minor, self.patch)
    
    def __lt__(self, other: "SemanticVersion") -> bool:
        """Less than comparison."""
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        return self.to_tuple() < other.to_tuple()
    
    def __le__(self, other: "SemanticVersion") -> bool:
        """Less than or equal comparison."""
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        return self.to_tuple() <= other.to_tuple()
    
    def __gt__(self, other: "SemanticVersion") -> bool:
        """Greater than comparison."""
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        return self.to_tuple() > other.to_tuple()
    
    def __ge__(self, other: "SemanticVersion") -> bool:
        """Greater than or equal comparison."""
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        return self.to_tuple() >= other.to_tuple()
    
    def __eq__(self, other: object) -> bool:
        """Equality comparison."""
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        return self.to_tuple() == other.to_tuple()
    
    def __hash__(self) -> int:
        """Hash for use in sets/dicts (frozen dataclass provides this)."""
        return hash(self.to_tuple())
