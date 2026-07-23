"""
Gazetteer loader — looks up places by name against gazetteerpack.

Phase 2.2: replaces the hardcoded ``_KNOWN_COUNTRIES`` set in
``core/gdu/llm_mapper.py``. Loads countries, maritime areas, and named
admin regions from ``libraries/gazetteerpack`` so the mapper can resolve
place names without LDU-P fallback for items that the pack already
covers.

Honors ``ENKI_NO_GAZETTEER_PACK=1`` for the Phase 5 contamination
experiment.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("enki.gdu.gazetteer")


@dataclass(frozen=True)
class GazetteerHit:
    canonical: str
    subtype: str           # country | maritime_area | admin_region
    qid: Optional[str]
    centroid_lat: Optional[float]
    centroid_lng: Optional[float]
    iso2: Optional[str] = None
    iso3: Optional[str] = None
    parent_country: Optional[str] = None


_INDEX: Optional[dict[str, GazetteerHit]] = None
_PACK_VERSION: Optional[str] = None


def _kill_switch_on() -> bool:
    val = (os.environ.get("ENKI_NO_GAZETTEER_PACK") or "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _pack_root() -> Path:
    try:
        from core.gdu.config import PROJECT_ROOT
        return PROJECT_ROOT / "libraries" / "gazetteerpack"
    except Exception:
        return Path(__file__).resolve().parents[2] / "libraries" / "gazetteerpack"


def _load() -> None:
    global _INDEX, _PACK_VERSION
    if _INDEX is not None:
        return

    if _kill_switch_on():
        log.info("ENKI_NO_GAZETTEER_PACK=1 — gazetteerpack disabled")
        _INDEX = {}
        _PACK_VERSION = "disabled"
        return

    index: dict[str, GazetteerHit] = {}
    version = "unknown"
    root = _pack_root()

    try:
        with open(root / "pack.json") as f:
            meta = json.load(f) or {}
            version = meta.get("version", "unknown")
    except Exception as e:
        log.warning("gazetteerpack/pack.json not readable: %s", e)

    # Subtype hint by filename. Files matching one of these prefixes get
    # the corresponding subtype; everything else defaults to the entry's
    # own ``subtype`` field, or "named_area" if absent.
    SUBTYPE_BY_FILENAME = {
        "gazetteerpack.countries.json":   "country",
        "gazetteerpack.maritime.json":    "maritime_area",
        "gazetteerpack.admin.json":       "admin_region",
        "gazetteerpack.cities.json":      "city",
    }

    def _ingest(filename: str, default_subtype: str) -> None:
        try:
            with open(root / "definitions" / filename) as f:
                data = json.load(f) or {}
                content = data.get("content") or data
                for entry in content.get("entries", []):
                    canonical = entry.get("canonical")
                    centroid = entry.get("centroid") or {}
                    subtype = entry.get("subtype") or default_subtype
                    hit = GazetteerHit(
                        canonical=canonical,
                        subtype=subtype,
                        qid=entry.get("qid"),
                        centroid_lat=centroid.get("lat"),
                        centroid_lng=centroid.get("lng"),
                        iso2=entry.get("iso2"),
                        iso3=entry.get("iso3"),
                        parent_country=entry.get("parent_country"),
                    )
                    for alias in entry.get("aliases", []):
                        key = (alias or "").lower().strip()
                        if key:
                            index.setdefault(key, hit)
                    if canonical:
                        index.setdefault(canonical.lower().strip(), hit)
        except FileNotFoundError:
            log.debug("gazetteerpack file %s missing", filename)
        except Exception as e:
            log.warning("gazetteerpack ingest %s failed: %s", filename, e)

    # Iterate every gazetteerpack.*.json shard in alphabetical order.
    # Earlier shards win (insertion-order preserved by dict). Curated
    # files (countries/maritime/admin) come first lexicographically
    # before bulk-fill files like cities.json.
    defs_dir = root / "definitions"
    if defs_dir.exists():
        for shard in sorted(defs_dir.glob("gazetteerpack.*.json")):
            default = SUBTYPE_BY_FILENAME.get(shard.name, "named_area")
            _ingest(shard.name, default)

    _INDEX = index
    _PACK_VERSION = version
    log.info("gazetteerpack loaded: %d aliases (v%s)", len(index), version)


def lookup(name: str) -> Optional[GazetteerHit]:
    """Find a place by surface form. Returns a GazetteerHit or None."""
    if not name:
        return None
    _load()
    return (_INDEX or {}).get(name.lower().strip())


def get_pack_version() -> str:
    _load()
    return _PACK_VERSION or "unknown"


def reload_for_tests() -> None:
    global _INDEX, _PACK_VERSION
    _INDEX = None
    _PACK_VERSION = None
