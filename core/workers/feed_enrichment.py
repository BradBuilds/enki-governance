"""
Feed Enrichment Worker — continuous Tier 2 LLM processing of feed articles.

Runs as a daemon thread alongside the existing LLM extraction worker.
Processes general feed articles (NOT watch-topic-specific) through Gemma
to extract richer entities, relationships, and event classification.

Priority system:
  - Yields to watch topic backfill and entity research (high priority)
  - Processes newest articles first (freshest content most valuable)
  - Sleeps when caught up, wakes on new articles
  - Batches 5 articles per LLM call to maximize throughput

Writes enrichment results to signal_enrichments table (append-only).
NEVER updates the signals table — source data is immutable.
Each enrichment is a new row, hash-chained for audit trail.
Also updates facet_counts for trending and snapshot data.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.feed_enrichment")

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "articles_processed": 0,
    "articles_enriched": 0,
    "entities_found": 0,
    "relationships_found": 0,
    "errors": 0,
    "batch_num": 0,
    # Phase 1 dedup metrics (the internal news collapse plan).
    # dedup_linked: number of duplicate-headline articles that bypassed the LLM
    # by linking to an existing enrichment. Should be >0 within an hour of
    # deploying migration 045 if the pipeline has any duplicate headlines.
    "dedup_linked": 0,
    "llm_called": 0,
}

CHECK_INTERVAL = 15       # Check for work every 15 seconds
BATCH_SIZE = 1            # ONE article per LLM call (avoids truncation)
IDLE_BACKOFF = 60         # Sleep when no work
MAX_BACKOFF = 120         # Max sleep between checks

# ── News LLM budget (token bucket) ─────────────────────────────────
# Operator-set ceiling on how many news articles get LLM enrichment per
# minute. Lets the operator preserve GPU/LLM capacity for research
# (investigations, dossier work) instead of burning it on broad news
# triage they may not care about.
#
# The bucket refills at (max_per_min / 60) tokens per second and caps
# at max_per_min. Each LLM-eligible article consumes 1 token. When
# tokens < 1, the worker self-throttles (sleeps briefly) until tokens
# refill. max_per_min = 0 pauses news LLM entirely.
#
# Source of truth: tenant_settings.news_llm_max_per_min. Re-read every
# SETTINGS_REFRESH_SECONDS so operator changes propagate without restart.
SETTINGS_REFRESH_SECONDS = 60
BUDGET_THROTTLE_BACKOFF  = 5     # how long to sleep when tokens exhausted
_BUDGET = {
    "max_per_min": 30,
    "tokens": 0.0,
    "last_refill": time.time(),
    "last_settings_check": 0.0,
}


def _refresh_budget(tenant_id: str) -> None:
    """Refill token bucket; re-read the cap from tenant_settings occasionally."""
    now = time.time()
    if now - _BUDGET["last_settings_check"] > SETTINGS_REFRESH_SECONDS:
        try:
            from services.api.src.routes.tenant_settings import read_tenant_settings
            s = read_tenant_settings(tenant_id)
            cap = max(0, int(s.get("news_llm_max_per_min", 30)))
            _BUDGET["max_per_min"] = cap
        except Exception as e:
            log.debug("budget settings refresh failed: %s", e)
        _BUDGET["last_settings_check"] = now

    elapsed = max(0.0, now - _BUDGET["last_refill"])
    cap = _BUDGET["max_per_min"]
    if cap > 0:
        _BUDGET["tokens"] = min(float(cap), _BUDGET["tokens"] + elapsed * (cap / 60.0))
    else:
        _BUDGET["tokens"] = 0.0
    _BUDGET["last_refill"] = now
    _STATUS["budget_max_per_min"] = cap
    _STATUS["budget_tokens"] = round(_BUDGET["tokens"], 2)


def _try_consume_token() -> bool:
    if _BUDGET["tokens"] >= 1.0:
        _BUDGET["tokens"] -= 1.0
        return True
    return False


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_worker_loop, args=(tenant_id,),
        name="feed-enrichment-worker", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    log.info("Feed enrichment worker started")
    return {"status": "started"}


def stop_worker():
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=10)
    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"
    return {"status": "stopped"}


def _worker_loop(tenant_id: str):
    _STATUS["running"] = True
    backoff = CHECK_INTERVAL

    while not _STOP_EVENT.is_set():
        try:
            _refresh_budget(tenant_id)

            if _BUDGET["max_per_min"] == 0:
                _STATUS["phase"] = "paused (news_llm_max_per_min=0)"
                backoff = SETTINGS_REFRESH_SECONDS
            elif not _try_consume_token():
                _STATUS["phase"] = "throttled (budget exhausted, waiting for refill)"
                backoff = BUDGET_THROTTLE_BACKOFF
            else:
                processed = _process_batch(tenant_id)
                if processed > 0:
                    backoff = CHECK_INTERVAL  # Reset on success
                    _STATUS["phase"] = "processing"
                else:
                    # No work — give the token back so we don't drain the
                    # bucket on idle checks.
                    _BUDGET["tokens"] = min(float(_BUDGET["max_per_min"]),
                                            _BUDGET["tokens"] + 1.0)
                    backoff = min(backoff + 30, IDLE_BACKOFF)
                    _STATUS["phase"] = "idle — caught up"
        except Exception as e:
            log.warning("Feed enrichment error: %s", e)
            _STATUS["errors"] += 1
            _STATUS["phase"] = f"error: {str(e)[:60]}"
            backoff = min(backoff * 2, MAX_BACKOFF)

        # Interruptible sleep
        for _ in range(int(backoff)):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)

    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"


def _headline_hash(headline: str) -> str:
    """Compute a hash for deduplication. Normalize whitespace and case."""
    import hashlib
    normalized = " ".join(headline.lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _link_duplicate(signal_rid: int, existing_enrichment_id: int, tenant_id: str):
    """Link a duplicate signal to an existing enrichment without calling the LLM.

    Creates a lightweight enrichment record that points to the original.
    This way the signal is marked as 'processed' and won't be picked up again.
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Copy the existing enrichment's data for this signal. We also
                # carry over headline_hash so the dedup-link row remains a valid
                # match target for any future near-duplicates of the same headline.
                cur.execute("""
                    INSERT INTO signal_enrichments
                        (signal_rid, tenant_id, tier, method, model,
                         entities, relationships, event_type, importance,
                         keywords, summary,
                         enrichment_hash, headline_hash, previous_hash, extracted_at)
                    SELECT %s, tenant_id, tier, 'dedup_link', model,
                           entities, relationships, event_type, importance,
                           keywords, summary,
                           enrichment_hash, headline_hash, previous_hash, NOW()
                    FROM signal_enrichments
                    WHERE enrichment_id = %s
                """, (signal_rid, existing_enrichment_id))
            conn.commit()
        _STATUS.setdefault("dedup_linked", 0)
        _STATUS["dedup_linked"] += 1
    except Exception as e:
        log.debug("Dedup link failed: %s", e)


def _process_batch(tenant_id: str) -> int:
    """Find and process ONE article. Deduplicates before calling LLM."""

    # Check if LLM is available
    provider = _get_provider()
    if not provider:
        _STATUS["phase"] = "no LLM provider available"
        return 0

    # Yield to higher-priority work (topic backfill, research)
    if _is_high_priority_running():
        _STATUS["phase"] = "yielding to backfill/research"
        return 0

    # Find articles needing enrichment (fetch a small batch for dedup checking)
    articles = _find_articles_needing_enrichment(tenant_id, 10)
    if not articles:
        return 0

    # Dedup: check if this headline was already enriched
    processed_count = 0
    art = None

    for candidate in articles:
        h_hash = _headline_hash(candidate["headline"])

        # Hash-based dedup: ask "have we already LLM-enriched a headline that
        # normalizes to the same hash?". Indexed lookup via idx_se_headline_hash
        # (migration 045). The previous implementation compared LEFT(summary,200)
        # to LEFT(headline,200) — wrong fields, full table scan, never matched.
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT se.enrichment_id FROM signal_enrichments se
                        WHERE se.tenant_id = %s
                          AND se.headline_hash = %s
                          AND se.tier >= 2
                          AND se.method = 'llm'
                        ORDER BY se.extracted_at ASC
                        LIMIT 1
                    """, (tenant_id, h_hash))
                    existing = cur.fetchone()

                    if existing:
                        # Duplicate! Link without LLM call
                        _link_duplicate(candidate["rid"], existing[0], tenant_id)
                        processed_count += 1
                        _STATUS["articles_processed"] += 1
                        continue
        except Exception:
            pass

        # Not a duplicate — this one needs LLM processing
        art = candidate
        break

    if not art:
        # All candidates were duplicates — we handled them
        if processed_count > 0:
            log.info("Dedup: linked %d duplicate articles (no LLM needed)", processed_count)
            return processed_count
        return 0

    _STATUS["batch_num"] += 1
    _STATUS["phase"] = f"Batch {_STATUS['batch_num']}: enriching '{art['headline'][:50]}...'"
    log.info("Feed enrichment batch %d: %s", _STATUS["batch_num"], art["headline"][:80])

    # Build LLM prompt (one article)
    headline = art["headline"][:300]
    text = art.get("text", "")[:1500]

    prompt = f"""You are an intelligence analyst. Extract structured intelligence from this article.

HEADLINE: {headline}
{f"TEXT: {text}" if text else ""}

Return ONLY valid JSON (no markdown, no explanation):
{{
  "articles": [
    {{
      "index": 1,
      "summary": "2-3 sentence intelligence summary: what happened, who did it, why it matters",
      "why_it_matters": "one sentence strategic significance",
      "event_type": "military_action|diplomatic|political|economic|disaster|cyber|legal|scientific|terrorism|archaeology|other",
      "importance": 3,

      "atomic_facts": [
        {{"kind": "number", "fact": "20% of global oil passes through the Strait of Hormuz", "value": "20%"}},
        {{"kind": "date", "fact": "IRGC seized the cargo vessel", "value": "2026-04-22"}},
        {{"kind": "location", "fact": "vessel intercepted in the Strait of Hormuz", "value": "Strait of Hormuz"}},
        {{"kind": "quote", "fact": "Pentagon response to seizure", "value": "This will not go unanswered", "subject": "Pentagon spokesperson"}}
      ],
      "claims": [
        {{"claim": "Iran is willing to negotiate if sanctions are lifted", "source": "Iranian Foreign Minister", "confidence": "medium"}},
        {{"claim": "The vessel was carrying contraband weapons", "source": "IRGC statement", "confidence": "low"}}
      ],

      "people": [{{"name": "Full Name", "title": "role/position", "organization": "org name", "role_in_article": "decision-maker|target|source|witness|victim", "significance": "central|supporting|peripheral"}}],
      "organizations": [{{"name": "Org Name", "type": "government|company|ngo|military|media|research|other", "country": "ISO code", "role_in_article": "actor|target|source|regulator"}}],
      "locations": [{{"name": "Place Name", "country": "ISO code", "type": "city|country|region|facility|strait|ocean|archaeological_site", "relevance": "event_location|headquarters|border|discovery_site"}}],
      "relationships": [{{"source": "Entity A", "target": "Entity B", "type": "meets_with|opposes|supports|attacks|sanctions|leads|member_of|allied_with|investigates|funds|seizes|threatens|negotiates_with", "temporal": "current|historical"}}],
      "events": [{{"title": "brief event description", "date": "YYYY-MM-DD or approximate", "is_new": true, "type": "military|diplomatic|political|economic|disaster|cyber|legal|scientific|archaeological"}}],
      "quotes": [{{"speaker": "Full Name", "text": "exact quote"}}],
      "quantities": [{{"value": "number", "unit": "unit", "context": "what it measures"}}]
    }}
  ]
}}

Rules:
- atomic_facts must be SUBSTANTIVE intelligence from the article content — specific numbers, dates of events, named locations of incidents, direct quotes from officials
- NEVER extract metadata like "article published date", "publication date", "author name" — those are NOT intelligence facts
- claims must be CONTESTABLE assertions that another source might disagree with — not obvious statements of fact
- Full formal names only (not pronouns or vague descriptions)
- ISO 3166-1 alpha-2 country codes (US, GB, UA, RU, CN, IR)
- Countries are ALWAYS locations with subtype "country" — NEVER classify a country as an organization or person
- Governments (e.g., "US Government", "Iranian regime") are organizations; the country itself (e.g., "Iran", "Egypt") is a location
- Demonyms like "Egyptian", "Iranian", "American" are NOT people — use the country name instead (Egypt, Iran, United States)
- "Egyptian pyramids" and "The Great Pyramid of Egypt" = location named "Great Pyramid", country "EG"
- Use the simplest canonical name: "Egypt" not "Arab Republic of Egypt", "Iran" not "Islamic Republic of Iran"
- importance: 1=routine, 2=noteworthy, 3=significant, 4=major development, 5=critical/breaking"""

    try:
        # Acquire LLM slot
        try:
            from core.workers.reconciliation import acquire_llm_slot, release_llm_slot
            if not acquire_llm_slot(timeout=60):
                _STATUS["phase"] = "waiting for LLM slot"
                return processed_count
        except ImportError:
            pass

        from core.ai.provider import LLMMessage
        response = provider.complete(
            [LLMMessage(role="user", content=prompt)],
            max_tokens=4000, temperature=0.0,
        )

        try:
            from core.workers.reconciliation import release_llm_slot
            release_llm_slot()
        except Exception:
            pass

        if not response.content or response.error:
            log.warning("LLM returned empty/error: %s", response.error)
            _STATUS["errors"] += 1
            return processed_count

        # Parse response
        result = _parse_llm_response(response.content)
        if not result:
            _STATUS["errors"] += 1
            return processed_count

        # Apply enrichment
        enriched = _apply_enrichment([art], result, tenant_id)
        _STATUS["articles_processed"] += 1
        _STATUS["articles_enriched"] += enriched
        _STATUS["llm_called"] += 1
        _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()

        return processed_count + enriched

    except Exception as e:
        log.warning("Feed enrichment LLM call failed: %s", e)
        _STATUS["errors"] += 1
        try:
            from core.workers.reconciliation import release_llm_slot
            release_llm_slot()
        except Exception:
            pass
        return 0


def _find_articles_needing_enrichment(tenant_id: str, limit: int) -> list:
    """Find feed articles that have no Tier 2 enrichment yet.

    Uses LEFT JOIN on signal_enrichments to find signals that have
    never been LLM-enriched. Does NOT check signals.facets — enrichment
    results live in the separate append-only signal_enrichments table.
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT s.rid, s.signal_id, s.payload,
                           COALESCE(s.metadata->'facets', '{}'::jsonb) as facets
                    FROM signals s
                    LEFT JOIN signal_enrichments se
                        ON se.signal_rid = s.rid AND se.tier >= 2
                    WHERE s.tenant_id = %s
                      AND s.signal_type IN (
                          'signal.content.rss_entry',
                          'signal.content.news',
                          'signal.content.social_post',
                          'signal.content.web_page'
                      )
                      AND se.enrichment_id IS NULL
                      AND (s.metadata->>'_topic_id' IS NULL OR s.metadata->>'_topic_id' = '')
                    ORDER BY s.timestamp DESC
                    LIMIT %s
                """, (tenant_id, limit))

                articles = []
                for r in cur.fetchall():
                    payload = r[2] or {}

                    # Handle nested payloads (Reddit stores data under 'data' key)
                    nested = payload.get("data", {}) if isinstance(payload.get("data"), dict) else {}

                    headline = (
                        payload.get("title")
                        or payload.get("headline")
                        or nested.get("title")
                        or ""
                    )
                    if not headline or len(headline) < 15:
                        continue

                    text = (
                        payload.get("full_text")
                        or payload.get("summary")
                        or payload.get("og_description")
                        or payload.get("description")
                        or nested.get("selftext")
                        or ""
                    )

                    articles.append({
                        "rid": r[0],
                        "signal_id": r[1],
                        "headline": headline,
                        "text": text,
                        "existing_facets": r[3] or {},
                    })
                return articles
    except Exception as e:
        log.debug("Failed to find articles: %s", e)
        return []


def _parse_llm_response(content: str) -> dict | None:
    """Parse the LLM JSON response, handling common formatting issues.

    Gemma often wraps JSON in markdown fences (```json ... ```),
    includes thinking blocks, or returns truncated JSON.
    This parser handles all of these cases.
    """
    if not content:
        return None

    # Strip markdown code fences (```json ... ``` or ``` ... ```)
    cleaned = re.sub(r'```(?:json)?\s*', '', content)
    cleaned = cleaned.strip()

    # Strip thinking blocks (Gemma 4 chain-of-thought)
    cleaned = re.sub(r'<thinking>[\s\S]*?</thinking>', '', cleaned)
    cleaned = re.sub(r'Thinking.*?(?=\{|\[)', '', cleaned, flags=re.DOTALL)

    # Try direct parse of the cleaned content
    try:
        match = re.search(r'\{[\s\S]*\}', cleaned)
        if match:
            return json.loads(match.group())
    except json.JSONDecodeError:
        pass

    # Try extracting just the articles array
    try:
        match = re.search(r'\[[\s\S]*\]', cleaned)
        if match:
            articles = json.loads(match.group())
            if isinstance(articles, list):
                return {"articles": articles}
    except json.JSONDecodeError:
        pass

    # Try fixing truncated JSON (LLM hit max_tokens)
    try:
        # Find the last complete object in truncated JSON
        partial = re.search(r'\{[\s\S]*', cleaned)
        if partial:
            text = partial.group()
            # Count braces to find where to truncate
            depth = 0
            last_valid = 0
            for i, c in enumerate(text):
                if c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        last_valid = i + 1
                        break
            if last_valid > 0:
                return json.loads(text[:last_valid])
    except (json.JSONDecodeError, ValueError):
        pass

    log.warning("Failed to parse LLM response: %s", content[:300])
    return None


def _apply_enrichment(
    articles: list[dict],
    llm_result: dict,
    tenant_id: str,
) -> int:
    """Write LLM extraction results to signal_enrichments table (append-only).

    NEVER updates the signals table. Each enrichment is a new row in
    signal_enrichments, linked to the signal by signal_rid. The signals
    table stays immutable — source data integrity preserved.
    """
    import hashlib

    enriched_articles = llm_result.get("articles", [])
    enriched = 0

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                for llm_art in enriched_articles:
                    idx = llm_art.get("index", 0) - 1  # 0-indexed
                    if idx < 0 or idx >= len(articles):
                        continue

                    art = articles[idx]

                    # Build entity list — preserve ALL LLM-extracted fields
                    entities = []
                    for p in llm_art.get("people", []):
                        if p.get("name"):
                            entities.append({
                                "name": p["name"],
                                "type": "person",
                                "title": p.get("title", ""),
                                "organization": p.get("organization", ""),
                                "role_in_article": p.get("role_in_article", ""),
                                "significance": p.get("significance", ""),
                            })
                    for o in llm_art.get("organizations", []):
                        if o.get("name"):
                            entities.append({
                                "name": o["name"],
                                "type": "organization",
                                "subtype": o.get("type", ""),
                                "country": o.get("country", ""),
                                "role_in_article": o.get("role_in_article", ""),
                            })
                    for loc in llm_art.get("locations", []):
                        if loc.get("name"):
                            entities.append({
                                "name": loc["name"],
                                "type": "location",
                                "subtype": loc.get("type", ""),
                                "country": loc.get("country", ""),
                                "relevance": loc.get("relevance", ""),
                            })

                    relationships = llm_art.get("relationships", [])[:10]

                    # Store events as entities with type="event" so node_builder
                    # can create event nodes from them
                    for ev in llm_art.get("events", [])[:5]:
                        if ev.get("title"):
                            entities.append({
                                "name": ev["title"],
                                "type": "event",
                                "subtype": ev.get("type", ""),
                                "date": ev.get("date", ""),
                                "is_new": ev.get("is_new", False),
                            })

                    # Store quotes — valuable for provenance and attribution
                    for q in llm_art.get("quotes", [])[:5]:
                        if q.get("text") and q.get("speaker"):
                            entities.append({
                                "name": q["speaker"],
                                "type": "quote",
                                "text": q["text"][:500],
                            })

                    # Store quantities — metrics and measurements
                    for qt in llm_art.get("quantities", [])[:5]:
                        if qt.get("value"):
                            entities.append({
                                "name": f"{qt['value']} {qt.get('unit', '')}".strip(),
                                "type": "quantity",
                                "context": qt.get("context", ""),
                            })

                    # Store atomic facts — the core intelligence product
                    for fact in llm_art.get("atomic_facts", [])[:10]:
                        if fact.get("fact"):
                            entities.append({
                                "name": fact.get("value", fact["fact"][:60]),
                                "type": "fact",
                                "kind": fact.get("kind", "general"),
                                "fact": fact["fact"],
                                "value": fact.get("value", ""),
                                "subject": fact.get("subject", ""),
                            })

                    # Store claims — contestable assertions for verification
                    for claim in llm_art.get("claims", [])[:5]:
                        if claim.get("claim"):
                            entities.append({
                                "name": claim["claim"][:60],
                                "type": "claim",
                                "claim": claim["claim"],
                                "source": claim.get("source", ""),
                                "confidence": claim.get("confidence", "medium"),
                            })

                    keywords = [e["name"] for e in entities if e["type"] in ("person", "organization", "location")][:10]
                    event_type = llm_art.get("event_type")
                    importance = llm_art.get("importance")
                    why_it_matters = llm_art.get("why_it_matters", "")

                    _STATUS["entities_found"] += len(entities)
                    _STATUS["relationships_found"] += len(relationships)

                    # Compute chain hash
                    # Get previous hash for this signal (if any prior enrichment exists)
                    cur.execute("""
                        SELECT enrichment_hash FROM signal_enrichments
                        WHERE signal_rid = %s ORDER BY extracted_at DESC LIMIT 1
                    """, (art["rid"],))
                    prev_row = cur.fetchone()
                    previous_hash = prev_row[0] if prev_row else None

                    enrichment_data = json.dumps({
                        "signal_rid": art["rid"],
                        "entities": entities,
                        "relationships": relationships,
                        "event_type": event_type,
                        "importance": importance,
                    }, sort_keys=True)

                    hash_input = (previous_hash or "genesis") + enrichment_data
                    enrichment_hash = hashlib.sha256(hash_input.encode()).hexdigest()

                    # INSERT enrichment record (append-only, never update).
                    # headline_hash drives the dedup-before-LLM lookup at
                    # _process_batch; populated here so this enrichment becomes
                    # a valid future match target.
                    headline_hash = _headline_hash(art["headline"])
                    cur.execute("""
                        INSERT INTO signal_enrichments
                            (signal_rid, tenant_id, tier, method, model,
                             entities, relationships, event_type, importance,
                             keywords, summary,
                             enrichment_hash, headline_hash, previous_hash, extracted_at)
                        VALUES (%s, %s, 2, 'llm', %s,
                                %s, %s, %s, %s,
                                %s, %s,
                                %s, %s, %s, NOW())
                    """, (
                        art["rid"], tenant_id, "gemma4:e4b",
                        json.dumps(entities), json.dumps(relationships),
                        event_type, importance,
                        keywords,
                        (art["headline"][:200] + (" | " + why_it_matters if why_it_matters else ""))[:500],
                        enrichment_hash, headline_hash, previous_hash,
                    ))

                    # Update facet counts for trending
                    try:
                        from storage.signals.facets import increment_facet_counts
                        facets = {
                            "tier": 2,
                            "people": [e["name"] for e in entities if e["type"] == "person"],
                            "organizations": [e["name"] for e in entities if e["type"] == "organization"],
                            "countries": [e.get("country") for e in entities if e.get("country")],
                            "event_type": event_type,
                            "importance": importance,
                        }
                        increment_facet_counts(facets, art["signal_id"], tenant_id)
                    except Exception:
                        pass

                    enriched += 1

            conn.commit()

    except Exception as e:
        log.warning("Failed to write enrichment: %s", e)

    log.info("Enriched %d/%d articles (appended to signal_enrichments)", enriched, len(articles))
    return enriched


def _is_high_priority_running() -> bool:
    """Check if higher-priority LLM work is running (backfill, research)."""
    try:
        from core.workers.llm_extraction import get_worker_status
        status = get_worker_status()
        if status.get("running") and status.get("phase") not in ("idle", "stopped", None):
            return True
    except Exception:
        pass
    return False


def _get_provider():
    try:
        from core.ai.resolver import get_effective_provider
        return get_effective_provider()
    except Exception:
        return None
