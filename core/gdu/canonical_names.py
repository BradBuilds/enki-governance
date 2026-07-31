"""
Canonical Names Dictionary — name normalization and alias resolution.

When the LLM extracts an alias like "Bibi" or "B. Stone" they should
all collapse to ONE canonical entity. This module provides the lookup.

Phase 2.1: the alias and sanctions tables previously hardcoded here have
been promoted to ``libraries/canonicalpack`` (versioned, signed, lineage-
stamped on consult). This module is now a thin loader + cache; the data
is the pack's responsibility.

Honors the ``ENKI_NO_CANONICAL_PACK`` environment variable: when set to
"1" or "true", the loader returns empty maps and every name passes
through to the LLM-only path. This is the kill-switch the contamination
experiment uses to measure raw LLM merge quality.

Used by:
  - LLM mapper: normalize names BEFORE admission so collapse is correct
  - Entity validator: pull sanctions/PEP status into validation envelope
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger("enki.gdu.canonical_names")


# ── Pack loading (cached) ──────────────────────────────────────────────────

_CANONICAL: Optional[dict[str, tuple[str, str, Optional[str], str]]] = None
_SANCTIONS: Optional[dict[str, dict]] = None
_PACK_VERSION: Optional[str] = None


def _kill_switch_on() -> bool:
    val = (os.environ.get("ENKI_NO_CANONICAL_PACK") or "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _pack_root() -> Path:
    try:
        from core.gdu.config import PROJECT_ROOT
        return PROJECT_ROOT / "libraries" / "canonicalpack"
    except Exception:
        return Path(__file__).resolve().parents[2] / "libraries" / "canonicalpack"


def _load_pack() -> None:
    global _CANONICAL, _SANCTIONS, _PACK_VERSION
    if _CANONICAL is not None and _SANCTIONS is not None:
        return

    if _kill_switch_on():
        log.info("ENKI_NO_CANONICAL_PACK=1 — canonicalpack disabled")
        _CANONICAL = {}
        _SANCTIONS = {}
        _PACK_VERSION = "disabled"
        return

    canonical: dict[str, tuple[str, str, Optional[str], str]] = {}
    sanctions: dict[str, dict] = {}
    version = "unknown"

    root = _pack_root()
    try:
        with open(root / "pack.json") as f:
            meta = json.load(f) or {}
            version = meta.get("version", "unknown")
    except Exception as e:
        log.warning("canonicalpack/pack.json not readable: %s", e)

    # Load every canonicalpack.*.json shard EXCEPT sanctions (handled below).
    # Phase 6.5: bulk-fill output lands as separate shards
    # (canonicalpack.persons_top.json, canonicalpack.sec_companies.json,
    # etc.) so growth doesn't bloat a single file.
    defs_dir = root / "definitions"
    shard_count = 0
    if defs_dir.exists():
        for shard in sorted(defs_dir.glob("canonicalpack.*.json")):
            if shard.name == "canonicalpack.sanctions.json":
                continue
            try:
                with open(shard) as f:
                    data = json.load(f) or {}
                content = data.get("content") or data
                for entry in content.get("entries", []):
                    cname = entry.get("canonical_name")
                    etype = entry.get("entity_type")
                    qid = entry.get("qid")
                    role = entry.get("role") or ""
                    if not cname or not etype:
                        continue
                    # Always index the canonical_name itself as a key.
                    # Earlier shards win over later (insertion-order
                    # preserved by dict). canonicalpack.aliases.json
                    # is the curated source of truth; bulk-fill
                    # shards back it up without overwriting.
                    canonical_key = cname.lower().strip()
                    if canonical_key:
                        canonical.setdefault(canonical_key,
                                             (cname, etype, qid, role))
                    for alias in entry.get("aliases", []):
                        key = (alias or "").lower().strip()
                        if key:
                            canonical.setdefault(key, (cname, etype, qid, role))
                shard_count += 1
            except Exception as e:
                log.warning("canonicalpack shard %s not loaded: %s",
                            shard.name, e)
    if shard_count == 0:
        log.warning("canonicalpack: no shards found in %s", defs_dir)

    try:
        with open(root / "definitions" / "canonicalpack.sanctions.json") as f:
            data = json.load(f) or {}
            content = data.get("content") or data
            for entry in content.get("entries", []):
                key = (entry.get("key") or "").lower().strip()
                if not key:
                    continue
                sanctions[key] = {
                    "lists": entry.get("lists") or [],
                    "reason": entry.get("reason") or "",
                    "since": entry.get("since") or "",
                }
    except Exception as e:
        log.warning("canonicalpack sanctions not loaded: %s", e)

    _CANONICAL = canonical
    _SANCTIONS = sanctions
    _PACK_VERSION = version
    log.info(
        "canonicalpack loaded: %d aliases, %d sanctions (v%s)",
        len(canonical), len(sanctions), version,
    )


def get_pack_version() -> str:
    """Return the loaded canonicalpack version (or 'disabled' / 'unknown')."""
    _load_pack()
    return _PACK_VERSION or "unknown"


def normalize_to_canonical(raw_name: str) -> Optional[tuple[str, str, Optional[str], str]]:
    """Look up a raw name (any LLM variant) and return canonical info.

    Returns: (canonical_name, type, qid, role) or None if not in dictionary.
    """
    if not raw_name:
        return None
    _load_pack()
    key = raw_name.lower().strip()
    key = re.sub(r"[.,;:!?]+$", "", key)
    return (_CANONICAL or {}).get(key)


def check_sanctions(canonical_name: str) -> Optional[dict]:
    """Check if a canonical name is on any sanctions list.

    Returns: {lists: [...], reason: str, since: str} or None.
    """
    if not canonical_name:
        return None
    _load_pack()
    return (_SANCTIONS or {}).get(canonical_name.lower().strip())


def is_known_entity(raw_name: str) -> bool:
    """Quick check if we have ANY canonical mapping for this name."""
    return normalize_to_canonical(raw_name) is not None


def reload_pack_for_tests() -> None:
    """Test-only: clear the pack cache so a re-read picks up file changes."""
    global _CANONICAL, _SANCTIONS, _PACK_VERSION
    _CANONICAL = None
    _SANCTIONS = None
    _PACK_VERSION = None
