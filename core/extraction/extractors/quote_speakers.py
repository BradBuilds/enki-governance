"""
Quote speaker resolution.

Runs after entity resolution. Walks envelope.summary.quotes[] and
fills each quote's speaker_node_id by matching speaker_raw against
the entities already resolved on the envelope. No LLM needed —
speaker_raw is a surface name, the resolved entities carry the same
surface forms.

Fallback: if no entity match, leaves speaker_node_id=None. A later
pass (reconciliation) can try again as the entity graph grows.
"""
from __future__ import annotations

import logging
from difflib import SequenceMatcher

log = logging.getLogger("enki.extraction.quote_speakers")


def _normalize(s: str) -> str:
    return " ".join((s or "").lower().split())


def _best_entity_match(speaker: str, entities: list[dict]) -> dict | None:
    """Find the entity that best matches the speaker name. Exact match
    wins; otherwise pick the highest fuzzy-similarity entity above 0.75."""
    if not speaker or not entities:
        return None
    sp = _normalize(speaker)

    # Pass 1 — exact normalized match
    for e in entities:
        if not isinstance(e, dict):
            continue
        for candidate in (e.get("name_raw"), e.get("name_canonical"),
                          e.get("label")):
            if candidate and _normalize(candidate) == sp:
                return e

    # Pass 2 — substring (speaker "Petrov" matches entity "Ivan Petrov")
    for e in entities:
        if not isinstance(e, dict):
            continue
        for candidate in (e.get("name_raw"), e.get("name_canonical"),
                          e.get("label")):
            if not candidate:
                continue
            cn = _normalize(candidate)
            if sp in cn or cn in sp:
                return e

    # Pass 3 — fuzzy
    best = None
    best_score = 0.75
    for e in entities:
        if not isinstance(e, dict):
            continue
        for candidate in (e.get("name_raw"), e.get("name_canonical"),
                          e.get("label")):
            if not candidate:
                continue
            score = SequenceMatcher(None, sp, _normalize(candidate)).ratio()
            if score > best_score:
                best = e
                best_score = score
    return best


def resolve_quote_speakers(envelope: dict) -> int:
    """Fill speaker_node_id on every quote. Returns the number of
    quotes successfully linked."""
    quotes = (envelope.get("summary") or {}).get("quotes") or []
    if not isinstance(quotes, list) or not quotes:
        return 0
    entities = envelope.get("entities") or []
    resolved_entities = [
        e for e in entities
        if isinstance(e, dict)
        and isinstance(e.get("node_id"), str)
        and e["node_id"].startswith("entity:")
    ]
    if not resolved_entities:
        return 0

    linked = 0
    for q in quotes:
        if not isinstance(q, dict):
            continue
        if q.get("speaker_node_id"):
            continue  # already resolved
        speaker = q.get("speaker_raw") or q.get("speaker") or ""
        match = _best_entity_match(speaker, resolved_entities)
        if match:
            q["speaker_node_id"] = match["node_id"]
            q["speaker_canonical"] = match.get("name_canonical") or match.get("name_raw")
            linked += 1
    return linked
