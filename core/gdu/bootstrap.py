"""
GDU Bootstrap - Single Authority Entry Point

This module provides the ONLY authorized way to access the GDU context
in production code. All canonical operations must go through get_gdu().

RULE: No other module may call load_active_gdu() directly.
"""

from pathlib import Path
from typing import Optional
from core.gdu.runtime import GDUContext, load_active_gdu, set_global_gdu_context
from core.spine.active_set import load_active_set_strict
from core.gdu.config import ACTIVE_SET_PATH

# Global singleton - initialized once on first access
_GDU_CONTEXT: Optional[GDUContext] = None
_INITIALIZED = False


def get_gdu() -> GDUContext:
    """
    Get the global GDU context (singleton pattern).
    
    On first access, loads active_set_v3.json and initializes GDUContext.
    Subsequent calls return the cached instance.
    
    Returns:
        GDUContext: The global GDU context with all admitted packs loaded.
        
    Raises:
        RuntimeError: If active set cannot be loaded or packs fail verification.
        
    Example:
        >>> gdu = get_gdu()
        >>> result = gdu.load_pack("numeric", Path("libraries/numeric"))
        >>> # Guaranteed: result.canonical=True if result.ok
    """
    global _GDU_CONTEXT, _INITIALIZED
    
    if not _INITIALIZED:
        # Load active set with strict verification
        try:
            active_set = load_active_set_strict(ACTIVE_SET_PATH)
        except Exception as e:
            raise RuntimeError(
                f"Failed to load active set from {ACTIVE_SET_PATH}: {e}"
            ) from e
        
        # Create GDU context (always uses strict mode)
        _GDU_CONTEXT = GDUContext(active_set)
        
        # Register as global context
        set_global_gdu_context(_GDU_CONTEXT)
        
        # Verify all packs are canonical (safety check)
        if not _GDU_CONTEXT.verify_all_canonical():
            raise RuntimeError(
                "GDU initialization failed: one or more packs not canonical"
            )
        
        _INITIALIZED = True
    
    return _GDU_CONTEXT


def reset_gdu() -> None:
    """
    Reset the global GDU context (for testing only).
    
    WARNING: This should NEVER be called in production code.
    Only use in test teardown to ensure test isolation.
    """
    global _GDU_CONTEXT, _INITIALIZED
    _GDU_CONTEXT = None
    _INITIALIZED = False
