"""
Event extractor — finds first-class events referenced in the article
body and persists them to the `events` table.

Events are distinct from entities. An event is something that HAPPENED
at a specific time and place, involving entities as actors/targets.
Examples: strike, meeting, arrival, departure, broadcast, alert,
transaction, sighting, statement.

Runs in the article worker's deep pass (LLM required). Output fills
envelope.events_referenced[] AND inserts rows into the events table.
Each referenced event also gets an edge from the article node
(relationship='references') via the custom edges array.

Allowed event types come from nodevocab.event.subtypes:
    strike, meeting, arrival, departure, broadcast, publication,
    alert, sighting, transaction, statement, contract
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from core.extraction.auto_node import upsert_knowledge_node
from core.ldu.lineage import lineage_columns

log = logging.getLogger("enki.extraction.events")

ALLOWED_EVENT_TYPES = {
    "strike", "meeting", "arrival", "departure", "broadcast",
    "publication", "alert", "sighting", "transaction", "statement",
    "contract",
}


_PROMPT = """Extract discrete events described in this article.

An event is something that HAPPENED at a point in time involving specific actors/targets. Not every mentioned fact is an event — pick discrete, datable incidents.

For each event return: type, title, event_time (ISO-8601 when derivable, else null), place_text (best-effort), actor_entities (node_ids from the provided list), target_entities (node_ids from the provided list).

Allowed event types: {types}

Return JSON: {{"events": [ONE OR MORE event objects, each with keys: type, title, event_time, place_text (array), actor_entities (array of node_ids), target_entities (array of node_ids), confidence (0.0-1.0)]}}

Rules:
- Max 5 events per article. Pick the most central.
- Only use node_ids from the provided entities list.
- event_time: use article publish date as a fallback if event is contemporary with the article.
- If the article is a roundup with no discrete event, return an empty events array.
- confidence: 1.0 for events explicitly dated and sourced; 0.7 for datable-from-context; 0.5 for vague.
- Return ONLY the JSON.
"""


def extract_events(
    body: str,
    envelope: dict,
    *,
    llm: Optional[Any] = None,
    publish_date: Optional[str] = None,
) -> list[dict]:
    if not llm or not body or len(body) < 200:
        return []

    entities = [
        e for e in (envelope.get("entities") or [])
        if isinstance(e, dict) and isinstance(e.get("node_id"), str)
        and e["node_id"].startswith("entity:")
    ][:15]
    entity_lines = "\n".join(
        f"- {e.get('name_raw') or e.get('label')}  [{e['node_id']}]"
        for e in entities
    ) or "(no resolved entities — skip entity references)"

    types_str = ", ".join(sorted(ALLOWED_EVENT_TYPES))
    prompt = (
        _PROMPT.format(types=types_str)
        + f"\n\nArticle publish date (for temporal fallback): {publish_date or 'unknown'}"
        + f"\n\nEntities in this article:\n{entity_lines}\n\n"
        + f"Article body:\n{body[:3500]}"
    )

    try:
        result = llm.complete_json(
            prompt, max_tokens=900, temperature=0.0,
            prompt_version="events_v1", timeout=120.0,
        )
    except Exception as e:
        log.debug("events LLM call threw: %s", e)
        return []
    if not result.success or not isinstance(result.parsed, dict):
        return []

    raw = result.parsed.get("events") or []
    if not isinstance(raw, list):
        return []
    known_ids = {e["node_id"] for e in entities}

    out: list[dict] = []
    for ev in raw[:5]:
        if not isinstance(ev, dict):
            continue
        etype = (ev.get("type") or "").strip().lower()
        if etype not in ALLOWED_EVENT_TYPES:
            continue
        title = (ev.get("title") or "").strip()
        if not title:
            continue
        actors = [
            a for a in (ev.get("actor_entities") or [])
            if isinstance(a, str) and a in known_ids
        ]
        targets = [
            t for t in (ev.get("target_entities") or [])
            if isinstance(t, str) and t in known_ids
        ]
        out.append({
            "type": etype,
            "title": title[:500],
            "event_time": ev.get("event_time"),
            "place_text": [p for p in (ev.get("place_text") or []) if isinstance(p, str)][:5],
            "actor_node_ids": actors,
            "target_node_ids": targets,
            "confidence": float(ev.get("confidence", 0.6) or 0.6),
        })
    return out


def _node_id_for_event(title: str, etype: str, event_time: Optional[str]) -> str:
    blob = f"{etype}|{title.lower()}|{event_time or ''}"
    return "event:" + hashlib.sha1(blob.encode()).hexdigest()[:16]


def _parse_time(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _build_event_envelope(
    ev: dict,
    node_id: str,
    parent_article_node_id: str,
) -> dict:
    """Minimal standalone envelope so auto_node can materialize the event
    as a first-class knowledge_nodes card. Actors/targets are exposed on
    envelope.entities so master_entity_id gets picked up; the article that
    surfaced the event is recorded via relationships.derived_from."""
    all_actor_ids = (ev.get("actor_node_ids") or []) + (ev.get("target_node_ids") or [])
    seen: set[str] = set()
    entities = []
    for nid in all_actor_ids:
        if not (isinstance(nid, str) and nid.startswith("entity:")):
            continue
        if nid in seen:
            continue
        seen.add(nid)
        entities.append({"node_id": nid, "label": nid})
    return {
        "node_id": node_id,
        "node_type": "event",
        "subtype": ev["type"],
        "envelope_version": "v1",
        "identity": {"title_display": ev["title"][:500]},
        "summary": {"narrative": ev["title"][:2000]},
        "temporal": {"event_time": ev.get("event_time")},
        "spatial": {"place_text": ev.get("place_text") or []},
        "entities": entities,
        "evidence": {"epistemic_type": "observation"},
        "relationships": {
            "derived_from": [parent_article_node_id] if parent_article_node_id else [],
        },
        "completeness": {"score": float(ev.get("confidence") or 0.6), "tier": "bronze"},
    }


def persist_events(
    events: list[dict],
    envelope: dict,
    conn,
    *,
    tenant_id: str = "default",
    signal_rid: Optional[int] = None,
) -> list[str]:
    """Insert event rows and return their node_ids."""
    if conn is None or not events:
        return []
    lin = lineage_columns(ldu_t_id=signal_rid)
    out_ids: list[str] = []
    parent_node = envelope.get("node_id") or ""

    for ev in events:
        evt = _parse_time(ev.get("event_time"))
        node_id = _node_id_for_event(ev["title"], ev["type"], ev.get("event_time"))
        actors = [_entity_id_from_node(x) for x in ev["actor_node_ids"]]
        targets = [_entity_id_from_node(x) for x in ev["target_node_ids"]]
        actors = [a for a in actors if a is not None]
        targets = [t for t in targets if t is not None]

        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO events (
                        tenant_id, node_id, event_type, title, narrative,
                        event_time, place_text, actor_entity_ids, target_entity_ids,
                        source_signal_rids, source_count, confidence,
                        admitted_under_active_set, admitted_under_pack_versions,
                        gdu_canonical, ldu_t_id
                    ) VALUES (
                        %(tenant)s, %(node)s, %(etype)s, %(title)s, %(narr)s,
                        %(etime)s, %(place)s::text[], %(actors)s::bigint[], %(targets)s::bigint[],
                        %(signals)s::bigint[], 1, %(conf)s,
                        %(active_set)s, %(pack_versions)s::jsonb,
                        %(canonical)s, %(ldu_t)s
                    )
                    ON CONFLICT (tenant_id, node_id) DO UPDATE SET
                        source_signal_rids = (
                            SELECT ARRAY(SELECT DISTINCT unnest(
                                COALESCE(events.source_signal_rids, ARRAY[]::bigint[])
                                || COALESCE(EXCLUDED.source_signal_rids, ARRAY[]::bigint[])
                            ))
                        ),
                        source_count = (
                            SELECT cardinality(ARRAY(SELECT DISTINCT unnest(
                                COALESCE(events.source_signal_rids, ARRAY[]::bigint[])
                                || COALESCE(EXCLUDED.source_signal_rids, ARRAY[]::bigint[])
                            )))
                        ),
                        actor_entity_ids = (
                            SELECT ARRAY(SELECT DISTINCT unnest(
                                COALESCE(events.actor_entity_ids, ARRAY[]::bigint[])
                                || COALESCE(EXCLUDED.actor_entity_ids, ARRAY[]::bigint[])
                            ))
                        ),
                        target_entity_ids = (
                            SELECT ARRAY(SELECT DISTINCT unnest(
                                COALESCE(events.target_entity_ids, ARRAY[]::bigint[])
                                || COALESCE(EXCLUDED.target_entity_ids, ARRAY[]::bigint[])
                            ))
                        ),
                        confidence = GREATEST(events.confidence, EXCLUDED.confidence),
                        updated_at = NOW()
                    """,
                    {
                        "tenant": tenant_id, "node": node_id,
                        "etype": ev["type"], "title": ev["title"][:500],
                        "narr": ev.get("title", "")[:2000],
                        "etime": evt,
                        "place": ev["place_text"] or [],
                        "actors": actors,
                        "targets": targets,
                        "signals": [signal_rid] if signal_rid else [],
                        "conf": ev["confidence"],
                        **lin,
                    },
                )
        except Exception as e:
            log.debug("event insert for %s failed: %s", ev["title"], e)
            try: conn.rollback()
            except Exception: pass
            continue

        # Commit the events-table INSERT now so the knowledge_node upsert
        # runs in its own transaction. Without this, an upsert rollback
        # would also wipe the events row we just inserted.
        try:
            conn.commit()
        except Exception:
            pass

        out_ids.append(node_id)

        # Materialize the event as a first-class knowledge_nodes card so
        # the graph's article→event edges point at a real node and the UI
        # can render the event as its own card. Best-effort; failures here
        # never break the article's extraction.
        try:
            event_env = _build_event_envelope(ev, node_id, parent_node)
            upsert_knowledge_node(
                event_env, conn,
                tenant_id=tenant_id,
                signal_rid=signal_rid,
            )
        except Exception as e:
            log.debug("event knowledge_node upsert for %s failed: %s", node_id, e)

        # Record edges: actor entities → references event, article → references event
        rels = envelope.setdefault("relationships", {})
        custom = rels.setdefault("custom", [])
        for a in ev["actor_node_ids"]:
            custom.append({"subject_id": a, "target": node_id,
                           "relationship": "participated_in",
                           "strength": 0.85, "evidence": ev["title"][:300]})
        for t in ev["target_node_ids"]:
            custom.append({"subject_id": t, "target": node_id,
                           "relationship": "affected_by",
                           "strength": 0.8, "evidence": ev["title"][:300]})
        if parent_node:
            custom.append({"target": node_id, "relationship": "mentions",
                           "strength": 0.85, "evidence": ev["title"][:300]})

    # Update envelope.events_referenced
    if out_ids:
        envelope["events_referenced"] = list(set(
            (envelope.get("events_referenced") or []) + out_ids
        ))
    try:
        conn.commit()
    except Exception:
        pass
    return out_ids


def _entity_id_from_node(node_id: str) -> Optional[int]:
    if not isinstance(node_id, str) or not node_id.startswith("entity:"):
        return None
    try:
        return int(node_id.split(":", 1)[1])
    except (ValueError, IndexError):
        return None
