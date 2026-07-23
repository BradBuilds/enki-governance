"""
Topic ontology classifier.

The current topic_extractor.py is a substring keyword-bag. It works
for obvious hits but never builds a hierarchy. This classifier
replaces it with an LLM pass that maps each article to ONE OR MORE
canonical topics drawn from the nodevocab `topic` subtypes:

    conflict, operation, treaty, protocol, technology, ideology,
    market, supply_chain, program, doctrine

And can nest: "PAC-3 procurement" classifies as:
    topic.program  (parent: topic.supply_chain)  (parent: topic.doctrine)

The classifier is additive — it runs AFTER the cheap keyword bag
so if keyword match already flagged "russia_ukraine_war" we don't
overwrite it; we add canonical parents above it.

Writes to `topics` master table via the existing topic_resolver.
Populates envelope.topics[] with node_ids so edges get created.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("enki.extraction.topic_ontology")

CANONICAL_TOPICS = {
    "conflict": "Armed conflict or military tensions between states/groups",
    "operation": "Named military/security operation or campaign",
    "treaty": "Formal agreement between states or parties",
    "protocol": "Technical or procedural standard",
    "technology": "Specific technology, platform, or capability",
    "ideology": "Political/religious/philosophical doctrine",
    "market": "Economic market, sector, or financial activity",
    "supply_chain": "Production, sourcing, or distribution network",
    "program": "Named government/corporate program or initiative",
    "doctrine": "Strategic or organizational doctrine",
}


_PROMPT = """Classify this article into the canonical topic taxonomy.

For each applicable topic, choose ONE canonical label from this list and optionally a short specific label.

Canonical topics:
{topics}

Return JSON: {{"topics": [{{"canonical": "<one of the list>", "specific": "<short label for this specific instance, e.g. 'Russia-Ukraine War' under 'conflict'>", "confidence": 0.0-1.0}}]}}

Rules:
1. Max 5 topics. Pick only the central themes.
2. "canonical" MUST be one of the listed labels.
3. "specific" should be a short human-readable instance label (4-40 chars).
4. If no clear topic applies, return {{"topics": []}}.
5. Return ONLY the JSON.

Article title: {title}
Article body:
{body}"""


def classify_topics(
    envelope: dict,
    *,
    llm: Optional[Any] = None,
) -> list[dict]:
    if not llm:
        return []
    body = (
        (envelope.get("summary") or {}).get("body_original")
        or (envelope.get("summary") or {}).get("narrative")
        or ""
    )
    title = (envelope.get("identity") or {}).get("title_display") or ""
    if not body or len(body) < 120:
        return []

    topics_str = "\n".join(f"  - {k}: {v}" for k, v in CANONICAL_TOPICS.items())
    prompt = _PROMPT.format(
        topics=topics_str, title=title[:200], body=body[:3000],
    )

    try:
        result = llm.complete_json(
            prompt, max_tokens=500, temperature=0.0,
            prompt_version="topic_ontology_v1", timeout=90.0,
        )
    except Exception as e:
        log.debug("topic ontology LLM failed: %s", e)
        return []

    if not result.success or not isinstance(result.parsed, dict):
        return []
    raw = result.parsed.get("topics") or []

    out = []
    for t in raw[:5]:
        if not isinstance(t, dict):
            continue
        canonical = (t.get("canonical") or "").strip().lower()
        if canonical not in CANONICAL_TOPICS:
            continue
        specific = (t.get("specific") or "").strip()[:80]
        conf = float(t.get("confidence", 0.7) or 0.7)
        # Emit BOTH the canonical topic and the specific child topic.
        # This lets the graph carry (article)-[about]-(specific)-[instance_of]-(canonical).
        out.append({
            "label": canonical,
            "weight": conf * 0.9,     # slightly lower — canonical is abstract
            "_canonical": True,
        })
        if specific and specific.lower() != canonical:
            out.append({
                "label": specific,
                "weight": conf,
                "_canonical": False,
                "parent_label": canonical,
            })
    return out


def merge_into_envelope(envelope: dict, classified: list[dict]) -> None:
    if not classified:
        return
    existing = envelope.get("topics") or []
    seen_labels = {
        (t.get("label") or "").strip().lower()
        for t in existing if isinstance(t, dict)
    }
    for t in classified:
        if t["label"].lower() in seen_labels:
            continue
        existing.append({
            "label": t["label"],
            "weight": t["weight"],
        })
        seen_labels.add(t["label"].lower())
    envelope["topics"] = existing
