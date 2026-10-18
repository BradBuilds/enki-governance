"""Connect ANY library to the collapse/expansion grid by its declared type.

One general entry point — `connect_library` dispatches on `pack.json: type`:

  lexical  → collapse every sense through the substrate (collapse_library):
             forms → concept@deterministic-address → bindings → relation edges.
  policy   → loaded at runtime by the equation engine (weights/thresholds); not
             collapsed, but acknowledged so it's never silently ignored.
  *vocab   → governance packs (entityvocab, edgevocab, …): they GOVERN collapse,
             they aren't collapsed against. Reported, not ingested.

`connect_all` walks libraries/ and connects each — so "add a library" is one call
and the whole set is validated uniformly. Fail-closed: an unknown type is reported
as unhandled, never guessed.
"""
from __future__ import annotations

import json
from pathlib import Path

from core.language.library_collapse import collapse_library_copy

LEXICAL = "lexical"
POLICY = "policy"


def _lexical_senses(root: Path, pack: dict):
    """Senses for a lexical pack, or None if its (generated) data isn't present."""
    senses = []
    for rel in pack.get("manifest", []):
        f = root / rel
        if not f.exists():
            return None  # generated definition not on this node (e.g. wordnet) — skip
        senses.extend(json.loads(f.read_text()).get("content", {}).get("senses", []))
    return senses


def connect_library(pack_root: str | Path, get_conn, phase: str = "all") -> dict:
    root = Path(pack_root)
    pack = json.loads((root / "pack.json").read_text())
    typ = pack.get("type", "unknown")
    lib_id = pack.get("id", root.name)

    if typ == LEXICAL:
        senses = _lexical_senses(root, pack)
        if senses is None:
            return {"id": lib_id, "type": typ, "action": "skipped_missing_data"}
        s = collapse_library_copy(senses, lib_id, get_conn, phase=phase)
        return {"id": lib_id, "type": typ, "action": "collapsed", **s}

    if typ == POLICY:
        return {"id": lib_id, "type": typ, "action": "runtime_policy"}

    # everything else is governance vocab — governs collapse, isn't collapsed.
    return {"id": lib_id, "type": typ, "action": "governance"}


def connect_all(libraries_root: str | Path, get_conn) -> list:
    """Two sweeps: LOAD every library's senses/forms/bindings, THEN LINK relations —
    so a relation can resolve a target sense in ANY library (cross-library edges)
    because all concepts exist before the link pass runs."""
    root = Path(libraries_root)
    dirs = [d for d in sorted(p for p in root.iterdir() if p.is_dir())
            if (d / "pack.json").exists()]

    results, lexical = [], []
    for d in dirs:  # sweep 1 — load
        try:
            r = connect_library(d, get_conn, phase="load")
            results.append(r)
            if r.get("action") == "collapsed":
                lexical.append((d, r))
        except Exception as e:
            results.append({"id": d.name, "type": "?", "action": "error",
                            "error": str(e)[:120]})

    for d, r in lexical:  # sweep 2 — link (cross-library targets now exist)
        try:
            link = connect_library(d, get_conn, phase="link")
            r["relations"] = link.get("relations", 0)
        except Exception as e:
            r["link_error"] = str(e)[:120]
    return results
