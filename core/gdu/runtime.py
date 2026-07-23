"""
GDU Core — Runtime Enforcement

Global Definition Universe context for canonical pack loading and computation.
The ONLY authorized path for loading definitions in production code.

Authority: GDU Technical Build Plan v2.4.0 §Phase 3
"""

from typing import Optional, List, Dict, Any
from pathlib import Path
from dataclasses import dataclass
import warnings

from core.packs.loader import PackLoader, PackLoadResult
from core.spine.active_set import ActiveSet


@dataclass
class GDULoadResult:
    """Result of GDU load operation."""
    success: bool
    packs: Dict[str, PackLoadResult]  # pack_id -> PackLoadResult
    errors: List[str]
    
    def get_pack(self, pack_id: str) -> Optional[PackLoadResult]:
        """Get specific pack result."""
        return self.packs.get(pack_id)
    
    def all_canonical(self) -> bool:
        """Check all loaded packs are canonical."""
        return all(p.canonical for p in self.packs.values())


class GDUContext:
    """
    Global Definition Universe context.
    
    Authoritative loader for canonical definitions. All production code
    using definitions MUST go through this context.
    
    Features:
    - Enforces strict mode (active set verification)
    - Verifies all loaded packs are canonical
    - Single entry point for definition loading
    - Prevents bypass paths
    
    Authority: GDU Technical Build Plan v2.4.0 §Phase 3.1
    """
    
    def __init__(self, active_set: ActiveSet):
        """
        Initialize GDU context.
        
        Args:
            active_set: Authoritative active set (must be loaded via load_active_set_strict)
        """
        if not isinstance(active_set, ActiveSet):
            raise TypeError("active_set must be ActiveSet instance")
        
        self.active_set = active_set
        self._loader = PackLoader(active_set=active_set, strict=True)
        self._loaded_packs: Dict[str, PackLoadResult] = {}
    
    def load_pack(self, pack_id: str, pack_path: Path) -> PackLoadResult:
        """
        Load single pack through GDU context (strict mode).
        
        Args:
            pack_id: Pack identifier
            pack_path: Filesystem path to pack
        
        Returns:
            PackLoadResult (guaranteed canonical if successful)
        
        Raises:
            RuntimeError: If pack fails to load or is not canonical
        
        Authority: GDU Technical Build Plan v2.4.0 §Phase 3.1
        """
        result = self._loader.load_pack(pack_path)
        
        # CRITICAL: Verify canonical flag
        if result.ok and not result.canonical:
            raise RuntimeError(
                f"CRITICAL: Pack {pack_id} loaded but marked non-canonical. "
                f"This should never happen with strict=True. "
                f"Possible corruption or bypass attempt."
            )
        
        # Cache result (successful or not)
        self._loaded_packs[pack_id] = result
        
        return result
    
    def load_all_active_packs(self, pack_paths: Dict[str, Path]) -> GDULoadResult:
        """
        Load all active set packs through GDU context.
        
        Args:
            pack_paths: Dict mapping pack_id to filesystem path
        
        Returns:
            GDULoadResult with all loaded packs
        
        Authority: GDU Technical Build Plan v2.4.0 §Phase 3.1
        """
        results = {}
        errors = []
        
        for pack_decl in self.active_set.packs:
            pack_id = pack_decl.pack_id
            
            if pack_id not in pack_paths:
                errors.append(f"Pack {pack_id} in active set but not found in pack_paths")
                continue
            
            pack_path = pack_paths[pack_id]
            
            try:
                result = self.load_pack(pack_id, pack_path)
                results[pack_id] = result
                
                if not result.ok:
                    errors.append(f"Pack {pack_id} failed to load: {result.errors}")
                elif not result.canonical:
                    errors.append(f"Pack {pack_id} loaded but not canonical (CRITICAL)")
                    
            except Exception as e:
                errors.append(f"Pack {pack_id} exception: {e}")
        
        success = len(errors) == 0 and len(results) == len(self.active_set.packs)
        
        return GDULoadResult(
            success=success,
            packs=results,
            errors=errors
        )
    
    def get_loaded_pack(self, pack_id: str) -> Optional[PackLoadResult]:
        """Get previously loaded pack."""
        return self._loaded_packs.get(pack_id)
    
    def get_all_loaded_packs(self) -> Dict[str, PackLoadResult]:
        """Get all loaded packs."""
        return dict(self._loaded_packs)
    
    def verify_all_canonical(self) -> bool:
        """Verify all loaded packs are canonical."""
        return all(p.canonical for p in self._loaded_packs.values())


# Module-level helper (recommended for most code)

_global_gdu_context: Optional[GDUContext] = None


def set_global_gdu_context(context: GDUContext) -> None:
    """
    Set global GDU context for module.
    
    This allows other modules to use load_active_gdu() without passing context.
    
    Args:
        context: GDUContext instance
    """
    global _global_gdu_context
    _global_gdu_context = context


def get_global_gdu_context() -> Optional[GDUContext]:
    """Get currently set global GDU context."""
    return _global_gdu_context


def load_active_gdu(pack_id: str, pack_path: Path) -> PackLoadResult:
    """
    Load pack through global GDU context (recommended API).
    
    This is the canonical way to load definitions in production code.
    
    Args:
        pack_id: Pack identifier
        pack_path: Filesystem path to pack
    
    Returns:
        PackLoadResult (guaranteed canonical if successful)
    
    Raises:
        RuntimeError: If no global context set or pack fails to load
    
    Example:
        >>> from core.gdu.runtime import load_active_gdu
        >>> result = load_active_gdu("identity", Path("libraries/identity"))
        >>> assert result.canonical  # Always true on success
    
    Authority: GDU Technical Build Plan v2.4.0 §Phase 3.1
    """
    if _global_gdu_context is None:
        raise RuntimeError(
            "No global GDU context set. Call set_global_gdu_context() first. "
            "Or use GDUContext directly: ctx = GDUContext(active_set); ctx.load_pack(...)"
        )
    
    return _global_gdu_context.load_pack(pack_id, pack_path)


def deprecation_warning_for_direct_pack_loading():
    """Emit deprecation warning for direct PackLoader usage."""
    warnings.warn(
        "Direct PackLoader usage is deprecated. Use load_active_gdu() or GDUContext instead.",
        DeprecationWarning,
        stacklevel=2
    )
