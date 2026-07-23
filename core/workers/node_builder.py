"""
Node Builder Worker — turns LLM enrichments into knowledge nodes and graph edges.

Reads signal_enrichments (append-only) and:
  1. Resolves each extracted entity against existing entities (5-layer dedup)
  2. Creates new entities or links to existing ones
  3. Creates knowledge_nodes (the cards you click)
  4. Creates entity_relationships (the graph edges)
  5. Appends entity_events (immutable audit chain)

ALL entity creation/updates go through the entity resolver for dedup.
ALL changes recorded in entity_events (append-only, hash-chained).
ALL entity types validated against nodevocab GDU pack.

Runs as a daemon thread, checking for unprocessed enrichments every 10 seconds.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.node_builder")

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "enrichments_processed": 0,
    "entities_created": 0,
    "entities_linked": 0,
    "relationships_created": 0,
    "nodes_created": 0,
    "errors": 0,
    "batch_num": 0,
}

CHECK_INTERVAL = 10
BATCH_SIZE = 10  # enrichments per batch (these are fast, no LLM)

# ─── Tier 0: Instant Rejection Rules ─────────────────────────────────────
# These run on every extracted name BEFORE any DB lookup. Fast, no I/O.

_REJECT_EXACT = {
    # Article boilerplate
    "breaking", "update", "opinion", "exclusive", "analysis", "report",
    "breaking news", "live updates", "developing story", "#breaking",
    "watch", "video", "photo", "gallery", "podcast",
    # Generic terms
    "the government", "the military", "the police", "officials",
    "sources", "experts", "researchers", "scientists", "analysts",
    "the company", "the organization", "the country",
    # Media outlets often misclassified as people
    "reuters", "associated press", "ap", "afp", "bbc", "cnn",
    "fox news", "al jazeera", "the guardian", "new york times",
    "washington post", "daily mail", "daily express",
}

_REJECT_PREFIXES = ("article ", "story ", "report ", "update ", "photo ")
_REJECT_SUFFIXES = ("'s", " says", " said", " report", " news")

# Demonyms — adjective forms that aren't entities
_DEMONYMS = {
    "egyptian", "iranian", "iraqi", "american", "british", "french",
    "german", "russian", "chinese", "japanese", "korean", "indian",
    "israeli", "palestinian", "syrian", "turkish", "saudi", "canadian",
    "australian", "brazilian", "mexican", "colombian", "venezuelan",
    "afghan", "pakistani", "lebanese", "yemeni", "libyan", "nigerian",
    "italian", "spanish", "polish", "swedish", "dutch", "swiss",
    "ukrainian", "somali", "sudanese", "ethiopian", "kenyan", "moroccan",
}


def _should_reject(name: str) -> bool:
    """Tier 0 instant rejection — returns True if name should NOT become a node."""
    low = name.lower().strip()

    # Too short
    if len(low) < 3:
        return True

    # Exact match reject list
    if low in _REJECT_EXACT:
        return True

    # Demonyms (adjective forms of countries)
    if low in _DEMONYMS:
        return True

    # Boilerplate prefixes/suffixes
    if any(low.startswith(p) for p in _REJECT_PREFIXES):
        return True
    if any(low.endswith(s) for s in _REJECT_SUFFIXES):
        return True

    # ALL-CAPS short strings that aren't known acronyms
    if name.isupper() and len(name) <= 3 and low not in (
        "us", "uk", "eu", "un", "who", "fbi", "cia", "nsa", "fda",
        "sec", "fed", "imf", "epa", "cdc", "doj", "dod", "idf",
        "iaea", "opec", "nato", "isis", "gru", "fsb", "cia",
    ):
        return True

    # Looks like a sentence, not an entity name (>6 words)
    if len(low.split()) > 6:
        return True

    return False


# ─── GDU Gateway Integration ────────────────────────────────────────────
# GDU gateway is ALWAYS enabled. All node/entity/edge creation goes through
# the gateway (the final collapse point). The gateway validates against
# vocabulary packs, assigns base-60 addresses, stamps lineage, and
# hash-chains every mutation.

def _use_gateway() -> bool:
    """GDU gateway is always enabled. Kept for backward compat."""
    return True


# Canonical entity-type validity is owned by the entityvocab pack via
# subtype_validator (the single source of truth). node_builder no longer
# keeps its own list — it asks validate_for_ldu() so it can never drift from
# the gateway / storage constraint.

TYPE_MAP = {
    "person": "person",
    "organization": "organization",
    "location": None,
    "company": "organization",
    "government": "organization",
    "military": "organization",
    "media": "organization",
    "ngo": "organization",
    "asset": "product",
    "weapon": "weapon_system",
    "vehicle": "vehicle",
    "vessel": "vessel",
    "aircraft": "aircraft",
    "facility": "facility",
}

VALID_RELATIONSHIPS = {
    "meets_with", "opposes", "supports", "attacks", "sanctions",
    "leads", "member_of", "allied_with", "investigates", "funds",
    "reports_on", "owns", "employs", "partners_with", "competes_with",
    "subsidiary_of", "targets", "arrested", "regulates",
}


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_worker_loop, args=(tenant_id,),
        daemon=True, name="node_builder",
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    _STATUS["phase"] = "starting"
    return {"status": "started"}


def stop_worker():
    _STOP_EVENT.set()
    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"
    return {"status": "stopped"}


def _worker_loop(tenant_id: str):
    _STATUS["running"] = True
    backoff = CHECK_INTERVAL

    while not _STOP_EVENT.is_set():
        try:
            processed = _process_batch(tenant_id)
            if processed > 0:
                backoff = CHECK_INTERVAL
                _STATUS["phase"] = "processing"
            else:
                backoff = min(backoff + 5, 60)
                _STATUS["phase"] = "idle — caught up"
        except Exception as e:
            log.warning("Node builder error: %s", e)
            _STATUS["errors"] += 1
            _STATUS["phase"] = f"error: {str(e)[:60]}"
            backoff = min(backoff * 2, 120)

        for _ in range(int(backoff)):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)

    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"


def _process_batch(tenant_id: str) -> int:
    """Find unprocessed enrichments and create nodes from them."""

    enrichments = _find_unprocessed_enrichments(tenant_id, BATCH_SIZE)
    if not enrichments:
        return 0

    _STATUS["batch_num"] += 1
    processed = 0

    for enr in enrichments:
        try:
            _process_enrichment(enr, tenant_id)
            _mark_enrichment_processed(enr["enrichment_id"])
            processed += 1
            _STATUS["enrichments_processed"] += 1
        except Exception as e:
            log.warning("Failed to process enrichment %d: %s", enr["enrichment_id"], e)
            _STATUS["errors"] += 1

    _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
    log.info("Node builder batch %d: processed %d enrichments", _STATUS["batch_num"], processed)
    return processed


def _find_unprocessed_enrichments(tenant_id: str, limit: int) -> list:
    """Find signal_enrichments that haven't been turned into nodes yet.

    Uses a processed flag — we track which enrichment_ids have been handled
    by checking entity_events for a 'node_builder_processed' marker.
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT se.enrichment_id, se.signal_rid, se.tenant_id,
                           se.entities, se.relationships, se.event_type,
                           se.importance, se.summary, se.method
                    FROM signal_enrichments se
                    WHERE se.tenant_id = %s
                      AND se.tier >= 2
                      AND se.method = 'llm'
                      AND NOT EXISTS (
                          SELECT 1 FROM entity_events ee
                          WHERE ee.signal_rid = se.signal_rid
                            AND ee.source = 'node_builder'
                            AND ee.event_type = 'enrichment_processed'
                      )
                    ORDER BY se.extracted_at ASC
                    LIMIT %s
                """, (tenant_id, limit))

                results = []
                for r in cur.fetchall():
                    results.append({
                        "enrichment_id": r[0],
                        "signal_rid": r[1],
                        "tenant_id": r[2],
                        "entities": r[3] or [],
                        "relationships": r[4] or [],
                        "event_type": r[5],
                        "importance": r[6],
                        "summary": r[7],
                        "method": r[8],
                    })
                return results
    except Exception as e:
        log.debug("Failed to find enrichments: %s", e)
        return []


def _process_enrichment(enr: dict, tenant_id: str):
    """Process one enrichment: create entities, places, event, and graph edges.

    Dispatches to GDU gateway path or legacy direct-SQL path based on
    ENKI_USE_GDU_GATEWAY environment variable.
    """
    if _use_gateway():
        return _process_enrichment_gdu(enr, tenant_id)
    return _process_enrichment_legacy(enr, tenant_id)


def _process_enrichment_gdu(enr: dict, tenant_id: str):
    """GDU Gateway path — all writes go through the collapse point.

    The LLM extracted freely into LDU-W. The mapper translates to GDU
    addresses. The gateway validates, collapses, assigns base-60 addresses,
    and hash-chains every mutation. Facts and claims become Assessment nodes.
    """
    from core.gdu.gateway import get_gateway, AdmissionResult
    from core.gdu.llm_mapper import get_mapper

    gateway = get_gateway()
    mapper = get_mapper()

    entities = enr.get("entities", [])
    relationships = enr.get("relationships", [])
    signal_rid = enr["signal_rid"]

    # Track admitted nodes for edge creation
    admitted: dict[str, AdmissionResult] = {}   # name → result
    entity_map: dict[str, int] = {}             # name → entity_id

    # ── Process each extracted entity through mapper → gateway ──────
    for ext_entity in entities:
        name = (ext_entity.get("name") or "").strip()
        if not name or len(name) < 2:
            continue

        # Tier 0 rejection still applies (boilerplate, demonyms, etc.)
        if _should_reject(name):
            continue

        # Map LLM output to GDU coordinates
        mapping = mapper.map_entity(ext_entity)

        # Metadata-only (quotes, quantities) — skip node creation
        if mapping.is_metadata:
            continue

        # Unmappable → LDU-P (provisional)
        if mapping.routed_to_ldup:
            gateway.route_to_ldup(
                tenant_id, ext_entity, mapping.input_type,
                mapping.ldup_reason, signal_rid,
            )
            continue

        # Skip media/source entities (they report, they're not the story)
        role = ext_entity.get("role_in_article", "")
        if role == "source" and mapping.input_type in ("media",):
            continue

        # Route through gateway based on category
        result = None
        if mapping.category in ("entity", "place"):
            # Capture-time admission goes to LDU (Phase 6, May 20 rebuild).
            # The candidate sits pending until promotion criteria are met —
            # signal accumulation + type_evidence from gazetteer / Wikidata /
            # LLM arbitration. When the surface form already resolves to a
            # canonical entity, admit_candidate returns
            # action='attached_to_gdu_entity' with node_id + base60_address
            # populated, so the downstream mention/edge code below keeps
            # working unchanged.
            from core.ldu.admit_candidate import admit_candidate
            identifiers = ext_entity.get("identifiers", {}) or {}
            type_evidence = {
                k: v for k, v in identifiers.items()
                if k in ("wikidata_id", "wikidata_qid", "ein", "cik",
                         "duns", "lei", "mmsi", "imo", "icao24",
                         "registration", "tail_number", "ticker")
                and v
            } or None
            result = admit_candidate(
                surface_form=name,
                proposed_type=mapping.subtype,
                proposed_subtype=mapping.subtype,
                signal_rid=signal_rid,
                tenant_id=tenant_id,
                type_evidence=type_evidence,
            )
        elif mapping.category == "event":
            # Events collapse by normalized title
            collapse_key = name.lower().strip() if len(name) >= 10 else None
            if collapse_key:
                result = gateway.admit_node(
                    tenant_id, "event", mapping.subtype,
                    name, name, signal_rid,
                    collapse_key=collapse_key,
                )
        elif mapping.category == "assessment":
            # FACTS AND CLAIMS — title is the FULL fact/claim text, not the
            # extracted value. Otherwise we end up with assessments titled
            # "27 years" or "20%" which create spurious cross-source matches.
            fact_text = ext_entity.get("fact", ext_entity.get("claim", name))
            value = ext_entity.get("value", "")

            # Skip assessments where the fact text is too thin to be meaningful.
            # A real claim is "Iran enriched to 90%", not "90%".
            if not fact_text or len(fact_text) < 15:
                continue

            # Reject if it's just a pure number/duration with no context
            import re as _re
            if _re.match(r'^\s*[\d.,%$]+\s*\w{0,12}\s*$', fact_text):
                continue

            # Reject overly generic claims with no proper nouns / specific entities.
            # "State involved in redistricting efforts" has no specific entity → reject.
            # A real claim names something specific.
            proper_nouns = _re.findall(r'\b[A-Z][a-z]+\b', fact_text)
            numbers = _re.findall(r'\b\d+\b', fact_text)
            if len(proper_nouns) < 1 and len(numbers) < 1:
                continue

            # Title = the full claim. Description carries extracted value.
            assessment_title = fact_text[:300]
            assessment_desc = (
                f"{fact_text}"
                + (f" (value: {value})" if value and value not in fact_text else "")
            )

            # Canonical collapse key — normalize so minor word variations
            # ("State involved in redistricting" vs "State involved in
            # redistricting efforts") produce the same key and collapse.
            _stopwords = {"the", "a", "an", "is", "are", "was", "were", "be",
                          "been", "being", "have", "has", "had", "do", "does",
                          "did", "will", "would", "should", "could", "may",
                          "might", "can", "of", "in", "for", "on", "with",
                          "at", "by", "from", "as", "into", "through", "and",
                          "or", "but", "if", "while", "that", "this", "these",
                          "those", "it", "its", "to", "involved", "efforts"}
            tokens = [t for t in _re.findall(r'\b\w{3,}\b', fact_text.lower())
                      if t not in _stopwords]
            # Sort tokens so word order doesn't change the key; take top distinctive
            tokens.sort()
            collapse_key = " ".join(tokens[:15])
            if not collapse_key or len(collapse_key) < 12:
                # Not enough distinctive content — use full text fallback
                collapse_key = fact_text.lower().strip()[:200]

            result = gateway.admit_node(
                tenant_id, "assessment", mapping.subtype,
                assessment_title, assessment_desc, signal_rid,
                collapse_key=collapse_key,
                metadata={
                    "source": ext_entity.get("source", ""),
                    "confidence": ext_entity.get("confidence", ""),
                    "kind": ext_entity.get("kind", ""),
                    "value": value,
                },
            )
        elif mapping.category == "topic":
            collapse_key = name.lower().strip()
            result = gateway.admit_node(
                tenant_id, "topic", mapping.subtype or "technology",
                name, f"Topic: {name}", signal_rid,
                collapse_key=collapse_key,
            )

        if result and result.node_id:
            admitted[name] = result
            if result.entity_id:
                entity_map[name] = result.entity_id

            # Append entity_event for audit trail
            if result.entity_id:
                with get_connection() as conn:
                    _append_entity_event(conn, result.entity_id, tenant_id, "linked", {
                        "signal_rid": signal_rid,
                        "role": ext_entity.get("role_in_article", ""),
                        "significance": ext_entity.get("significance", ""),
                        "base60_address": result.base60_address,
                        "gdu_action": result.action,
                    }, signal_rid)
                    conn.commit()

            _STATUS["entities_created" if result.is_new else "entities_linked"] += 1
            if result.is_new:
                _STATUS["nodes_created"] += 1

    # ── Article event node ──────────────────────────────────────────
    event_type = enr.get("event_type")
    summary = (enr.get("summary", "") or "").strip()
    if event_type and len(summary) >= 20:
        event_subtype = mapper.map_event_type(event_type) or "alert"
        collapse_key = summary.lower()[:150]
        result = gateway.admit_node(
            tenant_id, "event", event_subtype,
            summary[:150], summary, signal_rid,
            collapse_key=collapse_key,
        )
        if result and result.node_id:
            admitted[f"_event_{signal_rid}"] = result

    # ── Topic nodes from keywords ───────────────────────────────────
    keywords = enr.get("keywords") or []
    if keywords and isinstance(keywords, list):
        for kw in keywords[:3]:
            if isinstance(kw, str) and len(kw) >= 3:
                collapse_key = kw.lower().strip()
                result = gateway.admit_node(
                    tenant_id, "topic", "technology",
                    kw, f"Topic: {kw}", signal_rid,
                    collapse_key=collapse_key,
                )
                if result and result.node_id:
                    admitted[kw] = result

    # ── Document node for the source article ────────────────────────
    doc_result = None
    if admitted:
        headline = (enr.get("summary", "") or "")[:200] or f"Article #{signal_rid}"
        doc_result = gateway.admit_node(
            tenant_id, "document", "article",
            headline, (enr.get("summary", "") or "")[:500], signal_rid,
        )

    # ── Structural edges ────────────────────────────────────────────
    if doc_result and doc_result.node_id:
        for name, result in admitted.items():
            if not result.node_id:
                continue

            # mentions: document → extracted node
            gateway.admit_edge(
                tenant_id, doc_result.node_id, result.node_id,
                "mentions", name, signal_rid,
            )

            # derived_from: extracted node → document (audit lineage)
            gateway.admit_edge(
                tenant_id, result.node_id, doc_result.node_id,
                "derived_from", name, signal_rid,
            )

    # ── Co-occurrence edges (entities in same article) ──────────────
    entity_nodes = [
        (n, r) for n, r in admitted.items()
        if r.category in ("entity", "place") and r.node_id
    ]
    for i, (name_a, res_a) in enumerate(entity_nodes):
        for name_b, res_b in entity_nodes[i + 1:]:
            gateway.admit_edge(
                tenant_id, res_a.node_id, res_b.node_id,
                "co_occurs_with", f"{name_a} + {name_b}", signal_rid,
            )

    # ── Located_at edges (entities → places from same signal) ───────
    place_nodes = [(n, r) for n, r in admitted.items() if r.category == "place" and r.node_id]
    pure_entity_nodes = [(n, r) for n, r in admitted.items() if r.category == "entity" and r.node_id]
    for ent_name, ent_res in pure_entity_nodes:
        for place_name, place_res in place_nodes:
            gateway.admit_edge(
                tenant_id, ent_res.node_id, place_res.node_id,
                "located_at", f"{ent_name} at {place_name}", signal_rid,
            )

    # ── Entity relationships from LLM ───────────────────────────────
    for rel in relationships:
        mapped_edges = mapper.map_relationship(rel)
        for edge in mapped_edges:
            if edge.is_entity_relationship:
                # Entity-to-entity semantic relationship
                source_id = entity_map.get(edge.source_name)
                target_id = entity_map.get(edge.target_name)
                if source_id and target_id and source_id != target_id:
                    _create_relationship_legacy(
                        tenant_id, source_id, target_id,
                        edge.relationship, rel.get("temporal", "current"),
                        signal_rid, enr.get("summary", ""),
                    )
            else:
                # Structural edge via gateway
                source_result = admitted.get(edge.source_name)
                target_result = admitted.get(edge.target_name)
                if source_result and target_result and source_result.node_id and target_result.node_id:
                    gateway.admit_edge(
                        tenant_id, source_result.node_id, target_result.node_id,
                        edge.relationship, f"{edge.source_name} → {edge.target_name}",
                        signal_rid,
                    )

    _STATUS["relationships_found"] = _STATUS.get("relationships_found", 0) + len(relationships)


def _create_relationship_legacy(
    tenant_id: str, source_id: int, target_id: int,
    rel_type: str, temporal: str, signal_rid: int, evidence: str,
):
    """Create entity_relationship (semantic, entity-to-entity). Used by both paths."""
    is_current = temporal != "historical"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT rid, article_count FROM entity_relationships
                    WHERE tenant_id = %s AND source_entity_id = %s
                      AND target_entity_id = %s AND relationship = %s
                    LIMIT 1
                """, (tenant_id, source_id, target_id, rel_type))
                row = cur.fetchone()

                if row:
                    cur.execute("""
                        UPDATE entity_relationships
                        SET article_count = COALESCE(article_count, 0) + 1,
                            is_current = %s, updated_at = NOW()
                        WHERE rid = %s
                    """, (is_current, row[0]))
                else:
                    cur.execute("""
                        INSERT INTO entity_relationships
                            (tenant_id, source_entity_id, target_entity_id,
                             relationship, confidence, source, evidence,
                             is_current, article_count, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, 0.7, 'llm', %s, %s, 1, NOW(), NOW())
                    """, (tenant_id, source_id, target_id,
                          rel_type, evidence[:500], is_current))
            conn.commit()
    except Exception as e:
        log.debug("Relationship insert failed: %s", e)


def _process_enrichment_legacy(enr: dict, tenant_id: str):
    """Legacy direct-SQL path (used when ENKI_USE_GDU_GATEWAY is off)."""

    entities = enr.get("entities", [])
    relationships = enr.get("relationships", [])
    signal_rid = enr["signal_rid"]

    # Map of extracted name → entity_id (for relationship creation)
    entity_map: dict[str, int] = {}
    # All node_ids created/linked in this enrichment (for mentions edges)
    all_node_ids: dict[str, str] = {}  # name → node_id

    with get_connection() as conn:
        for ext_entity in entities:
            name = ext_entity.get("name", "").strip()
            if not name or len(name) < 2:
                continue

            # ── Tier 0: Instant rejection rules ─────────────────────
            if _should_reject(name):
                continue

            ext_type = ext_entity.get("type", "").lower()
            ext_subtype = ext_entity.get("subtype", "").lower() if ext_entity.get("subtype") else None

            # ── Locations → entity nodes with location subtype ────────
            # Everything is an entity. Locations get subtype=country/city/etc.
            if ext_type == "location":
                place_subtype = ext_subtype or _infer_place_subtype(name, ext_entity)
                # Normalize subtypes
                if place_subtype in ("state", "province", "district", "area"):
                    place_subtype = "region"
                elif place_subtype in ("town", "village"):
                    place_subtype = "city"
                # Resolve through the normal entity path (name-first dedup)
                entity_id = _resolve_entity(conn, tenant_id, place_subtype or "country", name, signal_rid)
                if entity_id:
                    entity_map[name] = entity_id
                    all_node_ids[name] = f"entity:{entity_id}"
                    _ensure_knowledge_node(conn, entity_id, place_subtype or "country", name, tenant_id, enr)
                continue

            # ── Skip non-node types (quotes, quantities — stored for reference) ──
            if ext_type in ("quote", "quantity"):
                continue

            # ── Inline events → event nodes ──────────────────────────
            if ext_type == "event":
                event_subtype = ext_subtype or ext_entity.get("subtype", "")
                if len(name) >= 10:  # need a real event title
                    ev_node_id = f"event:{hashlib.sha1(name.lower().encode()).hexdigest()[:12]}"
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO knowledge_nodes
                                (node_id, project_id, tenant_id, title, description,
                                 node_type, subtype, signal_rid,
                                 confidence, source_count,
                                 enrichment_status, gdu_canonical,
                                 created_at, updated_at)
                            VALUES (%s, 'default', %s, %s, %s,
                                    'event', %s, %s,
                                    0.6, 1, 'complete', TRUE, NOW(), NOW())
                            ON CONFLICT (node_id) DO UPDATE SET
                                source_count = knowledge_nodes.source_count + 1,
                                updated_at = NOW()
                        """, (ev_node_id, tenant_id, name, name,
                              event_subtype, signal_rid))
                    all_node_ids[name] = ev_node_id
                continue

            entity_type = TYPE_MAP.get(ext_type)

            # Skip if not a valid entity type — validity owned by the SSOT
            # (entityvocab pack via subtype_validator), not a local list.
            from core.ldu.subtype_validator import validate_for_ldu
            if not entity_type or validate_for_ldu(entity_type).action != "accept":
                continue

            # Skip media/source entities (they report, they're not the story)
            role = ext_entity.get("role_in_article", "")
            if role == "source" and ext_type in ("media", "organization"):
                subtype = ext_entity.get("subtype", "")
                if subtype == "media":
                    continue

            # Resolve entity (dedup against existing)
            entity_id = _resolve_entity(
                conn, tenant_id, entity_type, name, signal_rid
            )

            if entity_id:
                entity_map[name] = entity_id
                all_node_ids[name] = f"entity:{entity_id}"

                # Append entity_event: linked to signal
                _append_entity_event(conn, entity_id, tenant_id, "linked", {
                    "signal_rid": signal_rid,
                    "role": ext_entity.get("role_in_article", ""),
                    "significance": ext_entity.get("significance", ""),
                    "title": ext_entity.get("title", ""),
                    "organization": ext_entity.get("organization", ""),
                    "summary_context": enr.get("summary", "")[:200],
                }, signal_rid)

                # Create knowledge_node if this is a new entity
                _ensure_knowledge_node(conn, entity_id, entity_type, name, tenant_id, enr)

        # ── Create event node for this article's event ───────────────
        event_type = enr.get("event_type")
        if event_type:
            event_node_id = _ensure_event_node(conn, tenant_id, signal_rid, enr)
            if event_node_id:
                all_node_ids[f"_event_{signal_rid}"] = event_node_id

        # ── Create topic node for the article's primary keyword ──────
        keywords = enr.get("keywords") or []
        if keywords and isinstance(keywords, list):
            for kw in keywords[:3]:  # top 3 keywords as topic nodes
                if isinstance(kw, str) and len(kw) >= 3:
                    topic_node_id = _ensure_topic_node(conn, tenant_id, kw, signal_rid)
                    if topic_node_id:
                        all_node_ids[kw] = topic_node_id

        # ── Create document node + mentions edges to ALL nodes ───────
        if all_node_ids:
            _ensure_document_node_and_mentions(conn, tenant_id, signal_rid, enr, all_node_ids)

        # ── Process relationships between entities ───────────────────
        for rel in relationships:
            source_name = rel.get("source", "").strip()
            target_name = rel.get("target", "").strip()
            rel_type = rel.get("type", "").strip().lower()

            if not source_name or not target_name or not rel_type:
                continue

            source_id = entity_map.get(source_name)
            target_id = entity_map.get(target_name)

            if source_id and target_id and source_id != target_id:
                _create_relationship(
                    conn, tenant_id, source_id, target_id,
                    rel_type, rel.get("temporal", "current"),
                    signal_rid, enr.get("summary", "")
                )

        conn.commit()


def _resolve_entity(conn, tenant_id: str, entity_type: str, name: str,
                     signal_rid: int) -> Optional[int]:
    """Resolve an entity name against existing entities. Create if new.

    NAME-FIRST resolution — matches by normalized name regardless of type.
    This prevents "Egypt" from being created as both a country entity and
    an organization entity just because the LLM classified it differently
    in two articles.

    Layers:
      1. Exact normalized name match (ANY type)
      2. Alias match
      3. Demonym resolution (Egyptian → Egypt)
      4. Create new
    """
    normalized = name.lower().strip()

    # Strip demonyms: "Egyptian" → "egypt", "Iranian" → "iran"
    _DEMONYM_MAP = {
        "egyptian": "egypt", "iranian": "iran", "iraqi": "iraq",
        "american": "united states", "british": "united kingdom",
        "russian": "russia", "chinese": "china", "israeli": "israel",
        "palestinian": "palestine", "syrian": "syria", "turkish": "turkey",
        "saudi": "saudi arabia", "french": "france", "german": "germany",
        "japanese": "japan", "korean": "south korea", "indian": "india",
        "ukrainian": "ukraine", "afghan": "afghanistan", "pakistani": "pakistan",
    }
    demonym_resolved = _DEMONYM_MAP.get(normalized)

    with conn.cursor() as cur:
        # Layer 1: exact name match — ANY type (name-first resolution)
        cur.execute("""
            SELECT entity_id, signal_count, canonical_type FROM entities
            WHERE tenant_id = %s AND normalized_name = %s
            LIMIT 1
        """, (tenant_id, normalized))
        row = cur.fetchone()

        # Layer 1b: demonym fallback (e.g., "Egyptian" → find "Egypt")
        if not row and demonym_resolved:
            cur.execute("""
                SELECT entity_id, signal_count, canonical_type FROM entities
                WHERE tenant_id = %s AND normalized_name = %s
                LIMIT 1
            """, (tenant_id, demonym_resolved))
            row = cur.fetchone()

        if row:
            entity_id = row[0]
            new_signal_count = (row[1] or 0) + 1
            existing_type = row[2] or entity_type
            # Update signal count and last_seen
            cur.execute("""
                UPDATE entities SET signal_count = signal_count + 1,
                       last_seen = NOW(), updated_at = NOW()
                WHERE entity_id = %s
            """, (entity_id,))
            _STATUS["entities_linked"] += 1

            # Auto-trigger investigation at threshold
            _maybe_queue_investigation(
                entity_id, tenant_id, existing_type, name, new_signal_count,
            )

            return entity_id

        # Layer 2: alias match
        cur.execute("""
            SELECT entity_id, signal_count FROM entities
            WHERE tenant_id = %s AND canonical_type = %s AND %s = ANY(aliases)
            LIMIT 1
        """, (tenant_id, entity_type, normalized))
        row = cur.fetchone()

        if row:
            entity_id = row[0]
            cur.execute("""
                UPDATE entities SET signal_count = signal_count + 1,
                       last_seen = NOW(), updated_at = NOW()
                WHERE entity_id = %s
            """, (entity_id,))
            _STATUS["entities_linked"] += 1
            return entity_id

        # Layer 3: route to LDU. Layers 1+2 above already handled the
        # cases where this surface form maps to a known canonical entity.
        # Anything reaching here is a candidate — park it in LDU until
        # promotion criteria are met. Returns None so the caller skips
        # mention/edge work; the LDU promoter will replay these signal
        # rids into mentions once the candidate is promoted to GDU.
        from core.ldu.admit_candidate import admit_candidate

        result = admit_candidate(
            surface_form=name,
            proposed_type=entity_type or "unknown",
            signal_rid=signal_rid,
            tenant_id=tenant_id,
        )

        if result.action == "attached_to_gdu_entity" and result.entity_id:
            _STATUS["entities_linked"] += 1
            return result.entity_id

        # created_candidate / merged_with_candidate / rejected — no
        # canonical entity_id yet.
        return None


def _ensure_knowledge_node(conn, entity_id: int, entity_type: str,
                            name: str, tenant_id: str, enr: dict):
    """Create a knowledge_node for this entity if one doesn't exist."""

    node_id = f"entity:{entity_id}"

    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM knowledge_nodes WHERE node_id = %s", (node_id,))
        if cur.fetchone():
            # Node exists — update source_count
            cur.execute("""
                UPDATE knowledge_nodes
                SET source_count = source_count + 1,
                    updated_at = NOW()
                WHERE node_id = %s
            """, (node_id,))
            return

        # Create new knowledge node
        cur.execute("""
            INSERT INTO knowledge_nodes
                (node_id, project_id, tenant_id, title, description,
                 node_type, subtype, entity_type, entity_name,
                 master_entity_id, confidence, source_count,
                 enrichment_status, gdu_canonical,
                 created_at, updated_at)
            VALUES (%s, 'default', %s, %s, %s,
                    'entity', %s, %s, %s,
                    %s, 0.5, 1,
                    'pending', TRUE,
                    NOW(), NOW())
            ON CONFLICT (node_id) DO NOTHING
        """, (
            node_id, tenant_id, name,
            enr.get("summary", "")[:500],
            entity_type, entity_type, name,
            entity_id,
        ))

        _STATUS["nodes_created"] += 1
        log.info("Created knowledge node: %s → %s (%s)", node_id, name, entity_type)


def _infer_place_subtype(name: str, ext: dict) -> str:
    """Guess a place subtype from the LLM extraction context."""
    relevance = (ext.get("relevance", "") or "").lower()
    name_lower = name.lower()
    # Country-level heuristics
    if len(name) == 2 and name.isupper():
        return "country"  # ISO code like "US", "RU"
    if any(w in name_lower for w in ["republic", "kingdom", "states of"]):
        return "country"
    if any(w in relevance for w in ["country", "nation", "state"]):
        return "country"
    # City heuristics
    if any(w in relevance for w in ["city", "capital", "town", "municipality"]):
        return "city"
    # Region
    if any(w in relevance for w in ["region", "province", "state", "district"]):
        return "region"
    # Water features
    if any(w in name_lower for w in ["strait", "gulf", "bay", "sea", "ocean", "lake", "river"]):
        return name_lower.split()[-1] if name_lower.split()[-1] in (
            "strait", "gulf", "bay", "sea", "ocean", "lake", "river") else "place"
    return "country"  # default — most LLM-extracted locations are countries


def _ensure_place_node(conn, tenant_id: str, name: str, subtype: str,
                       signal_rid: int, enr: dict) -> Optional[str]:
    """Create a place knowledge_node if it doesn't exist. Returns node_id.

    Checks existing places AND entities by normalized name first to prevent
    duplicates like Egypt appearing as both entity:2888 and place:abc123.
    """
    normalized = name.lower().strip()

    with conn.cursor() as cur:
        # Check if this place already exists as an ENTITY node (cross-check)
        cur.execute("""
            SELECT node_id FROM knowledge_nodes
            WHERE LOWER(title) = %s AND tenant_id = %s
              AND node_type IN ('entity', 'place')
            ORDER BY source_count DESC LIMIT 1
        """, (normalized, tenant_id))
        existing = cur.fetchone()
        if existing:
            cur.execute("""
                UPDATE knowledge_nodes SET source_count = source_count + 1, updated_at = NOW()
                WHERE node_id = %s
            """, (existing[0],))
            return existing[0]

        # No existing match — create new place node
        node_id = f"place:{hashlib.sha1(f'{subtype}|{normalized}'.encode()).hexdigest()[:12]}"

        cur.execute("SELECT node_id FROM knowledge_nodes WHERE node_id = %s", (node_id,))
        if cur.fetchone():
            cur.execute("""
                UPDATE knowledge_nodes SET source_count = source_count + 1, updated_at = NOW()
                WHERE node_id = %s
            """, (node_id,))
            return node_id

        cur.execute("""
            INSERT INTO knowledge_nodes
                (node_id, project_id, tenant_id, title, description,
                 node_type, subtype, signal_rid,
                 confidence, source_count,
                 enrichment_status, gdu_canonical,
                 created_at, updated_at)
            VALUES (%s, 'default', %s, %s, %s,
                    'place', %s, %s,
                    0.7, 1, 'complete', TRUE, NOW(), NOW())
            ON CONFLICT (node_id) DO UPDATE SET
                source_count = knowledge_nodes.source_count + 1,
                updated_at = NOW()
        """, (node_id, tenant_id, name, f"{subtype}: {name}",
              subtype, signal_rid))

        _STATUS["nodes_created"] += 1
        log.info("Created place node: %s → %s (%s)", node_id, name, subtype)
        return node_id


def _ensure_event_node(conn, tenant_id: str, signal_rid: int, enr: dict) -> Optional[str]:
    """Create an event knowledge_node for this article's classified event.
    Only creates nodes from LLM enrichments with real summaries — skips
    raw structured signals (earthquakes, financial) that have no narrative.
    """
    event_type = enr.get("event_type", "")
    summary = (enr.get("summary", "") or "").strip()
    # Require both an event type AND a real summary (min 20 chars)
    # This prevents junk nodes from USGS/NWS structured feeds
    if not event_type or len(summary) < 20:
        return None

    node_id = f"event:sig_{signal_rid}"

    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM knowledge_nodes WHERE node_id = %s", (node_id,))
        if cur.fetchone():
            return node_id

        headline = summary[:150] or f"{event_type} event"
        cur.execute("""
            INSERT INTO knowledge_nodes
                (node_id, project_id, tenant_id, title, description,
                 node_type, subtype, signal_rid,
                 confidence, source_count,
                 enrichment_status, gdu_canonical,
                 created_at, updated_at)
            VALUES (%s, 'default', %s, %s, %s,
                    'event', %s, %s,
                    0.6, 1, 'complete', TRUE, NOW(), NOW())
            ON CONFLICT (node_id) DO NOTHING
        """, (node_id, tenant_id, headline, summary,
              event_type, signal_rid))

        _STATUS["nodes_created"] += 1
        return node_id


def _ensure_topic_node(conn, tenant_id: str, keyword: str, signal_rid: int) -> Optional[str]:
    """Create a topic knowledge_node for a keyword. Dedup by normalized name."""
    normalized = keyword.lower().strip()
    if len(normalized) < 3:
        return None

    node_id = f"topic:{hashlib.sha1(normalized.encode()).hexdigest()[:12]}"

    with conn.cursor() as cur:
        cur.execute("SELECT node_id FROM knowledge_nodes WHERE node_id = %s", (node_id,))
        if cur.fetchone():
            cur.execute("""
                UPDATE knowledge_nodes SET source_count = source_count + 1, updated_at = NOW()
                WHERE node_id = %s
            """, (node_id,))
            return node_id

        cur.execute("""
            INSERT INTO knowledge_nodes
                (node_id, project_id, tenant_id, title, description,
                 node_type, subtype, signal_rid,
                 confidence, source_count,
                 enrichment_status, gdu_canonical,
                 created_at, updated_at)
            VALUES (%s, 'default', %s, %s, %s,
                    'topic', 'keyword', %s,
                    0.5, 1, 'complete', TRUE, NOW(), NOW())
            ON CONFLICT (node_id) DO UPDATE SET
                source_count = knowledge_nodes.source_count + 1,
                updated_at = NOW()
        """, (node_id, tenant_id, keyword, f"Topic: {keyword}", signal_rid))

        _STATUS["nodes_created"] += 1
        return node_id


def _ensure_document_node_and_mentions(conn, tenant_id: str, signal_rid: int,
                                        enr: dict, all_node_ids: dict[str, str]):
    """Create a document knowledge_node for the source article and
    'mentions' edges to every node extracted from it (entities, places,
    topics, events).

    This powers the live/network co-occurrence graph: two nodes that
    share a document via mentions edges co-occur in the same article.
    """
    doc_node_id = f"document:sig_{signal_rid}"

    with conn.cursor() as cur:
        # Ensure document node exists
        cur.execute("SELECT 1 FROM knowledge_nodes WHERE node_id = %s", (doc_node_id,))
        if not cur.fetchone():
            # Use the enrichment summary as headline, fall back to signal_rid
            headline = (enr.get("summary", "") or "")[:200] or f"Article #{signal_rid}"
            cur.execute("""
                INSERT INTO knowledge_nodes
                    (node_id, project_id, tenant_id, title, description,
                     node_type, subtype, signal_rid,
                     confidence, source_count,
                     enrichment_status, gdu_canonical,
                     created_at, updated_at)
                VALUES (%s, 'default', %s, %s, %s,
                        'document', 'article', %s,
                        0.8, 1,
                        'complete', TRUE,
                        NOW(), NOW())
                ON CONFLICT (node_id) DO UPDATE SET
                    source_count = knowledge_nodes.source_count + 1,
                    updated_at = NOW()
            """, (doc_node_id, tenant_id, headline, (enr.get("summary", "") or "")[:500], signal_rid))

        # Create mentions edges: document → each extracted node
        for name, target_node_id in all_node_ids.items():
            edge_id = hashlib.sha1(
                f"{doc_node_id}|{target_node_id}|mentions".encode()
            ).hexdigest()[:16]
            cur.execute("""
                INSERT INTO knowledge_edges
                    (edge_id, project_id, source_node_id, target_node_id,
                     relationship, strength, evidence, ai_generated)
                VALUES (%s, 'default', %s, %s, 'mentions', 0.8, %s, TRUE)
                ON CONFLICT DO NOTHING
            """, (edge_id, doc_node_id, target_node_id, name))


def _create_relationship(conn, tenant_id: str, source_id: int, target_id: int,
                          rel_type: str, temporal: str, signal_rid: int,
                          evidence: str):
    """Create or strengthen a relationship between two entities."""

    # Normalize relationship type
    rel_type = rel_type.replace(" ", "_").lower()
    if rel_type not in VALID_RELATIONSHIPS:
        # Try to map common variants
        rel_map = {
            "reports_on": "reports_on",
            "takes_photos_of": "targets",
            "reveals": "investigates",
            "arrests": "arrested",
        }
        rel_type = rel_map.get(rel_type, rel_type)

    is_current = temporal != "historical"

    with conn.cursor() as cur:
        # Check if relationship exists
        cur.execute("""
            SELECT rid, article_count FROM entity_relationships
            WHERE tenant_id = %s AND source_entity_id = %s
              AND target_entity_id = %s AND relationship = %s
            LIMIT 1
        """, (tenant_id, source_id, target_id, rel_type))
        row = cur.fetchone()

        if row:
            # Strengthen existing relationship
            cur.execute("""
                UPDATE entity_relationships
                SET article_count = COALESCE(article_count, 0) + 1,
                    is_current = %s,
                    updated_at = NOW()
                WHERE rid = %s
            """, (is_current, row[0]))
        else:
            # Create new relationship
            try:
                cur.execute("""
                    INSERT INTO entity_relationships
                        (tenant_id, source_entity_id, target_entity_id,
                         relationship, confidence, source, evidence,
                         is_current, article_count, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, 0.7, 'llm', %s, %s, 1, NOW(), NOW())
                """, (
                    tenant_id, source_id, target_id,
                    rel_type, evidence[:500], is_current,
                ))
                _STATUS["relationships_created"] += 1
            except Exception as e:
                log.debug("Relationship insert failed: %s", e)


def _append_entity_event(conn, entity_id: int, tenant_id: str,
                          event_type: str, data: dict,
                          signal_rid: Optional[int] = None):
    """Append an event to the entity's audit chain (append-only)."""

    with conn.cursor() as cur:
        # Get previous hash
        cur.execute("""
            SELECT event_hash FROM entity_events
            WHERE entity_id = %s ORDER BY created_at DESC LIMIT 1
        """, (entity_id,))
        prev = cur.fetchone()
        previous_hash = prev[0] if prev else None

        # Compute chain hash
        hash_input = (previous_hash or "genesis") + json.dumps(data, sort_keys=True)
        event_hash = hashlib.sha256(hash_input.encode()).hexdigest()

        cur.execute("""
            INSERT INTO entity_events
                (entity_id, tenant_id, event_type, source, data,
                 signal_rid, caused_by, event_hash, previous_hash)
            VALUES (%s, %s, %s, 'node_builder', %s, %s, 'node_builder', %s, %s)
        """, (
            entity_id, tenant_id, event_type,
            json.dumps(data), signal_rid,
            event_hash, previous_hash,
        ))


# ─── Auto-trigger: queue investigation when entity crosses threshold ──────────

# Signal count thresholds for auto-queueing investigations.
# Once an entity is mentioned in enough articles, we auto-investigate it
# across public data sources (SEC, FEC, courts, etc.).
INVESTIGATION_THRESHOLD = 5    # articles mentioning entity
INVESTIGATION_COOLDOWN_HOURS = 24  # don't re-investigate too soon


def _maybe_queue_investigation(entity_id: int, tenant_id: str, entity_type: str,
                                name: str, signal_count: int):
    """Queue an investigation for an entity if it crosses the signal threshold.

    Called after entity resolution when signal_count is incremented.
    Delegates to investigation_queue.try_queue_investigation which handles
    eligibility, cooldown, and race-safe insertion via migration 119's
    partial unique index.
    """
    if signal_count < INVESTIGATION_THRESHOLD:
        return

    from core.research.investigation_queue import try_queue_investigation
    inv_id, outcome = try_queue_investigation(
        entity_id=entity_id,
        tenant_id=tenant_id,
        seed_name=name,
        entity_type=entity_type,
        discovered_from="threshold_admit",
    )
    if outcome == "queued":
        log.info("Auto-queued investigation %s for %s (%s, signal_count=%d)",
                 inv_id, name, entity_type, signal_count)


def _mark_enrichment_processed(enrichment_id: int):
    """Mark an enrichment as processed by inserting a marker entity_event.

    We use entity_id=0 as a synthetic marker. The _find_unprocessed_enrichments
    query checks for this marker via signal_rid matching.
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Get the signal_rid for this enrichment
                cur.execute("""
                    SELECT signal_rid, tenant_id FROM signal_enrichments
                    WHERE enrichment_id = %s
                """, (enrichment_id,))
                row = cur.fetchone()
                if row:
                    cur.execute("""
                        INSERT INTO entity_events
                            (entity_id, tenant_id, event_type, source, data,
                             signal_rid, caused_by)
                        VALUES (0, %s, 'enrichment_processed', 'node_builder',
                                %s, %s, 'node_builder')
                    """, (row[1], json.dumps({"enrichment_id": enrichment_id}), row[0]))
            conn.commit()
    except Exception as e:
        log.debug("Failed to mark enrichment processed: %s", e)
