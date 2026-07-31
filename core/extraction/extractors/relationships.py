"""
Semantic relationship extractor.

Universal LLM pass that reads an article body and its resolved
entities, returns structured (subject, verb, object, evidence) triples.
Runs after entity resolution so it can reference nodes by canonical id.

Output shape feeds `envelope.relationships.custom[]` which the edge
writer (core/extraction/edges.py) persists as knowledge_edges. This
turns a pile of co-occurring names into an actual graph of semantic
relationships.

Allowed relationship vocabulary — a small curated set, deliberately
narrower than the English verb space so downstream queries aren't
fragmented by synonyms:

    met_with, agreed_with, accused, denied, attacked,
    supported, opposed, sanctioned, acquired, merged_with,
    sold_to, founded, leads, works_for, represents, funded,
    supplied, transported, visited, arrived_at, departed,
    arrested, convicted, appointed, fired, resigned,
    mentioned, about

The LLM must pick from this list — unknown verbs are dropped. If it
sees a relationship we haven't catalogued, it should pick the closest
match (e.g. "partnered_with" → "agreed_with"; "bought" → "acquired").
"""
from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("enki.extraction.relationships")

ALLOWED_RELATIONSHIPS = {
    "met_with", "agreed_with", "accused", "denied", "attacked",
    "supported", "opposed", "sanctioned", "acquired", "merged_with",
    "sold_to", "founded", "leads", "works_for", "represents", "funded",
    "supplied", "transported", "visited", "arrived_at", "departed",
    "arrested", "convicted", "appointed", "fired", "resigned",
    "mentioned", "about", "located_in", "part_of",
}


_PROMPT = """You extract relationship triples from news articles.

You will be given the article body and a list of entities already resolved in the article.
Each entity has a node_id and a surface name.

Return a JSON object: {{"triples": [{{"subject_id": "<node_id>", "verb": "<allowed_verb>", "object_id": "<node_id>", "evidence": "<short sentence>"}}]}}

Rules:
1. Only use node_ids from the provided entities list. Never invent.
2. Only use verbs from this list: {verbs}
3. Pick the single closest verb when the article uses a synonym.
4. evidence must be a direct quote or near-paraphrase ≤ 200 chars.
5. If two entities are mentioned but no concrete relationship is stated, DO NOT output a triple. Silence is fine.
6. Max 10 triples. Pick the strongest/most central ones.

Return ONLY the JSON. No prose."""


def extract_relationships(
    body: str,
    envelope: dict,
    *,
    llm: Optional[Any] = None,
    max_entities: int = 15,
) -> list[dict]:
    """Run the LLM relationship pass. Returns a list of custom-edge
    dicts ready to append to `envelope.relationships.custom`."""
    if not llm or not body or len(body) < 200:
        return []

    entities = envelope.get("entities") or []
    resolved = [
        e for e in entities
        if isinstance(e, dict)
        and isinstance(e.get("node_id"), str)
        and e["node_id"].startswith("entity:")
    ][:max_entities]
    if len(resolved) < 2:
        return []

    entity_lines = "\n".join(
        f"- {e.get('name_raw') or e.get('label') or e['node_id']}  [{e['node_id']}]"
        for e in resolved
    )
    verbs_str = ", ".join(sorted(ALLOWED_RELATIONSHIPS))

    prompt = (
        _PROMPT.format(verbs=verbs_str) + "\n\n"
        f"Entities in this article:\n{entity_lines}\n\n"
        f"Article body:\n{body[:3500]}"
    )

    try:
        result = llm.complete_json(
            prompt, max_tokens=800, temperature=0.0,
            prompt_version="relationships_v1",
            timeout=120.0,
        )
    except Exception as e:
        log.debug("relationship LLM call threw: %s", e)
        return []

    if not result.success or not isinstance(result.parsed, dict):
        return []

    raw = result.parsed.get("triples") or []
    if not isinstance(raw, list):
        return []

    known_ids = {e["node_id"] for e in resolved}
    out: list[dict] = []
    for t in raw[:10]:
        if not isinstance(t, dict):
            continue
        sid = t.get("subject_id")
        oid = t.get("object_id")
        verb = (t.get("verb") or "").strip().lower()
        if not sid or not oid or sid == oid:
            continue
        if sid not in known_ids or oid not in known_ids:
            continue
        if verb not in ALLOWED_RELATIONSHIPS:
            continue
        evidence = (t.get("evidence") or "").strip()[:500]
        out.append({
            "subject_id": sid,
            "target": oid,
            "relationship": verb,
            "strength": 0.8,
            "evidence": evidence,
        })
    return out


def attach_relationships_to_envelope(envelope: dict, triples: list[dict]) -> None:
    """Append relationship triples to envelope.relationships.custom so
    core.extraction.edges.upsert_edges_from_envelope picks them up. The
    source of each edge is the subject entity's node_id, NOT the
    current envelope's node_id — that's how relationships like
    (Petrov)-[met_with]-(Wei) get written even though the envelope
    represents an article, not Petrov."""
    if not triples:
        return
    rels = envelope.setdefault("relationships", {})
    custom = rels.setdefault("custom", [])
    for t in triples:
        # The edges writer uses envelope.node_id as the source by
        # default. For entity-to-entity edges we need to override by
        # re-shaping the edge so the writer still emits it. We add a
        # new "subject_id" field that the edges writer honors.
        custom.append({
            "subject_id": t["subject_id"],   # edges.py will prefer this over envelope.node_id
            "target": t["target"],
            "relationship": t["relationship"],
            "strength": t.get("strength", 0.7),
            "evidence": t.get("evidence", ""),
        })
