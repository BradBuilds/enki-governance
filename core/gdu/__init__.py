"""
GDU (Global Definition Universe) Core Module

Authoritative, enforceable definition governance subsystem.

Authority: GDU Technical Build Plan v2.4.0
"""

from core.gdu.runtime import (
    GDUContext,
    GDULoadResult,
    load_active_gdu,
    set_global_gdu_context,
    get_global_gdu_context,
)

from core.gdu.bootstrap import (
    get_gdu,
    reset_gdu,
)

__all__ = [
    "GDUContext",
    "GDULoadResult",
    "load_active_gdu",
    "set_global_gdu_context",
    "get_global_gdu_context",
    "get_gdu",
    "reset_gdu",
]
