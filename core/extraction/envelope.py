"""
Envelope helpers — build, read, write, validate, stamp.

The canonical envelope shape is defined in the envelopevocab pack v1 at
libraries/envelopevocab/definitions/envelopevocab.envelope_v1.json. This
module provides:

    build_empty(node_type, *, signal_rid=None) → dict
        fresh envelope with the full field tree pre-populated with
        nulls/empty arrays/empty strings and a governance block
        stamped via core.ldu.lineage.

    get_path(env, "summary.narrative") → Any
    set_path(env, "summary.narrative", text) → None
        dotted-path accessors for nested reads/writes.

    validate_shape(env) → list[str]
        shape-only validation (required keys exist). Does NOT check
        non-emptiness; that is the completeness scorer's job (Phase 8).
        Returns a list of violations; empty = valid.

    record_pass(env, stage, *, fields_filled, model=None, ...) → None
        append an extraction pass entry to
        envelope["provenance"]["extraction_passes"].

Design note — validation strictness:
    The schema marks many fields `required: true`, but a cheap pass over
    a garbage-title HN entry cannot HONESTLY populate `summary.narrative`.
    We handle this by initializing every required string to "" and every
    required array to [], so the SHAPE is always complete. Empty content
    is then recorded in completeness.gaps and the completeness scorer
    (Phase 8) grades the envelope. validate_shape() checks structure,
    not content.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from core.ldu.lineage import get_lineage_stamp

log = logging.getLogger("enki.extraction.envelope")

ENVELOPE_VERSION = "v1"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_empty(node_type: str = "source", *, signal_rid: Optional[int] = None) -> dict:
    """Construct a fresh envelope with the full v1 field tree initialized.

    All required strings are "", all required arrays are [], all optional
    fields are None. The governance block is stamped live from the LDU
    lineage helper so the envelope is auditable from the moment it exists.
    """
    stamp = get_lineage_stamp(ldu_t_id=signal_rid)

    now = _utcnow_iso()

    return {
        "envelope_version": ENVELOPE_VERSION,
        "node_id": "",  # filled by worker / runner once identity is known
        "node_type": node_type,
        "subtype": None,

        "governance": {
            "gdu_canonical": bool(stamp.get("gdu_canonical")),
            "active_set_version": stamp.get("active_set_version") or "",
            "pack_versions": stamp.get("pack_versions") or {},
            "ldu_t_id": stamp.get("ldu_t_id"),
            "ldu_p_id": None,
            "admitted_at": stamp.get("stamped_at") or now,
        },

        "identity": {
            "title_raw": None,
            "title_display": "",
            "title_source": "feed",
            "subtitle": None,
            "canonical_name": None,
            "aliases": [],
            "byline_raw": None,
            "byline_resolved": None,
        },

        "summary": {
            "headline": "",
            "narrative": "",
            "key_facts": [],
            "quotes": [],
            "feed_summary": None,
            "feed_summary_clean": None,
            "feed_summary_translated": None,
            "body_original": None,
            "body_translated": None,
            "structured_fields": None,
            "domain_classification": "other",
        },

        "temporal": {
            "event_time": now,
            "ingested_at": now,
            "precision": "unknown",
        },

        "spatial": {
            "coordinates": None,
            "bbox": None,
            "place_text": [],
            "resolved_places": [],
            "extraction_method": "none",
        },

        "entities": [],
        "topics": [],
        "events_referenced": [],

        "source": {
            "node_id": "",
            "url": "",
            "discussion_url": None,
            "outlet_name": "",
            "language_declared": None,
            "language_detected": None,
            "language_displayed": None,
            "editorial_stance": "unknown",
            "fetched_at": None,
        },

        "media": {
            "thumbnail": None,
            "thumbnail_source": "none",
            "images": [],
            "videos": [],
            "audio": [],
            "embeds": [],
        },

        "evidence": {
            "epistemic_type": "observation",
            "confidence": 0.5,
            "source_count": 1,
            "corroborations": 0,
            "contradictions": 0,
            "background_relevance": "background",
            "relevance_to_active_cases": [],
            "assessment_nodes": [],
        },

        "relationships": {
            "mentions": [],
            "cites": [],
            "cited_by": [],
            "follows": None,
            "supersedes": None,
            "superseded_by": None,
            "part_of": [],
        },

        "completeness": {
            "score": 0.0,
            "tier": "empty",
            "filled_slots": 0,
            "total_slots": 0,
            "gaps": [],
        },

        "provenance": {
            "extraction_passes": [],
            "human_edits": [],
        },

        "card_actions": [],
    }


# ─── dotted path accessors ─────────────────────────────────────────────────

def get_path(env: dict, path: str, default: Any = None) -> Any:
    """Read env['a']['b']['c'] via 'a.b.c'. Returns default if any segment
    is missing."""
    cur: Any = env
    for seg in path.split("."):
        if not isinstance(cur, dict) or seg not in cur:
            return default
        cur = cur[seg]
    return cur


def set_path(env: dict, path: str, value: Any) -> None:
    """Write env['a']['b']['c'] = value via 'a.b.c'. Creates intermediate
    dicts if missing."""
    segs = path.split(".")
    cur: dict = env
    for seg in segs[:-1]:
        if seg not in cur or not isinstance(cur[seg], dict):
            cur[seg] = {}
        cur = cur[seg]
    cur[segs[-1]] = value


# ─── shape validation ──────────────────────────────────────────────────────

_REQUIRED_TOP = {
    "envelope_version", "node_id", "node_type",
    "governance", "identity", "summary", "temporal", "spatial",
    "entities", "topics", "source", "evidence", "relationships",
    "completeness", "provenance",
}

_REQUIRED_SUB = {
    "governance": {"gdu_canonical", "active_set_version", "pack_versions", "admitted_at"},
    "identity": {"title_display"},
    "summary": {"headline", "narrative"},
    "temporal": {"event_time", "ingested_at"},
    "source": {"node_id", "url", "outlet_name"},
    "evidence": {"epistemic_type", "confidence"},
    "completeness": {"score", "tier", "filled_slots", "total_slots", "gaps"},
    "provenance": {"extraction_passes"},
}


def validate_shape(env: dict) -> list[str]:
    """Shape-only validation. Returns list of violations; empty = valid.

    Only checks that required keys EXIST (not that they have content).
    Empty strings, empty arrays, and nulls all pass — the completeness
    scorer grades content richness separately.
    """
    violations: list[str] = []

    for key in _REQUIRED_TOP:
        if key not in env:
            violations.append(f"missing top-level field: {key}")

    for container, req_keys in _REQUIRED_SUB.items():
        block = env.get(container)
        if not isinstance(block, dict):
            violations.append(f"{container}: not a dict")
            continue
        for sub in req_keys:
            if sub not in block:
                violations.append(f"{container}.{sub}: missing")

    # Type sanity checks for the collections that runners append to
    for coll in ("entities", "topics", "events_referenced", "card_actions"):
        if coll in env and not isinstance(env[coll], list):
            violations.append(f"{coll}: not a list")

    return violations


# ─── provenance pass logging ───────────────────────────────────────────────

def record_pass(
    env: dict,
    stage: str,
    *,
    fields_filled: list[str],
    model: Optional[str] = None,
    model_version: Optional[str] = None,
    prompt_version: Optional[str] = None,
    duration_ms: int = 0,
    success: bool = True,
    error: Optional[str] = None,
) -> dict:
    """Append an extraction pass entry to the envelope's provenance.

    This is the in-envelope mirror of the extraction_passes DB table.
    Envelope and DB table are kept in sync by the runner: every call
    that writes a DB row also calls record_pass().
    """
    entry = {
        "stage": stage,
        "ts": _utcnow_iso(),
        "model": model,
        "model_version": model_version,
        "prompt_version": prompt_version,
        "fields_filled": list(fields_filled),
        "duration_ms": int(duration_ms),
        "success": bool(success),
        "error": error,
    }
    env.setdefault("provenance", {}).setdefault("extraction_passes", []).append(entry)
    return entry


def add_gap(env: dict, field_path: str) -> None:
    """Record that a required field could not be filled. Deduplicated."""
    gaps = env.setdefault("completeness", {}).setdefault("gaps", [])
    if field_path not in gaps:
        gaps.append(field_path)


def build_envelope_summary(envelope: dict | None) -> dict | None:
    """Compact, card-ready projection of an envelope.

    This is the API contract for card rendering across list views. The
    frontend reads from `envelope_summary` (never from flat denormalized
    fields) so that adding a new chip type (media, outlet, pattern, …)
    requires only an envelope-stage change, not an endpoint-by-endpoint
    schema migration.

    Returns None if given None so callers can pass the result through
    without defensive checks.

    Shape:
        {
          "identity": {"title_display", "canonical_name"},
          "temporal": {"event_time", "precision"},
          "spatial":  {
            "place_text": [...],
            "resolved_places": [{"node_id", "label"}],
            "coordinates": {"lat", "lon"} | None,
          },
          "entities": [{"node_id", "label", "role", "weight"}],  # all stamped entity:* refs
          "topics":   [{"node_id", "label", "weight"}],
          "events":   [{"node_id"}],                             # events_referenced[]
          "source":   {"outlet_name", "url"},
          "evidence": {"epistemic_type", "source_count",
                       "corroborations", "contradictions"},
          "completeness": {"score", "tier"},
        }

    Payload discipline: typical article envelope → ~500-1500 bytes summary.
    Full envelope is often 5-20KB. Use build_envelope_summary() on list
    endpoints; reserve full envelope for detail endpoints.
    """
    if not envelope:
        return None

    def g(path, default=None):
        return get_path(envelope, path, default)

    # entities[] — keep every entity with a stamped entity:* node_id;
    # the resolver is authoritative. Strip raw extraction debris.
    raw_entities = envelope.get("entities") or []
    entities: list[dict] = []
    for e in raw_entities:
        if not isinstance(e, dict):
            continue
        nid = e.get("node_id")
        if not (isinstance(nid, str) and nid.startswith("entity:")):
            continue
        label = (e.get("label") or e.get("name_raw")
                 or e.get("canonical_name") or nid)
        entities.append({
            "node_id": nid,
            "label":   label,
            "role":    e.get("role"),
            "weight":  e.get("weight"),
        })

    raw_topics = envelope.get("topics") or []
    topics: list[dict] = []
    for t in raw_topics:
        if not isinstance(t, dict):
            continue
        nid = t.get("node_id")
        if not (isinstance(nid, str) and nid.startswith("topic:")):
            continue
        label = t.get("label") or t.get("canonical_name") or nid
        topics.append({
            "node_id": nid,
            "label":   label,
            "weight":  t.get("weight"),
        })

    # Resolved places — envelope.spatial.resolved_places is either a flat
    # list of "place:N" strings (legacy) or a list of dicts. Normalize.
    raw_places = (envelope.get("spatial") or {}).get("resolved_places") or []
    resolved_places: list[dict] = []
    place_text = (envelope.get("spatial") or {}).get("place_text") or []
    for i, p in enumerate(raw_places):
        if isinstance(p, str) and p.startswith("place:"):
            # Pair with matching place_text entry if available
            label = place_text[i] if i < len(place_text) else p
            resolved_places.append({"node_id": p, "label": label})
        elif isinstance(p, dict):
            nid = p.get("node_id")
            if isinstance(nid, str) and nid.startswith("place:"):
                resolved_places.append({
                    "node_id": nid,
                    "label":   p.get("label") or p.get("name") or nid,
                })

    # events_referenced — just node_ids; the consumer fetches details
    events = [
        {"node_id": e}
        for e in (envelope.get("events_referenced") or [])
        if isinstance(e, str) and e.startswith("event:")
    ]

    # media.images[] — Phase 4B. Populated by the runner's media stage
    # with items shaped {node_id, url, width, height, role}. Legacy
    # envelopes with only media.thumbnail (URL string) produce nothing here.
    raw_media = (envelope.get("media") or {}).get("images") or []
    media: list[dict] = []
    for m in raw_media:
        if not isinstance(m, dict):
            continue
        nid = m.get("node_id")
        if not (isinstance(nid, str) and nid.startswith("media:")):
            continue
        media.append({
            "node_id": nid,
            "url":     m.get("url"),
            "width":   m.get("width"),
            "height":  m.get("height"),
            "role":    m.get("role"),
        })

    coords = (envelope.get("spatial") or {}).get("coordinates")
    if not isinstance(coords, dict) or coords.get("lat") is None:
        coords = None

    return {
        "identity": {
            "title_display":  g("identity.title_display"),
            "canonical_name": g("identity.canonical_name"),
        },
        "temporal": {
            "event_time": g("temporal.event_time"),
            "precision":  g("temporal.precision"),
        },
        "spatial": {
            "place_text":      list(place_text),
            "resolved_places": resolved_places,
            "coordinates":     coords,
        },
        "entities": entities,
        "topics":   topics,
        "events":   events,
        "media":    media,
        "source": {
            "outlet_name": g("source.outlet_name"),
            "url":         g("source.url"),
        },
        "evidence": {
            "epistemic_type":  g("evidence.epistemic_type"),
            "source_count":    g("evidence.source_count"),
            "corroborations":  g("evidence.corroborations"),
            "contradictions":  g("evidence.contradictions"),
        },
        "completeness": {
            "score": g("completeness.score"),
            "tier":  g("completeness.tier"),
        },
    }
