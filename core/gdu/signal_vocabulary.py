"""
GDU-Backed Signal Vocabulary Loader

Loads signal types through the GDU governance context, ensuring:
1. The signalvocab pack is in the active set (admitted)
2. The pack content hash matches the declared hash (integrity)
3. The types are loaded through strict mode (canonical)

This replaces the direct filesystem read that previously bypassed governance.

Authority: GDU Technical Build Plan v2.4.0 — Signal Pipeline Integration
"""

import json
import logging
from pathlib import Path
from typing import Optional

_log = logging.getLogger("enki.gdu.signalvocab")

# Cached results (singleton pattern matching get_gdu())
_SIGNAL_TYPES: Optional[set] = None
_CANONICAL: bool = False
_VERIFICATION: Optional[dict] = None


def load_signal_types_via_gdu() -> tuple[set, bool, dict]:
    """
    Load signal types through GDU governance context.

    Returns:
        (signal_types, canonical, verification) where:
        - signal_types: set of valid type_id strings
        - canonical: True if loaded through verified GDU context
        - verification: dict with verification metadata

    The GDU path:
    1. get_gdu() → loads active_set_v5.json with strict verification
    2. GDUContext.load_pack("signalvocab", ...) → strict mode, hash check
    3. Extract signal_types from the canonical pack definitions

    Fallback (if GDU unavailable):
    - Direct filesystem read (non-canonical, logged as warning)
    - Returns canonical=False so the pipeline knows it's degraded
    """
    global _SIGNAL_TYPES, _CANONICAL, _VERIFICATION

    if _SIGNAL_TYPES is not None:
        return _SIGNAL_TYPES, _CANONICAL, _VERIFICATION or {}

    verification = {
        "method": None,
        "active_set_version": None,
        "pack_hash_verified": False,
        "pack_version": None,
        "types_count": 0,
        "error": None,
    }

    # ─── Attempt 1: GDU Context (canonical) ─────────────────────────────
    try:
        from core.gdu.bootstrap import get_gdu
        from core.gdu.config import PACK_PATHS

        gdu = get_gdu()
        pack_path = PACK_PATHS.get("signalvocab")

        if pack_path is None:
            raise ValueError("signalvocab not in PACK_PATHS config")

        # Resolve relative to project root
        project_root = Path(__file__).parent.parent.parent
        resolved_path = project_root / pack_path

        if not resolved_path.exists():
            raise FileNotFoundError(f"signalvocab pack not found at {resolved_path}")

        # Load through GDU strict mode (active set + hash verification)
        result = gdu.load_pack("signalvocab", resolved_path)

        if result.ok and result.canonical:
            # Extract signal types from the loaded definitions
            types = set()
            for defn in result.definitions:
                content = defn.content if hasattr(defn, "content") else {}
                if isinstance(content, dict) and "signal_types" in content:
                    for st in content["signal_types"]:
                        if isinstance(st, dict) and "type_id" in st:
                            types.add(st["type_id"])

            if types:
                _SIGNAL_TYPES = types
                _CANONICAL = True
                verification["method"] = "gdu_strict"
                verification["active_set_version"] = gdu.active_set.version
                verification["pack_hash_verified"] = True
                verification["pack_version"] = result.version.get("version") if isinstance(result.version, dict) else str(result.version)
                verification["types_count"] = len(types)
                _VERIFICATION = verification

                _log.info(
                    "GDU: signalvocab loaded CANONICALLY — %d types, "
                    "active set v%s, hash verified",
                    len(types), gdu.active_set.version,
                )
                return _SIGNAL_TYPES, _CANONICAL, _VERIFICATION
            else:
                _log.warning("GDU: signalvocab pack loaded but no signal_types extracted")
                verification["error"] = "no_types_extracted"
        else:
            errors = result.errors if hasattr(result, "errors") else []
            _log.warning("GDU: signalvocab pack load failed — canonical=%s, errors=%s",
                        result.canonical if hasattr(result, "canonical") else "?", errors)
            verification["error"] = f"pack_load_failed: {errors}"

    except Exception as e:
        _log.warning("GDU: governance context unavailable — %s (falling back to filesystem)", e)
        verification["error"] = f"gdu_unavailable: {e}"

    # ─── Attempt 2: Direct filesystem (non-canonical fallback) ──────────
    try:
        project_root = Path(__file__).parent.parent.parent
        vocab_path = project_root / "libraries" / "signalvocab" / "definitions" / "signalvocab.signal_types.json"

        with open(vocab_path) as f:
            data = json.load(f)

        types = {
            st["type_id"]
            for st in data["content"]["signal_types"]
        }

        _SIGNAL_TYPES = types
        _CANONICAL = False
        verification["method"] = "filesystem_fallback"
        verification["pack_hash_verified"] = False
        verification["types_count"] = len(types)
        _VERIFICATION = verification

        _log.warning(
            "GDU: signalvocab loaded via FILESYSTEM FALLBACK (non-canonical) — "
            "%d types. Governance verification was NOT performed.",
            len(types),
        )
        return _SIGNAL_TYPES, _CANONICAL, _VERIFICATION

    except Exception as e2:
        _log.error("GDU: signalvocab UNAVAILABLE — both GDU and filesystem failed: %s", e2)
        _SIGNAL_TYPES = set()
        _CANONICAL = False
        verification["method"] = "failed"
        verification["error"] = f"all_paths_failed: {e2}"
        _VERIFICATION = verification
        return _SIGNAL_TYPES, _CANONICAL, _VERIFICATION


def get_signal_types() -> set:
    """Get the cached set of valid signal types. Loads on first call."""
    types, _, _ = load_signal_types_via_gdu()
    return types


def is_canonical() -> bool:
    """Check if signal types were loaded through verified GDU context."""
    if _SIGNAL_TYPES is None:
        load_signal_types_via_gdu()
    return _CANONICAL


def get_verification() -> dict:
    """Get verification metadata for the loaded signal types."""
    if _SIGNAL_TYPES is None:
        load_signal_types_via_gdu()
    return _VERIFICATION or {}


def reset():
    """Reset cached state (for testing only)."""
    global _SIGNAL_TYPES, _CANONICAL, _VERIFICATION
    _SIGNAL_TYPES = None
    _CANONICAL = False
    _VERIFICATION = None
