"""
Mapping file loader — reads YAML mappings, validates shape, stamps lineage.

A mapping document looks like:

    signal_type: signal.environmental.seismic
    aliases: []
    node_type: event
    subtype: earthquake
    description: USGS earthquake event
    field_map:
      payload.properties.title: identity.title_raw
      payload.properties.mag:   _scratch.magnitude
      payload.properties.place: identity.subtitle
      payload.properties.time:  temporal.event_time_epoch_ms
      payload.properties.url:   source.url
      payload.geometry.coordinates: _scratch.coordinates_lonlat
    constants:
      summary.domain_classification: science
    templates:
      summary.headline: "M{_scratch.magnitude} earthquake — {identity.subtitle}"
      summary.narrative: "Magnitude {_scratch.magnitude} earthquake, {identity.subtitle}."
    extractors:
      - places_cheap
      - topics_cheap
    governance:
      author: hand_written
      reviewed: true
      version: 1

Validation is shape-only: we check that the required top-level keys
exist and that field_map entries point at known envelope path prefixes.
We do NOT check that the signal_type exists in signalvocab here — that
check happens at registration time in the worker registry, which is
already wired to reject unknown types.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from core.ldu.lineage import get_lineage_stamp

log = logging.getLogger("enki.extraction.mappings")


# Envelope top-level blocks that field_map targets are allowed to write into.
# _scratch is a pseudo-namespace for intermediate values that templates
# can reference (not written to the final envelope).
_ALLOWED_TARGETS = {
    "node_id", "node_type", "subtype",
    "identity", "summary", "temporal", "spatial",
    "entities", "topics", "events_referenced",
    "source", "media", "evidence", "relationships",
    "completeness", "provenance", "card_actions",
    "_scratch",
}


@dataclass
class MappingDoc:
    """Parsed mapping document + its LDU lineage stamp."""
    signal_type: str
    aliases: tuple[str, ...]
    node_type: str
    subtype: Optional[str]
    description: str

    field_map: dict[str, str]             # payload_path → envelope_or_scratch_path
    constants: dict[str, Any]             # envelope_path → literal value
    templates: dict[str, str]             # envelope_path → "{placeholder} template"
    extractors: tuple[str, ...]           # universal extractor names to run

    author: str                            # 'hand_written' | 'learned'
    reviewed: bool
    version: int
    source_path: Optional[Path]            # file path, or None if in-memory

    # LDU stamp captured at load time
    lineage: dict

    def all_types(self) -> tuple[str, ...]:
        return (self.signal_type, *self.aliases)


def _validate(doc: dict, *, source: str) -> list[str]:
    """Shape-only validation. Returns list of violation strings."""
    violations: list[str] = []

    required = {"signal_type", "field_map"}
    for key in required:
        if key not in doc:
            violations.append(f"{source}: missing required key '{key}'")

    st = doc.get("signal_type")
    if st is not None and not isinstance(st, str):
        violations.append(f"{source}: signal_type must be string")

    fm = doc.get("field_map", {})
    if not isinstance(fm, dict):
        violations.append(f"{source}: field_map must be a dict")
    else:
        for src, dst in fm.items():
            if not isinstance(src, str) or not isinstance(dst, str):
                violations.append(f"{source}: field_map entry {src!r}→{dst!r} must be string→string")
                continue
            head = dst.split(".", 1)[0]
            if head not in _ALLOWED_TARGETS:
                violations.append(
                    f"{source}: field_map target {dst!r} writes to unknown "
                    f"envelope block {head!r} (allowed: {sorted(_ALLOWED_TARGETS)})"
                )

    consts = doc.get("constants", {})
    if consts and not isinstance(consts, dict):
        violations.append(f"{source}: constants must be a dict")

    tmpls = doc.get("templates", {})
    if tmpls and not isinstance(tmpls, dict):
        violations.append(f"{source}: templates must be a dict")

    exts = doc.get("extractors", [])
    if exts and not isinstance(exts, list):
        violations.append(f"{source}: extractors must be a list")

    return violations


def _to_mapping_doc(doc: dict, *, source_path: Optional[Path]) -> MappingDoc:
    gov = doc.get("governance") or {}
    return MappingDoc(
        signal_type=doc["signal_type"],
        aliases=tuple(doc.get("aliases") or []),
        node_type=doc.get("node_type") or "source",
        subtype=doc.get("subtype"),
        description=doc.get("description") or "",
        field_map=dict(doc.get("field_map") or {}),
        constants=dict(doc.get("constants") or {}),
        templates=dict(doc.get("templates") or {}),
        extractors=tuple(doc.get("extractors") or []),
        author=gov.get("author") or "hand_written",
        reviewed=bool(gov.get("reviewed", True)),
        version=int(gov.get("version", 1)),
        source_path=source_path,
        lineage=get_lineage_stamp(),
    )


def load_mapping_file(path: Path) -> Optional[MappingDoc]:
    """Load a single mapping YAML file. Returns None on validation failure
    (and logs the violations) so callers can skip bad files rather than
    crash the import."""
    try:
        with open(path) as f:
            doc = yaml.safe_load(f)
    except Exception as e:
        log.error("failed to read mapping %s: %s", path, e)
        return None

    if not isinstance(doc, dict):
        log.error("mapping %s: top-level must be a YAML mapping", path)
        return None

    violations = _validate(doc, source=str(path))
    if violations:
        for v in violations:
            log.error("mapping validation: %s", v)
        return None

    return _to_mapping_doc(doc, source_path=path)


def load_mapping_dir(directory: Path) -> list[MappingDoc]:
    """Load every *.yaml / *.yml file in a directory (non-recursive).
    Missing directories are not an error — returns empty list."""
    if not directory.exists() or not directory.is_dir():
        return []
    results: list[MappingDoc] = []
    for p in sorted(directory.iterdir()):
        if p.suffix.lower() not in (".yaml", ".yml"):
            continue
        if p.name.startswith("_"):
            continue  # _template.yaml etc.
        mdoc = load_mapping_file(p)
        if mdoc is not None:
            results.append(mdoc)
    return results


def discover_mappings() -> list[MappingDoc]:
    """Return all mappings from both load paths:

        1. core/extraction/workers/mappings/  (hand-written, shipped)
        2. storage/mappings/learned/           (learned, deployment-local)

    Hand-written mappings load first so a learned mapping for the same
    signal_type will override them (learned is assumed to be newer if
    it was promoted). The registry logs the override.
    """
    project_root = Path(__file__).resolve().parents[3]
    hand_dir = project_root / "core" / "extraction" / "workers" / "mappings"
    learned_dir = project_root / "storage" / "mappings" / "learned"

    mappings = load_mapping_dir(hand_dir)
    mappings += load_mapping_dir(learned_dir)
    return mappings
