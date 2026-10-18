"""
Mapping writer — materialize a declarative worker YAML from an LDU-P
promotion. When an operator promotes an unknown signal type to a canonical
GDU type and chooses "write mapping", this module inspects the entry's
extracted_structure + proposed_mapping and emits a YAML file under
core/extraction/workers/mappings/ so future signals of this shape get
handled by the existing DeclarativeWorker.

Key design rule: we ONLY write files here. Registering the new mapping
with the worker registry is a separate concern (the registry picks up
files on reload). Nothing in this module touches the DB.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("enki.extraction.mapping_writer")

MAPPINGS_DIR = Path(__file__).resolve().parent / "mappings"

# Envelope slots that the cheap pass understands. Any field_map target
# that isn't one of these gets routed through _scratch so deep pass /
# templates can still reference it.
_KNOWN_SLOTS = {
    "identity.canonical_name",
    "identity.title_raw",
    "identity.title_display",
    "identity.language",
    "summary.headline",
    "summary.narrative",
    "summary.domain_classification",
    "temporal.event_time",
    "temporal.observed_time",
    "spatial.latitude",
    "spatial.longitude",
    "spatial.place_name",
    "source.outlet_name",
    "source.url",
    "source.language_detected",
    "evidence.epistemic_type",
    "evidence.confidence",
    "evidence.background_relevance",
}


def _slug(text: str) -> str:
    """Filesystem-safe slug for a signal type id."""
    s = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip().lower()).strip("_")
    return s or "untitled"


def _guess_field_map(
    extracted_structure: dict,
    proposed_mapping: dict,
) -> dict[str, str]:
    """Derive a YAML-shaped field_map from the LDU-P proposed_mapping.

    proposed_mapping may carry a `field_map` already (if the learner or
    the operator filled one in). If not, we heuristically route common
    payload keys to envelope slots.
    """
    fm = proposed_mapping.get("field_map") if isinstance(proposed_mapping, dict) else None
    if isinstance(fm, dict) and fm:
        # Trust operator/learner output. Prefix unknown targets with _scratch.
        out: dict[str, str] = {}
        for k, v in fm.items():
            if not isinstance(k, str) or not isinstance(v, str):
                continue
            key = k if k.startswith("payload.") else f"payload.{k}"
            target = v if v in _KNOWN_SLOTS or v.startswith("_scratch.") else f"_scratch.{v}"
            out[key] = target
        return out

    # Heuristic fallback — scan extracted_structure keys and guess.
    guesses: dict[str, str] = {}
    for k in (extracted_structure or {}).keys():
        lk = k.lower()
        if lk in ("title", "name", "headline", "subject"):
            guesses[f"payload.{k}"] = "identity.title_raw"
        elif lk in ("summary", "description", "body", "text"):
            guesses[f"payload.{k}"] = "summary.narrative"
        elif lk in ("lat", "latitude"):
            guesses[f"payload.{k}"] = "spatial.latitude"
        elif lk in ("lon", "lng", "longitude"):
            guesses[f"payload.{k}"] = "spatial.longitude"
        elif lk in ("url", "link", "href"):
            guesses[f"payload.{k}"] = "source.url"
        elif lk in ("timestamp", "ts", "time", "published_at", "event_time"):
            guesses[f"payload.{k}"] = "temporal.event_time"
        elif lk in ("source", "outlet", "feed_title"):
            guesses[f"payload.{k}"] = "source.outlet_name"
        else:
            guesses[f"payload.{k}"] = f"_scratch.{_slug(k)}"
    return guesses


def write_mapping_yaml(
    *,
    signal_type: str,
    extracted_structure: dict,
    proposed_mapping: dict,
    author: str = "operator_promotion",
    node_type: str = "event",
    subtype: Optional[str] = None,
    description: Optional[str] = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Write a declarative mapping YAML for a promoted signal type.

    Returns {"path": str, "wrote": bool, "skipped_reason"?: str}.
    Does not raise on skip — returns a descriptive reason instead so the
    promotion API can surface it to the UI without rolling back.
    """
    if not signal_type:
        return {"path": "", "wrote": False, "skipped_reason": "no_signal_type"}

    fname = f"{_slug(signal_type.replace('signal.', ''))}.yaml"
    path = MAPPINGS_DIR / fname

    if path.exists() and not overwrite:
        return {"path": str(path), "wrote": False, "skipped_reason": "exists"}

    field_map = _guess_field_map(extracted_structure or {}, proposed_mapping or {})
    sub = subtype or _slug(signal_type.split(".")[-1]) or "generic"

    # Build YAML manually — no yaml dep needed, and this shape is simple
    # enough that the formatting is stable. Matches the style of the
    # hand-written mappings alongside this file.
    lines: list[str] = []
    lines.append(f"# {signal_type} — auto-generated from operator promotion.")
    lines.append(f"# Review and hand-tune the field_map / templates as needed.")
    lines.append("")
    lines.append(f"signal_type: {signal_type}")
    lines.append("")
    lines.append(f"node_type: {node_type}")
    lines.append(f"subtype: {sub}")
    if description:
        lines.append(f"description: {description!r}")
    else:
        lines.append(f"description: Auto-generated from LDU-P promotion")
    lines.append("")
    lines.append("field_map:")
    if field_map:
        width = max((len(k) for k in field_map), default=0) + 2
        for src, tgt in field_map.items():
            lines.append(f"  {src:<{width}} {tgt}")
    else:
        lines.append("  {}")
    lines.append("")
    lines.append("constants:")
    lines.append("  evidence.epistemic_type: observation")
    lines.append("  evidence.confidence: 0.85")
    lines.append("  evidence.background_relevance: background")
    lines.append("")
    lines.append("templates:")
    lines.append('  identity.title_display: "{identity.title_raw}"')
    lines.append('  summary.headline: "{identity.title_raw}"')
    lines.append("")
    lines.append("extractors: []")
    lines.append("")
    lines.append("governance:")
    lines.append(f"  author: {author}")
    lines.append("  reviewed: false")
    lines.append("  version: 1")
    lines.append("")

    try:
        MAPPINGS_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
        log.info("wrote mapping yaml: %s", path)
        return {"path": str(path), "wrote": True}
    except Exception as e:
        log.exception("mapping yaml write failed")
        return {"path": str(path), "wrote": False, "skipped_reason": f"write_failed:{e}"}
