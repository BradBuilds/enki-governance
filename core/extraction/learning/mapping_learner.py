"""
Mapping learner — propose a payload→envelope mapping from an accumulated
shape profile.

The learner is a bank of heuristic rules that match field names (not
values) against envelope slots. Each rule has:
    - a list of candidate payload path patterns (with wildcards)
    - the envelope destination path
    - a confidence weight
    - an optional type constraint (e.g. "must be numeric")

When multiple rules match the same payload path, the highest-confidence
rule wins. When multiple payload paths claim the same envelope slot, the
one observed most frequently (from sample_count) wins.

Unmapped payload fields are dropped into summary.structured_fields so
nothing is lost — the completeness scorer will still count them as
present even if we can't semantically classify them.

Phase 4: when an LLM is available and the heuristic confidence is low,
consult Gemma with the shape profile + envelope schema and merge its
proposed mappings.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

log = logging.getLogger("enki.extraction.learning.learner")


# Heuristic rule bank. Each tuple: (regex_pattern, envelope_dst, confidence,
#   optional_type_hint, optional_extractor_suggestion)
#
# Pattern is a fullmatch regex over a payload path (e.g. 'payload.title',
# 'payload.properties.mag'). Patterns are deliberately permissive because
# real-world payloads use many spellings: title/headline/name/subject,
# published/timestamp/time/ts, url/link/href, etc.
_RULES: list[tuple[str, str, float]] = [
    # Identity — title / headline
    (r"payload\.(title|headline|name|subject|summary_title)", "identity.title_raw", 0.95),
    (r"payload\.(body|description|content|text|body_html|story)", "summary.body_original", 0.85),
    (r"payload\.(summary|abstract|excerpt|teaser|og_description)", "summary.feed_summary", 0.85),
    (r"payload\.(first_paragraph|lead)", "summary.feed_summary_clean", 0.75),
    (r"payload\.(byline|author|creator|reporter|writer)", "identity.byline_raw", 0.90),
    (r"payload\.(subtitle|sub_title|deck)", "identity.subtitle", 0.80),

    # Temporal
    (r"payload\.(published|pub_date|publish_date|date|timestamp|time|ts|created_at|reported_at|event_time)", "temporal.event_time", 0.95),
    (r"payload\.(ingested_at|received_at|fetched_at)", "temporal.ingested_at", 0.85),

    # Source
    (r"payload\.(link|url|href|source_url|canonical_url|permalink)", "source.url", 0.95),
    (r"payload\.(comments_url|discussion_url|hn_url)", "source.discussion_url", 0.80),
    (r"payload\.(outlet|publisher|source_name|site)", "source.outlet_name", 0.85),
    (r"payload\.(language|lang|locale)", "source.language_declared", 0.85),

    # Media
    (r"payload\.(image|image_url|thumbnail|photo|picture|media_url|og_image)", "media.thumbnail", 0.90),

    # Spatial — lat/lon pairs (engine pairs _scratch.lat + _scratch.lon)
    (r"payload\.(lat|latitude|y)", "_scratch.lat", 0.90),
    (r"payload\.(lon|lng|long|longitude|x)", "_scratch.lon", 0.90),
    (r"payload\.geo\.lat", "_scratch.lat", 0.95),
    (r"payload\.geo\.lon", "_scratch.lon", 0.95),
    (r"payload\.(location|place|place_name|locale_name)", "identity.subtitle", 0.60),
    (r"payload\.(coordinates|geometry\.coordinates)", "spatial.coordinates", 0.85),

    # Evidence signals
    (r"payload\.(confidence|score|reliability)", "evidence.confidence", 0.70),
    (r"payload\.(severity|priority|urgency)", "evidence.background_relevance", 0.50),

    # Identifiers — drop into scratch for template use
    (r"payload\.(id|uid|guid|event_id)", "_scratch.raw_id", 0.85),
]


def _compile_rules():
    return [(re.compile(p + r"(\[\])?"), dst, conf) for p, dst, conf in _RULES]


_COMPILED = _compile_rules()


def _match_rules(path: str) -> Optional[tuple[str, float]]:
    """Return (destination, confidence) for the highest-confidence matching
    rule, or None if no rule matches."""
    best: Optional[tuple[str, float]] = None
    for rx, dst, conf in _COMPILED:
        if rx.fullmatch(path):
            if best is None or conf > best[1]:
                best = (dst, conf)
    return best


def _pick_extractors(mapped_paths: set[str]) -> list[str]:
    """Decide which universal extractors this mapping should run, based
    on which text-bearing fields were populated."""
    ext: list[str] = []
    text_paths = {
        "summary.body_original", "summary.feed_summary",
        "summary.feed_summary_clean", "identity.title_raw",
    }
    if mapped_paths & text_paths:
        ext.extend(["places_cheap", "topics_cheap", "entities_cheap", "quotes"])
    return ext


def propose_mapping(profile: dict, *, signal_type: str) -> dict[str, Any]:
    """Turn an accumulated shape_profile into a candidate mapping dict.

    Input: the shape_profile JSONB from learned_mappings.
    Output: a mapping dict matching the declarative YAML schema (minus
    the 'governance' block which the promoter adds on write).
    """
    field_map: dict[str, str] = {}
    claimed: dict[str, tuple[str, float, int]] = {}  # dst → (src, conf, count)

    for src_path, entry in (profile or {}).items():
        match = _match_rules(src_path)
        if match is None:
            continue
        dst, conf = match
        count = entry.get("count", 0)
        if dst in claimed:
            prev_src, prev_conf, prev_count = claimed[dst]
            # Higher confidence wins; on tie, more frequent wins
            if conf > prev_conf or (conf == prev_conf and count > prev_count):
                del field_map[prev_src]
            else:
                continue
        claimed[dst] = (src_path, conf, count)
        field_map[src_path] = dst

    mapped_dsts = {dst for dst in field_map.values()}
    extractors = _pick_extractors(mapped_dsts)

    # Unmapped field paths → structured_fields
    unmapped = [
        p for p in (profile or {}).keys()
        if p not in field_map and not p.endswith("[]")
    ]

    mapping = {
        "signal_type": signal_type,
        "aliases": [],
        "node_type": "source",
        "subtype": None,
        "description": f"Auto-learned mapping for {signal_type}",
        "field_map": field_map,
        "constants": {
            "summary.domain_classification": "other",
            "evidence.epistemic_type": "observation",
            "evidence.confidence": 0.5,
        },
        "templates": {
            "identity.title_display": "{identity.title_raw}",
            "summary.headline": "{identity.title_raw}",
            "summary.narrative": "{summary.feed_summary}",
        },
        "extractors": extractors,
        "_unmapped_paths": unmapped,  # diagnostic, not part of execution
    }
    return mapping
