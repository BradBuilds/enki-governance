"""
Tier 2: Background LLM Extraction Worker — fires only when triggered.

Runs as a daemon thread. Monitors for conditions that require LLM processing:
  1. New entity candidates from Tier 1 (unmatched proper nouns)
  2. Unprocessed articles above threshold
  3. Topics that need initial node library building
  4. Manual research requests

When triggered, uses the full LLM (gemma4:e2b) for:
  - Proper entity extraction with typing
  - Relationship detection
  - Contradiction scanning
  - New node creation through canonical resolver

Never crashes — exponential backoff on errors, throttled LLM calls.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.llm_extraction")

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "articles_processed": 0,
    "entities_created": 0,
    "errors": 0,
    "current_topic": None,
}

CHECK_INTERVAL = 60  # Check for work every 60 seconds
UNPROCESSED_THRESHOLD = 10  # Trigger LLM if 10+ unmatched articles
BATCH_SIZE = 10  # Process 10 articles per LLM batch (multi-article prompt)


def get_worker_status() -> dict:
    return dict(_STATUS)


def start_llm_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_worker_loop, args=(tenant_id,),
        name="llm-extraction-worker", daemon=True,
    )
    _WORKER_THREAD.start()
    log.info("LLM extraction worker started")


def stop_llm_worker():
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=10)


def _worker_loop(tenant_id: str):
    _STATUS["running"] = True
    backoff = CHECK_INTERVAL

    while not _STOP_EVENT.is_set():
        try:
            work_done = _check_for_work(tenant_id)
            if work_done:
                backoff = CHECK_INTERVAL  # Reset on success
            else:
                backoff = min(backoff + 30, 300)  # Slow down when idle
            _STATUS["phase"] = "idle"
        except Exception as e:
            log.warning("LLM worker error: %s", e)
            _STATUS["errors"] += 1
            _STATUS["phase"] = f"error: {str(e)[:50]}"
            backoff = min(backoff * 2, 600)

        for _ in range(int(backoff)):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)

    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"


def _check_for_work(tenant_id: str) -> bool:
    """Check all trigger conditions. Returns True if work was done."""

    # Trigger 1: Topics with articles that haven't been node-matched
    topics = _find_topics_needing_processing(tenant_id)
    if topics:
        for topic_id, title, article_count in topics:
            _STATUS["phase"] = f"Processing: {title}"
            _STATUS["current_topic"] = title
            _process_topic_articles(topic_id, tenant_id)
        return True

    # Trigger 2: Accumulated unmatched candidates
    candidates = _find_unmatched_candidates(tenant_id)
    if len(candidates) >= UNPROCESSED_THRESHOLD:
        _STATUS["phase"] = f"Extracting {len(candidates)} new entities..."
        _extract_new_entities(candidates, tenant_id)
        return True

    return False


def _find_topics_needing_processing(tenant_id: str) -> list:
    """Find topics where articles exist but haven't been node-matched."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT wt.topic_id, wt.title,
                           (SELECT count(*) FROM watch_topic_articles WHERE topic_id = wt.topic_id) as art_count,
                           wt.entity_count
                    FROM watch_topics wt
                    WHERE wt.tenant_id = %s AND wt.status = 'active'
                      AND wt.backfill_status IN ('complete', 'none')
                """, (tenant_id,))
                results = []
                for r in cur.fetchall():
                    art_count = r[2] or 0
                    entity_count = r[3] or 0
                    # If we have articles but few entities relative to articles, needs processing
                    if art_count > 0 and entity_count < art_count * 0.1:
                        results.append((r[0], r[1], art_count))
                return results
    except Exception:
        return []


def _process_topic_articles(topic_id: str, tenant_id: str):
    """Run Tier 1 node matching on all articles in a topic, then Tier 2 on unmatched."""
    from core.extraction.node_matcher import match_batch, invalidate_cache

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT article_id, title, full_text, source_name
                    FROM watch_topic_articles
                    WHERE topic_id = %s AND tenant_id = %s
                      AND (processing_status IS NULL OR processing_status = 'pending')
                    ORDER BY published_at DESC
                    LIMIT 200
                """, (topic_id, tenant_id))
                articles = [{
                    "article_id": r[0], "title": r[1] or "",
                    "full_text": r[2] or "", "source_name": r[3] or "",
                } for r in cur.fetchall()]

        if not articles:
            return

        log.info("Tier 1: Matching %d articles against %d known entities for topic %s",
                 len(articles), len(_get_entity_count(tenant_id)), topic_id)

        # Tier 1: Fast keyword matching
        batch_result = match_batch(articles, topic_id=topic_id, tenant_id=tenant_id)

        log.info("Tier 1 complete: %d matched, %d unmatched candidates, trigger_llm=%s",
                 batch_result["total_matched"], batch_result["total_unmatched"],
                 batch_result["trigger_llm"])

        # Mark articles as processed by Tier 1
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    for art in articles:
                        cur.execute("""
                            UPDATE watch_topic_articles SET processing_status = 'matched'
                            WHERE article_id = %s AND topic_id = %s
                        """, (art["article_id"], topic_id))
                conn.commit()
        except Exception:
            pass

        # Tier 2: If triggered, run LLM on unmatched candidates
        if batch_result["trigger_llm"] and batch_result.get("unique_candidates"):
            _run_tier2_extraction(
                articles, batch_result["unique_candidates"],
                topic_id, tenant_id,
            )
            invalidate_cache()

        # Geocode all location entities that lack coordinates
        try:
            from core.extraction.geocoder import backfill_coordinates
            geocoded = backfill_coordinates(topic_id, tenant_id)
            if geocoded:
                log.info("Geocoded %d location entities for topic %s", geocoded, topic_id)
        except Exception as e:
            log.debug("Geocode backfill failed: %s", e)

    except Exception as e:
        log.warning("Topic processing failed for %s: %s", topic_id, e)


def _run_tier2_extraction(
    articles: list[dict],
    candidates: list[str],
    topic_id: str,
    tenant_id: str,
):
    """Run LLM extraction on articles containing unmatched entity candidates."""
    _STATUS["phase"] = f"LLM: Extracting {len(candidates)} new entities..."

    provider = _get_provider()
    if not provider:
        log.info("Tier 2: No LLM provider available, skipping")
        return

    # Filter to articles that likely contain the candidates
    candidate_set = {c.lower() for c in candidates}
    relevant_articles = []
    for art in articles:
        text = f"{art['title']} {art.get('full_text', '')[:500]}".lower()
        if any(c in text for c in candidate_set):
            relevant_articles.append(art)

    if not relevant_articles:
        return

    log.info("Tier 2: Running LLM on %d articles with %d candidate entities",
             len(relevant_articles), len(candidates))

    # Process in batches — send multiple articles in one LLM prompt
    from core.ai.provider import LLMMessage

    for i in range(0, len(relevant_articles), BATCH_SIZE):
        batch = relevant_articles[i:i + BATCH_SIZE]

        # Build multi-article prompt
        article_block = ""
        for idx, art in enumerate(batch):
            article_block += f"\n[Article {idx+1}]\n{art['title']}\n{art.get('full_text', '')[:300]}\n"

        prompt = f"""Extract ALL named entities from these {len(batch)} articles. Return ONLY valid JSON.

{article_block}

Return a JSON array of objects:
[{{"name": "full formal name", "type": "person|organization|location|country|vessel|aircraft|facility", "articles": [1,2], "role": "brief context"}}]

Rules:
- Use the entity's most complete formal name
- For people, include any role/title surface form mentioned in the source
- "articles" field lists which article numbers (1-indexed) mention this entity
- Combine duplicate entities across articles into one entry
- Return empty array [] if no entities found"""

        try:
            # Throttle
            try:
                from core.workers.reconciliation import acquire_llm_slot, release_llm_slot
                if not acquire_llm_slot(timeout=120):
                    log.info("Tier 2: LLM throttled, will retry later")
                    return
            except ImportError:
                pass

            response = provider.complete(
                [LLMMessage(role="user", content=prompt)],
                max_tokens=800, temperature=0.0,
            )

            try:
                release_llm_slot()
            except Exception:
                pass

            if response.content and not response.error:
                import re
                match = re.search(r'\[.*\]', response.content, re.DOTALL)
                if match:
                    extracted = json.loads(match.group())
                    for ent in extracted:
                        if not isinstance(ent, dict) or not ent.get("name"):
                            continue
                        _create_entity_from_extraction(
                            ent, batch, topic_id, tenant_id,
                        )
                        _STATUS["entities_created"] += 1

        except Exception as e:
            log.debug("Tier 2 LLM extraction failed: %s", e)
            _STATUS["errors"] += 1

        _STATUS["articles_processed"] += len(batch)

    _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()


def _create_entity_from_extraction(
    ent_data: dict,
    batch_articles: list[dict],
    topic_id: str,
    tenant_id: str,
):
    """Create a canonical entity from LLM extraction, through the resolver."""
    try:
        from core.gdu.entity_resolution import resolve_entity

        result = resolve_entity(
            tenant_id=tenant_id,
            name=ent_data["name"],
            entity_type=ent_data.get("type", "unknown"),
            context=ent_data.get("role", ""),
            source_pipeline="tier2_llm_worker",
        )

        # Link to topic
        if result.entity_id and topic_id:
            import hashlib
            ent_key = f"{ent_data['name']}|{ent_data.get('type', 'unknown')}"
            te_id = f"ent_{hashlib.md5(ent_key.encode()).hexdigest()[:12]}"

            # Get article_ids for this entity
            article_indices = ent_data.get("articles", [])
            article_ids = []
            for idx in article_indices:
                if isinstance(idx, int) and 1 <= idx <= len(batch_articles):
                    article_ids.append(batch_articles[idx - 1].get("article_id", ""))

            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO watch_topic_entities
                            (entity_id, topic_id, tenant_id, entity_type, name,
                             mention_count, canonical_entity_id, resolution_status,
                             article_ids)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (entity_id) DO UPDATE SET
                            mention_count = watch_topic_entities.mention_count + EXCLUDED.mention_count,
                            canonical_entity_id = COALESCE(EXCLUDED.canonical_entity_id, watch_topic_entities.canonical_entity_id),
                            updated_at = NOW()
                    """, (
                        te_id, topic_id, tenant_id,
                        ent_data.get("type", "unknown"), ent_data["name"],
                        len(article_ids) or 1, result.entity_id,
                        result.action, article_ids[:50],
                    ))

                    # Update topic counts
                    cur.execute("""
                        UPDATE watch_topics SET
                            entity_count = (SELECT count(*) FROM watch_topic_entities WHERE topic_id = %s),
                            updated_at = NOW()
                        WHERE topic_id = %s
                    """, (topic_id, topic_id))

                conn.commit()

    except Exception as e:
        log.debug("Entity creation failed for %s: %s", ent_data.get("name"), e)


def _find_unmatched_candidates(tenant_id: str) -> list[str]:
    """Find accumulated unmatched entity candidates from resolution log."""
    # For now, return empty — candidates accumulate through Tier 1 batch results
    return []


def _get_entity_count(tenant_id: str) -> int:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM entities WHERE tenant_id = %s", (tenant_id,))
                return cur.fetchone()[0] or 0
    except Exception:
        return 0


def _get_provider():
    """Resolve via the effective-provider chain so any cloud provider
    configured in /ai/config wins over the legacy local default."""
    try:
        from core.ai.resolver import get_effective_provider
        return get_effective_provider()
    except Exception:
        return None
