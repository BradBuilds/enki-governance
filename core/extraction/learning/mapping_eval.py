"""
Mapping evaluator — score a proposed mapping by running it against the
samples recorded in a shape profile.

Scoring is coverage-based: how many of a fixed set of "desirable"
envelope fields does the proposed mapping populate, averaged over the
samples drawn from the profile. Because the profile only stores small
sample values (not full signals), the evaluator reconstructs synthetic
signals by unflattening the sample values back into a payload dict.

This is a lightweight gate, not a ground-truth metric. It answers: "does
this proposal produce non-empty envelopes on the shape we've observed?"
A human reviewer (or Phase 4 LLM) still judges semantic correctness
before promotion.
"""

from __future__ import annotations

import logging
from typing import Any

from core.extraction.envelope import build_empty, get_path
from core.extraction.mappings.loader import MappingDoc, _to_mapping_doc
from core.extraction.workers.declarative import DeclarativeWorker

log = logging.getLogger("enki.extraction.learning.eval")

# Envelope paths that count toward the coverage score.
_SCORED_PATHS = [
    "identity.title_display",
    "summary.headline",
    "summary.narrative",
    "temporal.event_time",
    "source.url",
    "source.outlet_name",
    "spatial.coordinates",
    "media.thumbnail",
    "identity.byline_raw",
]


def _unflatten_sample(profile: dict, index: int = 0) -> dict:
    """Reconstruct a synthetic signal payload from profile samples.

    The profile stores {path: {samples: [...]}}. We rebuild the nested
    dict by walking each path and inserting the first sample value at
    each position.
    """
    payload: dict[str, Any] = {}
    for path, entry in (profile or {}).items():
        if not path.startswith("payload."):
            continue
        samples = entry.get("samples") or []
        if not samples:
            continue
        value = samples[min(index, len(samples) - 1)]
        # Strip the 'payload.' prefix and walk the remaining segments.
        segs = path[len("payload."):].split(".")
        cur = payload
        for seg in segs[:-1]:
            if seg.endswith("[]"):
                seg = seg[:-2]
            if seg not in cur or not isinstance(cur[seg], dict):
                cur[seg] = {}
            cur = cur[seg]
        last = segs[-1]
        if last.endswith("[]"):
            last = last[:-2]
        cur[last] = value
    return payload


def evaluate_candidate(mapping_dict: dict, profile: dict) -> float:
    """Run the proposed mapping against synthetic samples reconstructed
    from the profile. Return a coverage score in [0.0, 1.0]."""
    try:
        # Temporarily coerce the candidate dict into a MappingDoc using
        # the same converter the loader uses. We patch a fake 'governance'
        # block so _to_mapping_doc doesn't fail.
        doc_in = dict(mapping_dict)
        doc_in.setdefault("governance", {"author": "learned", "reviewed": False, "version": 0})
        mdoc: MappingDoc = _to_mapping_doc(doc_in, source_path=None)
    except Exception as e:
        log.warning("evaluate_candidate: failed to build MappingDoc: %s", e)
        return 0.0

    worker = DeclarativeWorker(mdoc)

    # Try a few sample indices to average noise
    scores: list[float] = []
    for idx in range(3):
        payload = _unflatten_sample(profile, index=idx)
        if not payload:
            continue
        synthetic = {
            "type": mapping_dict.get("signal_type") or "unknown",
            "signal_rid": None,
            "payload": payload,
            "source": {"outlet": "synthetic", "slug": "synthetic"},
        }
        envelope = build_empty(node_type=mdoc.node_type)
        try:
            envelope = worker.cheap_pass(synthetic, envelope)
        except Exception as e:
            log.debug("cheap_pass failed during eval: %s", e)
            continue

        filled = 0
        for path in _SCORED_PATHS:
            v = get_path(envelope, path)
            if isinstance(v, str) and v.strip():
                filled += 1
            elif isinstance(v, (list, dict)) and v:
                filled += 1
            elif v not in (None, ""):
                filled += 1
        scores.append(filled / len(_SCORED_PATHS))

    return round(sum(scores) / len(scores), 3) if scores else 0.0
