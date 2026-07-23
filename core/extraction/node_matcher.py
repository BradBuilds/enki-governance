"""
Tier 1: Fast Node Matcher — runs on every incoming article (no LLM).

Matches article text against existing canonical entity nodes using
keyword/name matching. One article can match multiple nodes. Each
match creates an edge (article → node) that strengthens relationship
connections in the graph.

This is the instant pass — 95% of articles mention entities we already
know. The LLM (Tier 2) only fires for new discoveries.

Called from:
  - Signal ingestion pipeline (after article stored)
  - Watch topic fanout (after article attributed to topic)
  - Batch reprocessing (for historical articles)
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.extraction.node_matcher")

# Cache of canonical entities — refreshed periodically
_ENTITY_CACHE: Optional[dict] = None
_CACHE_AGE: Optional[datetime] = None
_CACHE_MAX_AGE = 300  # 5 minutes


def match_article_to_nodes(
    article_id: str,
    title: str,
    full_text: str = "",
    source_name: str = "",
    topic_id: Optional[str] = None,
    tenant_id: str = "default",
    dsn: Optional[str] = None,
) -> dict:
    """Match an article against all known entities. Returns match stats.

    Fast path — no LLM, just text matching against canonical names + aliases.
    One article can match many nodes. Each match:
      - Links article to entity via watch_topic_entities (if topic_id)
      - Updates entity mention_count
      - Strengthens relationship edges between co-mentioned entities
      - Flags unmatched proper nouns for Tier 2 LLM processing
    """
    entities = _get_entity_cache(tenant_id, dsn)
    if not entities:
        return {"matched": 0, "unmatched_candidates": 0, "trigger_llm": False}

    # Build searchable text
    search_text = f" {title} {full_text[:2000]} ".lower()

    # Find all matching entities
    matched_entities: list[dict] = []
    matched_ids: set[int] = set()

    for ent in entities:
        names_to_check = [ent["normalized_name"]]
        names_to_check.extend([a.lower() for a in (ent.get("aliases") or [])])

        for name in names_to_check:
            if not name or len(name) < 3:
                continue
            # Word boundary matching — "iran" shouldn't match "irregular"
            pattern = f" {re.escape(name)} "
            if re.search(pattern, search_text):
                if ent["entity_id"] not in matched_ids:
                    matched_entities.append(ent)
                    matched_ids.add(ent["entity_id"])
                break

    if not matched_entities:
        # Check for unmatched proper nouns — potential new entities for Tier 2
        unmatched = _find_unmatched_proper_nouns(title, entities)
        return {
            "matched": 0,
            "unmatched_candidates": len(unmatched),
            "candidates": unmatched[:10],
            "trigger_llm": len(unmatched) >= 2,
        }

    # Write matches to database (includes geocoding for location entities)
    _record_matches(article_id, matched_entities, topic_id, tenant_id, dsn)

    # Check for potential new entities in unmatched text
    unmatched = _find_unmatched_proper_nouns(title, entities)

    return {
        "matched": len(matched_entities),
        "entities": [{"name": e["canonical_name"], "type": e["canonical_type"],
                       "entity_id": e["entity_id"]} for e in matched_entities],
        "unmatched_candidates": len(unmatched),
        "candidates": unmatched[:10],
        "trigger_llm": len(unmatched) >= 2,
    }


def match_batch(
    articles: list[dict],
    topic_id: Optional[str] = None,
    tenant_id: str = "default",
    dsn: Optional[str] = None,
) -> dict:
    """Match a batch of articles. Returns aggregate stats + LLM trigger decision."""
    total_matched = 0
    total_unmatched = 0
    all_candidates: list[str] = []
    entity_mention_counts: dict[int, int] = {}
    article_entity_map: dict[str, list[int]] = {}

    for art in articles:
        result = match_article_to_nodes(
            article_id=art.get("article_id", ""),
            title=art.get("title", ""),
            full_text=art.get("full_text", ""),
            source_name=art.get("source_name", ""),
            topic_id=topic_id,
            tenant_id=tenant_id,
            dsn=dsn,
        )
        total_matched += result["matched"]
        total_unmatched += result.get("unmatched_candidates", 0)
        for c in result.get("candidates", []):
            if c not in all_candidates:
                all_candidates.append(c)

        # Track which entities each article matched
        for ent in result.get("entities", []):
            eid = ent["entity_id"]
            entity_mention_counts[eid] = entity_mention_counts.get(eid, 0) + 1
            art_id = art.get("article_id", "")
            if art_id not in article_entity_map:
                article_entity_map[art_id] = []
            article_entity_map[art_id].append(eid)

    # Build relationship edges from co-mentions
    _build_relationship_edges(article_entity_map, topic_id, tenant_id, dsn)

    # Decide if Tier 2 LLM should run
    trigger_llm = (
        len(all_candidates) >= 5 or
        total_unmatched > len(articles) * 0.3  # >30% articles have unknown entities
    )

    return {
        "articles_processed": len(articles),
        "total_matched": total_matched,
        "total_unmatched": total_unmatched,
        "unique_candidates": all_candidates[:20],
        "trigger_llm": trigger_llm,
        "entity_mentions": entity_mention_counts,
    }


def _get_entity_cache(tenant_id: str, dsn: Optional[str] = None) -> list[dict]:
    """Get cached entity list, refreshing if stale."""
    global _ENTITY_CACHE, _CACHE_AGE

    now = datetime.now(timezone.utc)
    if _ENTITY_CACHE is not None and _CACHE_AGE and (now - _CACHE_AGE).total_seconds() < _CACHE_MAX_AGE:
        return _ENTITY_CACHE

    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT entity_id, canonical_name, normalized_name,
                           canonical_type, aliases, signal_count
                    FROM entities
                    WHERE tenant_id = %s
                    ORDER BY signal_count DESC
                    LIMIT 5000
                """, (tenant_id,))
                _ENTITY_CACHE = [{
                    "entity_id": r[0],
                    "canonical_name": r[1],
                    "normalized_name": r[2],
                    "canonical_type": r[3],
                    "aliases": r[4] or [],
                    "signal_count": r[5] or 0,
                } for r in cur.fetchall()]
                _CACHE_AGE = now
    except Exception as e:
        log.debug("Entity cache refresh failed: %s", e)
        _ENTITY_CACHE = _ENTITY_CACHE or []

    return _ENTITY_CACHE


def _find_unmatched_proper_nouns(title: str, known_entities: list[dict]) -> list[str]:
    """Find capitalized multi-word phrases in the title that don't match any known entity."""
    known_names = set()
    for ent in known_entities:
        known_names.add(ent["normalized_name"])
        for alias in (ent.get("aliases") or []):
            known_names.add(alias.lower())

    candidates = []
    # Match capitalized phrases (2+ words)
    for match in re.finditer(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b', title):
        name = match.group(1).strip()
        if len(name) > 4 and name.lower() not in known_names:
            candidates.append(name)

    # Match single capitalized words that might be entity names (not common English words)
    common_words = {"the", "this", "that", "with", "from", "after", "before", "under",
                    "over", "into", "about", "first", "last", "new", "old", "just",
                    "now", "also", "more", "most", "some", "many", "much", "very"}
    for match in re.finditer(r'\b([A-Z][a-z]{3,})\b', title):
        word = match.group(1)
        if word.lower() not in known_names and word.lower() not in common_words:
            # Only if it looks like a proper noun (not start of sentence)
            pos = match.start()
            if pos > 0 and title[pos-1] not in '.!?\n':
                candidates.append(word)

    return list(set(candidates))


def _record_matches(
    article_id: str,
    matched_entities: list[dict],
    topic_id: Optional[str],
    tenant_id: str,
    dsn: Optional[str],
):
    """Record article→entity matches in the database."""
    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                for ent in matched_entities:
                    # Update entity signal_count
                    cur.execute("""
                        UPDATE entities SET
                            signal_count = signal_count + 1,
                            last_seen = NOW(),
                            updated_at = NOW()
                        WHERE entity_id = %s
                    """, (ent["entity_id"],))

                    # If topic_id, update watch_topic_entities
                    if topic_id:
                        import hashlib
                        ent_key = f"{ent['canonical_name']}|{ent['canonical_type']}"
                        te_id = f"ent_{hashlib.md5(ent_key.encode()).hexdigest()[:12]}"
                        cur.execute("""
                            INSERT INTO watch_topic_entities
                                (entity_id, topic_id, tenant_id, entity_type, name,
                                 mention_count, canonical_entity_id, resolution_status,
                                 article_ids)
                            VALUES (%s, %s, %s, %s, %s, 1, %s, 'matched', ARRAY[%s])
                            ON CONFLICT (entity_id) DO UPDATE SET
                                mention_count = watch_topic_entities.mention_count + 1,
                                article_ids = array_append(
                                    CASE WHEN %s = ANY(watch_topic_entities.article_ids)
                                         THEN watch_topic_entities.article_ids
                                         ELSE watch_topic_entities.article_ids END,
                                    CASE WHEN %s = ANY(watch_topic_entities.article_ids)
                                         THEN NULL ELSE %s END
                                ),
                                updated_at = NOW()
                        """, (
                            te_id, topic_id, tenant_id,
                            ent["canonical_type"], ent["canonical_name"],
                            ent["entity_id"], article_id,
                            article_id, article_id, article_id,
                        ))

                # Update topic entity count
                if topic_id:
                    cur.execute("""
                        UPDATE watch_topics SET
                            entity_count = (SELECT count(*) FROM watch_topic_entities WHERE topic_id = %s),
                            updated_at = NOW()
                        WHERE topic_id = %s
                    """, (topic_id, topic_id))

            conn.commit()

        # Geocode location entities (after commit so the rows exist)
        if topic_id:
            try:
                from core.extraction.geocoder import geocode_and_update_entity, GEOCODABLE_TYPES
                for ent in matched_entities:
                    if ent["canonical_type"] in GEOCODABLE_TYPES:
                        ent_key = f"{ent['canonical_name']}|{ent['canonical_type']}"
                        te_id = f"ent_{hashlib.md5(ent_key.encode()).hexdigest()[:12]}"
                        geocode_and_update_entity(te_id, ent["canonical_name"], ent["canonical_type"], topic_id, tenant_id)
            except Exception as e:
                log.debug("Geocoding failed: %s", e)

    except Exception as e:
        log.debug("Record matches failed: %s", e)


def _build_relationship_edges(
    article_entity_map: dict[str, list[int]],
    topic_id: Optional[str],
    tenant_id: str,
    dsn: Optional[str],
):
    """Build/strengthen relationship edges between entities that co-appear in articles."""
    # Count co-occurrences
    edge_counts: dict[tuple[int, int], int] = {}
    for art_id, entity_ids in article_entity_map.items():
        for i, eid1 in enumerate(entity_ids):
            for eid2 in entity_ids[i+1:]:
                key = (min(eid1, eid2), max(eid1, eid2))
                edge_counts[key] = edge_counts.get(key, 0) + 1

    if not edge_counts:
        return

    # Write co-occurrence edges to entity_relationships table
    try:
        from core.research.entity_links import persist_co_occurrence_edges
        written = persist_co_occurrence_edges(edge_counts, tenant_id, dsn)
        if written:
            log.info("Wrote %d co-occurrence edges for topic %s", written, topic_id)
    except Exception as e:
        log.debug("Co-occurrence edge write failed: %s", e)


def invalidate_cache():
    """Force refresh of entity cache. Call after Tier 2 creates new entities."""
    global _ENTITY_CACHE, _CACHE_AGE
    _ENTITY_CACHE = None
    _CACHE_AGE = None
