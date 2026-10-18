"""
DeclarativeWorker — generic worker that executes any MappingDoc.

One worker class parameterized by a mapping. Hand-written and learned
mappings both run through this engine. There is no other code path.

Execution order of cheap_pass:
    1. Copy field_map entries from signal.payload into envelope (or
       _scratch namespace). Supports dotted source paths.
    2. Apply constants (literal values written directly).
    3. Run templates — string formatting with placeholders like
       "{identity.title_display}" or "{_scratch.magnitude}".
    4. Run named universal extractors over the assembled text bundle.
       The text bundle is built from envelope fields that are already
       populated (identity.title_display, summary.feed_summary,
       summary.body_original).
    5. Post-processing: normalize temporal fields (epoch ms → iso),
       spatial fields (lonlat array → {lat, lon}), deterministic node_id.
    6. Record the cheap pass in provenance.

deep_pass is the same Phase-2 stub pattern used by the article worker:
if llm is None, log the no-op and return. Phase 4 will wire Gemma-backed
template synthesis for declarative workers.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

from core.extraction.base import ExtractionWorker
from core.extraction.envelope import (
    add_gap,
    get_path,
    record_pass,
    set_path,
)
from core.extraction.extractors import (
    extract_entities_cheap,
    extract_places_cheap,
    extract_quotes,
    extract_topics_cheap,
)
from core.extraction.mappings.loader import MappingDoc

log = logging.getLogger("enki.extraction.workers.declarative")

# Universal extractor name → callable
_EXTRACTOR_REGISTRY = {
    "entities_cheap": extract_entities_cheap,
    "places_cheap": extract_places_cheap,
    "topics_cheap": extract_topics_cheap,
    "quotes": extract_quotes,
}


def _get_by_path(obj: Any, path: str) -> Any:
    """Read nested dict/list value by dotted path. Supports integer
    segments for list indexing (e.g. 'geometry.coordinates.0' → lon)."""
    if not path:
        return obj
    cur: Any = obj
    for seg in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(seg)
        elif isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(seg)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _normalize_time(value: Any) -> Optional[str]:
    """Best-effort time normalization. Accepts:
       - ISO 8601 string
       - RFC-822 string
       - Unix epoch seconds or milliseconds (int/float)
       Returns ISO 8601 UTC string, or None if unparseable."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            ts = float(value)
            # Heuristic: values > 1e12 are milliseconds
            if ts > 1e12:
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        if isinstance(value, str):
            # Try ISO first
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc).isoformat()
            except Exception:
                pass
            # Try RFC-822
            try:
                dt = parsedate_to_datetime(value)
                if dt:
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt.astimezone(timezone.utc).isoformat()
            except Exception:
                pass
    except Exception:
        pass
    return None


_TEMPLATE_RE = re.compile(r"\{([a-zA-Z0-9_.]+)\}")


def _render_template(template: str, envelope: dict, scratch: dict) -> str:
    """Replace {path} placeholders with values from envelope or _scratch.
    Missing values render as empty string (not a KeyError) so templates
    degrade gracefully."""
    def repl(m: re.Match) -> str:
        path = m.group(1)
        if path.startswith("_scratch."):
            val = _get_by_path(scratch, path[len("_scratch."):])
        else:
            val = get_path(envelope, path)
        return "" if val is None else str(val)
    return _TEMPLATE_RE.sub(repl, template)


def _slug(text: str) -> str:
    if not text:
        text = "empty"
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


class DeclarativeWorker(ExtractionWorker):
    """Worker that executes a mapping document. Every instance carries
    its own MappingDoc; the registry creates one instance per mapping."""

    def __init__(self, mapping: MappingDoc):
        self._mapping = mapping
        # Class-level signal_type / aliases are required by the ABC;
        # we mirror the mapping onto the instance so the runner's
        # worker.signal_type lookups work.
        self.signal_type = mapping.signal_type  # type: ignore[misc]
        self.aliases = mapping.aliases          # type: ignore[misc]

    def cheap_pass(self, signal: dict, envelope: dict) -> dict:
        t0 = time.monotonic()
        filled: list[str] = []
        scratch: dict[str, Any] = {}
        m = self._mapping

        # Stamp node_type / subtype from mapping
        if m.node_type:
            envelope["node_type"] = m.node_type
            filled.append("node_type")
        if m.subtype:
            envelope["subtype"] = m.subtype
            filled.append("subtype")

        # ─── 1. field_map ────────────────────────────────────────────
        for src_path, dst_path in m.field_map.items():
            value = _get_by_path(signal, src_path)
            if value is None:
                add_gap(envelope, dst_path)
                continue

            if dst_path.startswith("_scratch."):
                scratch[dst_path[len("_scratch."):]] = value
                continue

            # Temporal fields: normalize before writing
            if dst_path in ("temporal.event_time", "temporal.ingested_at"):
                iso = _normalize_time(value)
                if iso:
                    set_path(envelope, dst_path, iso)
                    set_path(envelope, "temporal.precision", "second")
                    filled.append(dst_path)
                else:
                    add_gap(envelope, dst_path)
                continue

            # Spatial: allow lonlat list → dict coercion
            if dst_path == "spatial.coordinates" and isinstance(value, (list, tuple)):
                if len(value) >= 2:
                    set_path(envelope, dst_path, {"lon": value[0], "lat": value[1]})
                    filled.append(dst_path)
                continue

            set_path(envelope, dst_path, value)
            filled.append(dst_path)

        # ─── 2. constants ────────────────────────────────────────────
        for dst_path, value in m.constants.items():
            set_path(envelope, dst_path, value)
            filled.append(dst_path)

        # ─── 2b. scratch lat/lon pairing ─────────────────────────────
        # If the mapping wrote both _scratch.lat and _scratch.lon, pair
        # them into spatial.coordinates automatically so mappings don't
        # have to hand-shape a dict.
        if "lat" in scratch and "lon" in scratch and scratch["lat"] is not None and scratch["lon"] is not None:
            try:
                set_path(envelope, "spatial.coordinates",
                         {"lat": float(scratch["lat"]), "lon": float(scratch["lon"])})
                set_path(envelope, "spatial.extraction_method", "feed")
                filled.append("spatial.coordinates")
            except (TypeError, ValueError):
                pass

        # ─── 3. templates ────────────────────────────────────────────
        for dst_path, tmpl in m.templates.items():
            rendered = _render_template(tmpl, envelope, scratch).strip()
            if rendered:
                set_path(envelope, dst_path, rendered)
                filled.append(dst_path)
            else:
                add_gap(envelope, dst_path)

        # ─── 4. universal extractors over the assembled text bundle ──
        text_parts: list[str] = []
        for path in ("identity.title_display", "summary.headline",
                     "summary.narrative", "summary.feed_summary",
                     "summary.body_original", "identity.subtitle"):
            v = get_path(envelope, path, "")
            if isinstance(v, str) and v.strip():
                text_parts.append(v)
        text_bundle = "\n\n".join(text_parts)

        for ext_name in m.extractors:
            fn = _EXTRACTOR_REGISTRY.get(ext_name)
            if fn is None:
                log.warning("mapping %s: unknown extractor %r", m.signal_type, ext_name)
                continue
            try:
                results = fn(text_bundle)
            except Exception as e:
                log.warning("extractor %s failed: %s", ext_name, e)
                continue

            if ext_name == "entities_cheap" and results:
                envelope["entities"].extend(results)
                filled.append("entities")
            elif ext_name == "places_cheap" and results:
                place_text = [p["name_canonical"] for p in results]
                set_path(envelope, "spatial.place_text", place_text)
                if get_path(envelope, "spatial.coordinates") is None:
                    top = results[0]
                    set_path(envelope, "spatial.coordinates",
                             {"lat": top["lat"], "lon": top["lon"]})
                    set_path(envelope, "spatial.extraction_method", "gazetteer")
                    filled.append("spatial.coordinates")
                filled.append("spatial.place_text")
            elif ext_name == "topics_cheap" and results:
                envelope["topics"].extend([
                    {"node_id": None, "label": t["label"], "weight": t["weight"]}
                    for t in results
                ])
                filled.append("topics")
            elif ext_name == "quotes" and results:
                set_path(envelope, "summary.quotes", results)
                filled.append("summary.quotes")

        # ─── 5. deterministic node_id ────────────────────────────────
        seed_parts = [
            m.signal_type,
            str(get_path(envelope, "source.url") or ""),
            str(get_path(envelope, "identity.title_raw") or ""),
            str(get_path(envelope, "temporal.event_time") or ""),
        ]
        nid = f"{m.node_type}:{_slug('|'.join(seed_parts))}"
        envelope["node_id"] = nid
        filled.append("node_id")

        record_pass(
            envelope,
            stage="cheap",
            fields_filled=filled,
            model=None,
            prompt_version=f"declarative:{m.signal_type}:v{m.version}",
            duration_ms=int((time.monotonic() - t0) * 1000),
            success=True,
        )
        return envelope

    def deep_pass(
        self,
        signal: dict,
        envelope: dict,
        llm: Optional[Any] = None,
    ) -> dict:
        t0 = time.monotonic()
        if llm is None:
            record_pass(
                envelope,
                stage="deep",
                fields_filled=[],
                model=None,
                duration_ms=int((time.monotonic() - t0) * 1000),
                success=False,
                error="llm_unavailable_phase2_stub",
            )
            return envelope
        # TODO(Phase 4): LLM fills template placeholders that couldn't
        # be rendered cheaply (e.g. synthesize narrative from body when
        # mapping has no body field).
        record_pass(
            envelope, stage="deep", fields_filled=[],
            model=getattr(llm, "model_name", "unknown"),
            duration_ms=int((time.monotonic() - t0) * 1000),
            success=True,
        )
        return envelope
