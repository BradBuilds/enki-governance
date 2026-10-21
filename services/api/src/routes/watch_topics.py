"""
Watch Topics — OSINT monitoring endpoints.
POST   /watch-topics                         — create topic
GET    /watch-topics                         — list topics
GET    /watch-topics/{id}                    — get topic
PATCH  /watch-topics/{id}                    — update topic
DELETE /watch-topics/{id}                    — delete topic
GET    /watch-topics/{id}/clusters           — story clusters
GET    /watch-topics/{id}/articles           — articles
GET    /watch-topics/{id}/entities           — extracted entities
GET    /watch-topics/{id}/graph              — entity graph
GET    /watch-topics/{id}/timeline           — event timeline
POST   /watch-topics/{id}/promote/project    — create project from topic
POST   /watch-topics/{id}/promote/case       — create case from topic
GET    /watch-topics/{id}/projects           — linked projects
POST   /watch-topics/{id}/projects           — link project
DELETE /watch-topics/{id}/projects/{pid}     — unlink project
PATCH  /watch-topics/{id}/projects/{pid}     — update sync
GET    /watch-topics/{id}/cases              — linked cases
POST   /watch-topics/{id}/cases              — link case
DELETE /watch-topics/{id}/cases/{cid}        — unlink case
PATCH  /watch-topics/{id}/cases/{cid}        — update sync
POST   /watch-topics/{id}/entities/{eid}/promote — promote entity to contact
"""

from __future__ import annotations
from typing import Optional
from datetime import datetime, timezone
import uuid
import json

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from pydantic import BaseModel

router = APIRouter(tags=["watch-topics"])


class CreateTopicRequest(BaseModel):
    title: str
    keywords: Optional[list[str]] = None
    sources: Optional[dict] = None
    backfill_months: int = 0
    description: Optional[str] = None


class UpdateTopicRequest(BaseModel):
    title: Optional[str] = None
    keywords: Optional[list[str]] = None
    sources: Optional[dict] = None
    status: Optional[str] = None
    description: Optional[str] = None


class LinkProjectRequest(BaseModel):
    project_id: str


class LinkCaseRequest(BaseModel):
    case_id: str


class SyncUpdateRequest(BaseModel):
    auto_sync: bool


def _get_conn():
    from storage.postgres.db import get_connection
    return get_connection()


# Map of watch topic source flags → recipe IDs
_SOURCE_TO_RECIPES = {
    "gdelt": "gdelt_events",
    "rss": "custom_rss",
    "reddit": "reddit_subreddit",
    "twitter": None,  # requires API key, skip
    "hackernews": "hackernews",
    "usgs": "usgs_earthquakes",
    "nws": "nws_alerts",
    "adsb": "adsb_aircraft",
    "noaa": "noaa_weather",
}


def _auto_create_connectors(
    topic_id: str,
    tenant_id: str,
    keywords: str,
    sources: dict,
) -> list[str]:
    """Auto-create connectors for a watch topic based on selected sources.

    For each enabled source, instantiates the matching recipe with the
    topic's keywords and creates a live connector. The connector scheduler
    will start polling immediately on its next cycle (every 30s).
    """
    import logging
    log = logging.getLogger("enki.watch_topics")

    from storage.connectors.recipes import get_recipe, instantiate_recipe
    from storage.connectors.manager import create_connector

    created = []

    for source_key, enabled in sources.items():
        if not enabled:
            continue

        recipe_id = _SOURCE_TO_RECIPES.get(source_key)
        if not recipe_id:
            continue

        recipe = get_recipe(recipe_id)
        if not recipe:
            log.warning(f"Recipe {recipe_id} not found for source {source_key}")
            continue

        # Build user params from keywords
        user_params = {}
        for param in recipe.get("user_params", []):
            name = param["name"]
            if name in ("keywords", "keyword", "query", "q", "search"):
                user_params[name] = keywords
            elif name == "subreddit" and source_key == "reddit":
                # Use first keyword as subreddit guess
                user_params[name] = keywords.split()[0].lower().replace(" ", "")
            elif name == "feed_url" and source_key == "rss":
                # For RSS, create a Google News RSS feed for the keywords
                from urllib.parse import quote
                user_params[name] = f"https://news.google.com/rss/search?q={quote(keywords)}&hl=en-US&gl=US&ceid=US:en"
            elif param.get("default") is not None:
                user_params[name] = param["default"]

        try:
            instance = instantiate_recipe(recipe_id, user_params)
            if not instance:
                continue

            conn_config = instance["connector_config"]
            source_config = conn_config["source_config"]
            if instance.get("signal_type_hint"):
                source_config["signal_type_hint"] = instance["signal_type_hint"]

            # Tag with topic_id for traceability
            source_config["_topic_id"] = topic_id

            result = create_connector(
                tenant_id=tenant_id,
                device_id=f"topic_{topic_id}_{recipe_id}",
                connector_type=conn_config.get("connector_type", "pull"),
                source_type=conn_config["source_type"],
                source_config=source_config,
                poll_interval=conn_config.get("poll_interval", "15 minutes"),
            )

            connector_id = result.get("connector_id", "")
            created.append(connector_id)
            log.info(f"Auto-created connector {connector_id} ({recipe_id}) for topic {topic_id}")

        except Exception as e:
            log.warning(f"Failed to create connector for {recipe_id}: {e}")

    return created


@router.post("/watch-topics")
async def create_topic(body: CreateTopicRequest, auth: UnifiedAuthInfo = Depends(unified_auth)):
    topic_id = f"topic_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    # Sources are now an explicit user choice from the catalog. The
    # previous "always enable GDELT + hackernews" default was the second
    # contamination amplifier the Phase 1 plan called out — it injected
    # connectors no one asked for. If the body has no sources, the topic
    # is created with no sources and the user is expected to pick from
    # the source catalog (Phase 3).
    sources = body.sources or {}
    keywords = body.keywords or []

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO watch_topics (topic_id, tenant_id, identity_id, title, keywords, sources, description,
                    status, backfill_months, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (topic_id, auth.tenant_id, "api", body.title, keywords, json.dumps(sources),
                  body.description, "active", body.backfill_months, now, now))
            conn.commit()

    # Auto-create connectors from selected sources
    connector_ids = []
    if keywords:
        keyword_str = " ".join(keywords)
        connector_ids = _auto_create_connectors(
            topic_id=topic_id,
            tenant_id=auth.tenant_id,
            keywords=keyword_str,
            sources=sources,
        )

        # Store connector IDs back on the topic
        if connector_ids:
            with _get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE watch_topics SET connector_ids = %s WHERE topic_id = %s AND tenant_id = %s",
                        (connector_ids, topic_id, auth.tenant_id),
                    )
                    conn.commit()

    # Auto-start research immediately, using the user's requested
    # backfill_months (defaults to 0 — ZERO backfill unless asked).
    # Hardcoding 24 here was the contamination amplifier the Phase 1
    # plan called out: every topic created via API silently kicked off
    # 24 months of research, regardless of YAML / body intent.
    if keywords and body.backfill_months > 0:
        try:
            from core.research.backfill import start_backfill
            start_backfill(topic_id, keywords, sources, backfill_months=body.backfill_months, tenant_id=auth.tenant_id)
            with _get_conn() as conn2:
                with conn2.cursor() as cur2:
                    cur2.execute("UPDATE watch_topics SET backfill_status = 'running' WHERE topic_id = %s", (topic_id,))
                    conn2.commit()
        except Exception as e:
            import logging
            logging.getLogger("enki.watch").warning(f"Auto-backfill failed to start: {e}")

    return JSONResponse(status_code=201, content={
        "topic_id": topic_id,
        "title": body.title,
        "connectors_created": len(connector_ids),
        "connector_ids": connector_ids,
        "research_started": True,
    })


@router.get("/watch-topics")
async def list_topics(auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT wt.topic_id, wt.title, wt.status, wt.keywords, wt.description,
                    wt.created_at, wt.updated_at,
                    (SELECT COUNT(*) FROM watch_topic_articles WHERE topic_id = wt.topic_id AND tenant_id = wt.tenant_id),
                    (SELECT COUNT(*) FROM watch_topic_clusters WHERE topic_id = wt.topic_id AND tenant_id = wt.tenant_id),
                    wt.backfill_status, wt.deep_research_status, wt.deep_research_progress,
                    wt.ai_summary,
                    (SELECT COUNT(*) FROM watch_topic_entities WHERE topic_id = wt.topic_id AND tenant_id = wt.tenant_id),
                    (SELECT COUNT(*) FROM watch_topic_entities WHERE topic_id = wt.topic_id AND tenant_id = wt.tenant_id
                        AND entity_type IN ('location','country','city','region','place','facility'))
                FROM watch_topics wt WHERE wt.tenant_id = %s ORDER BY wt.updated_at DESC
            """, (auth.tenant_id,))
            topics = []
            for r in cur.fetchall():
                progress = r[11] or {}
                if isinstance(progress, str):
                    try: progress = json.loads(progress)
                    except Exception: progress = {}
                topics.append({
                    "topic_id": r[0], "title": r[1], "status": r[2], "keywords": r[3] or [],
                    "description": r[4],
                    "created_at": r[5].isoformat() if r[5] else None,
                    "updated_at": r[6].isoformat() if r[6] else None,
                    "article_count": r[7] or 0, "cluster_count": r[8] or 0,
                    "backfill_status": r[9] or "none",
                    "deep_research_status": r[10] or "none",
                    "deep_research_progress": progress,
                    "ai_summary": r[12],
                    "entity_count": r[13] or 0, "location_count": r[14] or 0,
                })
    return {"topics": topics, "count": len(topics)}


@router.get("/watch-topics/{topic_id}")
async def get_topic(topic_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT wt.topic_id, wt.title, wt.status, wt.keywords, wt.sources, wt.description,
                    wt.backfill_months, wt.ai_summary, wt.summary_updated_at, wt.created_at, wt.updated_at,
                    wt.backfill_status, wt.deep_research_status, wt.deep_research_progress,
                    (SELECT COUNT(*) FROM watch_topic_articles WHERE topic_id = wt.topic_id AND tenant_id = wt.tenant_id),
                    (SELECT COUNT(*) FROM watch_topic_entities WHERE topic_id = wt.topic_id AND tenant_id = wt.tenant_id),
                    (SELECT COUNT(*) FROM watch_topic_clusters WHERE topic_id = wt.topic_id AND tenant_id = wt.tenant_id),
                    (SELECT COUNT(*) FROM watch_topic_entities WHERE topic_id = wt.topic_id AND tenant_id = wt.tenant_id
                        AND entity_type IN ('location','country','city','region','place','facility'))
                FROM watch_topics wt WHERE wt.topic_id = %s AND wt.tenant_id = %s
            """, (topic_id, auth.tenant_id))
            r = cur.fetchone()
            if not r:
                return JSONResponse(status_code=404, content={"error": "Topic not found"})
            sources = r[4]
            if isinstance(sources, str):
                try:
                    sources = json.loads(sources)
                except Exception:
                    sources = {}
            progress = r[13] or {}
            if isinstance(progress, str):
                try: progress = json.loads(progress)
                except Exception: progress = {}
            return {
                "topic_id": r[0], "title": r[1], "status": r[2], "keywords": r[3] or [],
                "sources": sources, "description": r[5], "backfill_months": r[6],
                "ai_summary": r[7], "summary_updated_at": r[8].isoformat() if r[8] else None,
                "created_at": r[9].isoformat() if r[9] else None,
                "updated_at": r[10].isoformat() if r[10] else None,
                "backfill_status": r[11] or "none",
                "deep_research_status": r[12] or "none",
                "deep_research_progress": progress,
                "article_count": r[14] or 0,
                "entity_count": r[15] or 0,
                "cluster_count": r[16] or 0,
                "location_count": r[17] or 0,
            }


@router.patch("/watch-topics/{topic_id}")
async def update_topic(topic_id: str, body: UpdateTopicRequest, auth: UnifiedAuthInfo = Depends(unified_auth)):
    updates = {}
    if body.title is not None: updates["title"] = body.title
    if body.keywords is not None: updates["keywords"] = body.keywords
    if body.sources is not None: updates["sources"] = json.dumps(body.sources)
    if body.status is not None: updates["status"] = body.status
    if body.description is not None: updates["description"] = body.description
    if not updates:
        return {"updated": False}
    updates["updated_at"] = datetime.now(timezone.utc)
    set_clause = ", ".join(f"{k} = %s" for k in updates)
    values = list(updates.values()) + [topic_id, auth.tenant_id]
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE watch_topics SET {set_clause} WHERE topic_id = %s AND tenant_id = %s", values)
            conn.commit()
    return {"updated": True}


@router.post("/watch-topics/{topic_id}/backfill")
async def trigger_backfill(topic_id: str, body: dict = {}, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Trigger historical backfill for a watch topic.

    Fetches articles from GDELT (3mo), HN (1yr), Reddit matching the topic's keywords.
    Runs in background — poll GET /watch-topics/{id} for backfill_progress.
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT keywords, sources, backfill_months, backfill_status FROM watch_topics WHERE topic_id = %s AND tenant_id = %s",
                (topic_id, auth.tenant_id),
            )
            row = cur.fetchone()
            if not row:
                return {"error": "Topic not found"}

            keywords = row[0] or []
            sources = json.loads(row[1]) if isinstance(row[1], str) else (row[1] or {})
            months = body.get("months", row[2] or 3)
            current_status = row[3]

            if current_status == "running":
                return {"status": "already_running"}

            # Mark as running
            cur.execute(
                "UPDATE watch_topics SET backfill_status = 'running', backfill_months = %s, updated_at = NOW() WHERE topic_id = %s AND tenant_id = %s",
                (months, topic_id, auth.tenant_id),
            )
            conn.commit()

    # Launch background backfill
    from core.research.backfill import start_backfill
    start_backfill(topic_id, keywords, sources, backfill_months=months, tenant_id=auth.tenant_id)

    return {"status": "started", "topic_id": topic_id, "keywords": keywords, "months": months}


@router.post("/watch-topics/{topic_id}/process")
async def trigger_processing(topic_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Process articles: cluster, extract entities, geocode. Run after backfill or manually."""
    from core.research.processor import process_topic
    import threading
    t = threading.Thread(target=process_topic, args=(topic_id, auth.tenant_id), daemon=True)
    t.start()
    return {"status": "started", "topic_id": topic_id}


@router.post("/watch-topics/{topic_id}/research")
async def trigger_deep_research(topic_id: str, body: dict = {}, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Mode 2 — user-triggered deep research.

    Phases (background thread):
      1. Historical backfill (GDELT/HN/Reddit) if include_backfill=True
      2. Processor: cluster, extract entities, geocode
      3. LLM synthesis: narrative per cluster (Gemma)
      4. Discovery expansion: spawn child knowledge_nodes for high-mention entities

    Poll GET /watch-topics/{id}/backfill/progress for progress.
    Body: {"include_backfill": bool, "backfill_months": int}
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM watch_topics WHERE topic_id = %s AND tenant_id = %s",
                (topic_id, auth.tenant_id),
            )
            if not cur.fetchone():
                return JSONResponse(status_code=404, content={"error": "Topic not found"})

    include_backfill = bool(body.get("include_backfill", True))
    backfill_months = int(body.get("backfill_months", 3))

    from core.research.deep_research import start_deep_research
    start_deep_research(
        topic_id,
        tenant_id=auth.tenant_id,
        include_backfill=include_backfill,
        backfill_months=backfill_months,
    )
    return {"status": "started", "topic_id": topic_id, "include_backfill": include_backfill, "backfill_months": backfill_months}


@router.get("/watch-topics/{topic_id}/backfill/progress")
async def get_backfill_progress(topic_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Get current backfill progress for a topic."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT backfill_status, backfill_progress FROM watch_topics WHERE topic_id = %s AND tenant_id = %s",
                (topic_id, auth.tenant_id),
            )
            row = cur.fetchone()
            if not row:
                return {"error": "Topic not found"}

            progress = row[1]
            if isinstance(progress, str):
                try:
                    progress = json.loads(progress)
                except (json.JSONDecodeError, TypeError):
                    progress = {}

            return {"status": row[0] or "none", "progress": progress or {}}


# ─── Deep Research (slow-burn self-expanding entity research) ────────────────

@router.post("/watch-topics/{topic_id}/deep-research")
async def start_deep_research(
    topic_id: str,
    body: dict = {},
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Trigger deep research for a watch topic.

    Queues all entities with mention_count >= min_mentions as seed research
    jobs. The research governor processes them one at a time in the background,
    discovers connections, and queues those connections at depth+1.

    Body (all optional):
      min_mentions: int = 3
      max_depth: int = 3
      max_jobs: int = 500
    """
    from core.research.governor import queue_seed_entities
    result = queue_seed_entities(
        topic_id, auth.tenant_id,
        min_mentions=body.get("min_mentions", 3),
        max_depth=body.get("max_depth", 3),
        max_jobs=body.get("max_jobs", 500),
    )
    return {"status": "queued", **result}


@router.get("/watch-topics/{topic_id}/deep-research/status")
async def get_deep_research_status(
    topic_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Return deep research progress for a topic."""
    from core.research.governor import get_research_status
    return get_research_status(topic_id, auth.tenant_id)


@router.post("/watch-topics/{topic_id}/deep-research/pause")
async def pause_deep_research(
    topic_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Pause deep research for a topic (governor skips paused topics)."""
    from core.research.governor import pause_research
    pause_research(topic_id, auth.tenant_id)
    return {"status": "paused"}


@router.post("/watch-topics/{topic_id}/deep-research/resume")
async def resume_deep_research(
    topic_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Resume paused deep research for a topic."""
    from core.research.governor import resume_research
    resume_research(topic_id, auth.tenant_id)
    return {"status": "running"}


@router.delete("/watch-topics/{topic_id}")
async def delete_topic(topic_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM watch_topic_articles WHERE topic_id = %s AND tenant_id = %s", (topic_id, auth.tenant_id))
            cur.execute("DELETE FROM watch_topic_clusters WHERE topic_id = %s AND tenant_id = %s", (topic_id, auth.tenant_id))
            cur.execute("DELETE FROM watch_topic_projects WHERE topic_id = %s AND tenant_id = %s", (topic_id, auth.tenant_id))
            cur.execute("DELETE FROM watch_topic_cases WHERE topic_id = %s AND tenant_id = %s", (topic_id, auth.tenant_id))
            cur.execute("DELETE FROM watch_topics WHERE topic_id = %s AND tenant_id = %s", (topic_id, auth.tenant_id))
            conn.commit()
    return {"deleted": True}


@router.get("/watch-topics/{topic_id}/clusters")
async def list_clusters(topic_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT cluster_id, headline, event_type, event_date, summary, source_count, sentiment,
                    key_claims, conflicts, people, locations, organizations
                FROM watch_topic_clusters WHERE topic_id = %s AND tenant_id = %s ORDER BY event_date DESC
            """, (topic_id, auth.tenant_id))
            clusters = [{"cluster_id": r[0], "headline": r[1], "event_type": r[2],
                         "event_date": r[3].isoformat() if r[3] else None, "summary": r[4],
                         "source_count": r[5], "sentiment": r[6],
                         "key_claims": r[7] or [], "conflicts": r[8] or [],
                         "people": r[9] or [], "locations": r[10] or [],
                         "organizations": r[11] or []} for r in cur.fetchall()]
    return {"clusters": clusters}


@router.get("/watch-topics/{topic_id}/articles")
async def list_articles(topic_id: str, auth: UnifiedAuthInfo = Depends(unified_auth),
                        cluster_id: Optional[str] = Query(None),
                        source: Optional[str] = Query(None),
                        entity: Optional[str] = Query(None)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            where = "topic_id = %s AND tenant_id = %s"
            params: list = [topic_id, auth.tenant_id]

            if cluster_id:
                where += " AND cluster_id = %s"
                params.append(cluster_id)
            if source:
                where += " AND LOWER(source_name) = LOWER(%s)"
                params.append(source)

            cur.execute(f"""
                SELECT article_id, title, url, source_name, author, word_count, published_at,
                       cluster_id, extraction, processing_status
                FROM watch_topic_articles WHERE {where}
                ORDER BY published_at DESC LIMIT 1000
            """, params)
            articles = []
            for r in cur.fetchall():
                extraction = r[8] or {}
                if isinstance(extraction, str):
                    try:
                        extraction = json.loads(extraction)
                    except Exception:
                        extraction = {}

                # Extract from envelope structure (nested) or flat processor format
                summary = extraction.get("summary") or {}
                source_info = extraction.get("source") or {}
                identity = extraction.get("identity") or {}
                media = extraction.get("media") or {}

                art = {
                    "article_id": r[0], "title": r[1], "url": r[2], "source_name": r[3],
                    "author": r[4], "word_count": r[5],
                    "published_at": r[6].isoformat() if r[6] else None,
                    "cluster_id": r[7],
                    "people": extraction.get("people", []),
                    "organizations": extraction.get("organizations", []),
                    "locations": extraction.get("locations", []),
                    "countries": extraction.get("countries", []),
                    "addresses": extraction.get("addresses", []),
                    "language": extraction.get("language") or source_info.get("language_detected"),
                    "title_original": extraction.get("title_original") or identity.get("title_original"),
                    "title_translated": extraction.get("title_translated") or identity.get("title_translated"),
                    "sentiment": extraction.get("sentiment"),
                    "processing_status": r[9],
                    # Enriched fields from deep pass
                    "key_facts": summary.get("key_facts", []),
                    "narrative": summary.get("narrative", ""),
                    "headline": summary.get("headline", ""),
                    "image_url": media.get("og_image") or extraction.get("thumbnail_url") or extraction.get("image_url"),
                    "thumbnail_url": media.get("thumbnail_url") or extraction.get("thumbnail_url"),
                }
                articles.append(art)

            # If entity filter, post-filter by entity name in extracted data
            if entity:
                entity_lower = entity.lower()
                articles = [a for a in articles if
                    any(entity_lower in str(p).lower() for p in a.get("people", [])) or
                    any(entity_lower in str(o).lower() for o in a.get("organizations", [])) or
                    any(entity_lower in str(l).lower() for l in a.get("locations", [])) or
                    any(entity_lower in str(c).lower() for c in a.get("countries", []))
                ]

            # Compute filter facets
            sources = {}
            people_facet = {}
            org_facet = {}
            country_facet = {}
            for a in articles:
                sn = a.get("source_name") or "Unknown"
                sources[sn] = sources.get(sn, 0) + 1
                for p in a.get("people", []):
                    name = p if isinstance(p, str) else p.get("name", str(p))
                    people_facet[name] = people_facet.get(name, 0) + 1
                for o in a.get("organizations", []):
                    name = o if isinstance(o, str) else o.get("name", str(o))
                    org_facet[name] = org_facet.get(name, 0) + 1
                for c in a.get("countries", []):
                    name = c if isinstance(c, str) else c.get("name", str(c))
                    country_facet[name] = country_facet.get(name, 0) + 1

            facets = {
                "sources": sorted(sources.items(), key=lambda x: -x[1]),
                "people": sorted(people_facet.items(), key=lambda x: -x[1])[:20],
                "organizations": sorted(org_facet.items(), key=lambda x: -x[1])[:20],
                "countries": sorted(country_facet.items(), key=lambda x: -x[1])[:20],
            }

    return {"articles": articles, "facets": facets, "total": len(articles)}


@router.get("/watch-topics/{topic_id}/entities")
async def list_entities(topic_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Return extracted entities, with canonical dossier data if available."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            # Try the full query with canonical entity join; fall back if columns don't exist yet.
            # Top-level entity_type uses the VALIDATED canonical type when the entity has been
            # resolved — the cheap extractor's default (everything=person) would otherwise poison
            # every filter bucket in the UI.
            try:
                cur.execute("""
                    SELECT wte.entity_id,
                           COALESCE(e.canonical_type, wte.entity_type) AS effective_type,
                           wte.name, wte.mention_count,
                           wte.first_seen, wte.last_seen, wte.latitude, wte.longitude,
                           wte.article_ids, wte.properties,
                           wte.canonical_entity_id, wte.resolution_status, wte.resolution_confidence,
                           e.canonical_name, e.aliases, e.completeness_score, e.completeness_tier,
                           e.identifiers, e.signal_count, e.gdu_canonical,
                           e.validation_status, e.validated_by_source, e.validation_source_id,
                           e.validation_source_url, e.validation_confidence
                    FROM watch_topic_entities wte
                    LEFT JOIN entities e ON e.entity_id = wte.canonical_entity_id
                    WHERE wte.topic_id = %s AND wte.tenant_id = %s
                    ORDER BY wte.mention_count DESC LIMIT 100
                """, (topic_id, auth.tenant_id))
                entities = []
                for r in cur.fetchall():
                    ent = {
                        "entity_id": r[0], "entity_type": r[1], "name": r[2],
                        "mention_count": r[3],
                        "first_seen": r[4].isoformat() if r[4] else None,
                        "last_seen": r[5].isoformat() if r[5] else None,
                        "latitude": r[6], "longitude": r[7],
                        "article_ids": r[8] or [], "properties": r[9] or {},
                        "canonical_entity_id": r[10],
                        "resolution_status": r[11],
                        "resolution_confidence": float(r[12]) if r[12] else None,
                    }
                    if r[10]:
                        ent["dossier"] = {
                            "canonical_name": r[13],
                            "aliases": r[14] or [],
                            "completeness_score": float(r[15]) if r[15] is not None else None,
                            "completeness_tier": r[16],
                            "identifiers": r[17] or {},
                            "signal_count": r[18] or 0,
                            "gdu_canonical": r[19],
                        }
                        # Phase 5 validation provenance — UI renders the badge
                        ent["validation"] = {
                            "status":     r[20],
                            "source":     r[21],
                            "source_id":  r[22],
                            "source_url": r[23],
                            "confidence": float(r[24]) if r[24] else None,
                        }
                    entities.append(ent)
            except Exception:
                conn.rollback()
                cur.execute("""
                    SELECT entity_id, entity_type, name, mention_count, first_seen, last_seen,
                           latitude, longitude, article_ids, properties
                    FROM watch_topic_entities
                    WHERE topic_id = %s AND tenant_id = %s
                    ORDER BY mention_count DESC LIMIT 100
                """, (topic_id, auth.tenant_id))
                entities = [{"entity_id": r[0], "entity_type": r[1], "name": r[2],
                             "mention_count": r[3],
                             "first_seen": r[4].isoformat() if r[4] else None,
                             "last_seen": r[5].isoformat() if r[5] else None,
                             "latitude": r[6], "longitude": r[7],
                             "article_ids": r[8] or [], "properties": r[9] or {}} for r in cur.fetchall()]
    return {"entities": entities}


@router.get("/watch-topics/{topic_id}/graph")
async def get_topic_graph(topic_id: str, auth: UnifiedAuthInfo = Depends(unified_auth),
                          focal: Optional[str] = Query(None)):
    """Return entity relationship graph with canonical dossier data.
    GROUPS BY canonical_entity_id to collapse duplicates. Filters fragments."""
    from core.research.entity_quality import is_quality_entity

    with _get_conn() as conn:
        with conn.cursor() as cur:
            # GROUPED query: one row per canonical entity, summing mentions across variants
            try:
                cur.execute("""
                    WITH grouped AS (
                        SELECT
                            COALESCE(wte.canonical_entity_id::text, 'raw_' || wte.entity_id) as group_key,
                            wte.canonical_entity_id,
                            MIN(wte.entity_id) as fallback_id,
                            COALESCE(MIN(e.canonical_type), MIN(wte.entity_type)) as entity_type,
                            COALESCE(MIN(e.canonical_name), MIN(wte.name)) as display_name,
                            SUM(wte.mention_count) as total_mentions,
                            (array_agg(wte.latitude) FILTER (WHERE wte.latitude IS NOT NULL))[1] as latitude,
                            (array_agg(wte.longitude) FILTER (WHERE wte.longitude IS NOT NULL))[1] as longitude,
                            array_agg(DISTINCT a) as article_ids
                        FROM watch_topic_entities wte
                        LEFT JOIN entities e ON e.entity_id = wte.canonical_entity_id
                        LEFT JOIN LATERAL unnest(wte.article_ids) AS a ON TRUE
                        WHERE wte.topic_id = %s AND wte.tenant_id = %s
                        GROUP BY group_key, wte.canonical_entity_id
                    )
                    SELECT g.fallback_id, g.entity_type, g.display_name, g.total_mentions,
                           g.latitude, g.longitude, g.article_ids,
                           g.canonical_entity_id,
                           CASE WHEN g.canonical_entity_id IS NULL THEN 'unresolved' ELSE 'resolved' END,
                           e.completeness_tier, e.completeness_score, e.aliases,
                           e.identifiers, e.canonical_name, e.conflict_count
                    FROM grouped g
                    LEFT JOIN entities e ON e.entity_id = g.canonical_entity_id
                    WHERE g.total_mentions >= 2  -- minimum mention threshold
                    ORDER BY g.total_mentions DESC LIMIT 80
                """, (topic_id, auth.tenant_id))
                nodes = []
                for r in cur.fetchall():
                    # Quality filter: skip fragments even if they made it past extraction
                    name = r[13] or r[2]
                    valid, reason = is_quality_entity(name, r[1] or "unknown")
                    if not valid:
                        continue
                    identifiers = r[12] or {}
                    if isinstance(identifiers, str):
                        try:
                            identifiers = json.loads(identifiers)
                        except Exception:
                            identifiers = {}

                    node = {
                        "id": r[0], "title": r[13] or r[2], "entity_type": r[1],
                        "mention_count": r[3], "latitude": r[4], "longitude": r[5],
                        "article_ids": r[6] or [],
                        "canonical_entity_id": r[7],
                        "resolution_status": r[8] or "unresolved",
                        # NULL completeness propagates as null — an unscored node
                        # is not a stub scored at zero. See feed_facets.py.
                        "completeness_tier": r[9],
                        "completeness_score": float(r[10]) if r[10] is not None else None,
                        "aliases": r[11] or [],
                        "conflict_count": r[14] or 0,
                        "dossier": {
                            k: v for k, v in {
                                "current_role": identifiers.get("current_role"),
                                "nationality": identifiers.get("nationality"),
                                "organization": identifiers.get("organization"),
                                "date_of_birth": identifiers.get("date_of_birth"),
                                "population": identifiers.get("population"),
                                "capital": identifiers.get("capital"),
                                "head_of_state": identifiers.get("head_of_state"),
                                "head_of_government": identifiers.get("head_of_government"),
                                "currency": identifiers.get("currency"),
                                "cik": identifiers.get("cik"),
                                "twitter": identifiers.get("twitter"),
                            }.items() if v is not None
                        },
                    }
                    nodes.append(node)
            except Exception:
                conn.rollback()
                cur.execute("""
                    SELECT entity_id, entity_type, name, mention_count,
                           latitude, longitude, article_ids
                    FROM watch_topic_entities
                    WHERE topic_id = %s AND tenant_id = %s
                    ORDER BY mention_count DESC LIMIT 60
                """, (topic_id, auth.tenant_id))
                nodes = [{
                    "id": r[0], "title": r[2], "entity_type": r[1],
                    "mention_count": r[3], "latitude": r[4], "longitude": r[5],
                    "article_ids": r[6] or [],
                } for r in cur.fetchall()]

            # Build edges from co-occurrence + knowledge graph edges
            edges = []
            for i, n1 in enumerate(nodes):
                a1 = set(n1["article_ids"])
                for j in range(i + 1, len(nodes)):
                    n2 = nodes[j]
                    a2 = set(n2["article_ids"])
                    shared = a1 & a2
                    if len(shared) >= 1:
                        edges.append({
                            "source": n1["id"], "target": n2["id"],
                            "relationship": "co-occurs",
                            "strength": min(len(shared) / 5, 1.0),
                            "shared_articles": len(shared),
                        })

            # Add knowledge graph edges + conflict data (may fail if migration not applied)
            canonical_ids = [n.get("canonical_entity_id") for n in nodes if n.get("canonical_entity_id")]
            if canonical_ids:
                try:
                    canonical_to_topic = {n["canonical_entity_id"]: n["id"]
                                          for n in nodes if n.get("canonical_entity_id")}
                    cur.execute("""
                        SELECT DISTINCT kn1.master_entity_id, kn2.master_entity_id,
                               ke.relationship, ke.evidence
                        FROM knowledge_edges ke
                        JOIN knowledge_nodes kn1 ON kn1.node_id = ke.source_node_id
                        JOIN knowledge_nodes kn2 ON kn2.node_id = ke.target_node_id
                        WHERE kn1.master_entity_id = ANY(%s)
                          AND kn2.master_entity_id = ANY(%s)
                          AND kn1.master_entity_id != kn2.master_entity_id
                        LIMIT 100
                    """, (canonical_ids, canonical_ids))
                    for r in cur.fetchall():
                        src_topic = canonical_to_topic.get(r[0])
                        tgt_topic = canonical_to_topic.get(r[1])
                        if src_topic and tgt_topic:
                            edge_key = f"{src_topic}|{tgt_topic}|{r[2]}"
                            if not any(f"{e['source']}|{e['target']}|{e['relationship']}" == edge_key for e in edges):
                                edges.append({
                                    "source": src_topic, "target": tgt_topic,
                                    "relationship": r[2], "evidence": r[3], "strength": 0.8,
                                })

                    # Conflict counts
                    cur.execute("""
                        SELECT kn.master_entity_id, COUNT(*)
                        FROM knowledge_nodes kn
                        WHERE kn.master_entity_id = ANY(%s) AND kn.tenant_id = %s
                          AND kn.epistemic_type = 'claim'
                        GROUP BY kn.master_entity_id
                    """, (canonical_ids, auth.tenant_id))
                    conflict_counts = dict(cur.fetchall())
                    for node in nodes:
                        cid = node.get("canonical_entity_id")
                        if cid and cid in conflict_counts:
                            node["conflict_count"] = conflict_counts[cid]
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass

    return {"nodes": nodes, "edges": edges}


@router.get("/watch-topics/{topic_id}/timeline")
async def get_topic_timeline(
    topic_id: str,
    limit: int = 365,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Topic-scoped timeline with daily buckets from articles, clusters,
    research discoveries, and entity relationship dates."""
    from core.research.timeline import assemble_topic_timeline
    days = assemble_topic_timeline(topic_id, auth.tenant_id, limit=limit)
    return {"days": days, "count": len(days)}


# ─── Promote ─────────────────────────────────────────────────────────────────

@router.post("/watch-topics/{topic_id}/promote/project")
async def promote_to_project(topic_id: str, body: dict = {}, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT title, description FROM watch_topics WHERE topic_id = %s AND tenant_id = %s", (topic_id, auth.tenant_id))
            topic = cur.fetchone()
            if not topic:
                return JSONResponse(status_code=404, content={"error": "Topic not found"})
            project_id = f"proj_{uuid.uuid4().hex[:12]}"
            now = datetime.now(timezone.utc)
            cur.execute("""
                INSERT INTO projects (project_id, tenant_id, name, description, status, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            """, (project_id, auth.tenant_id, body.get("title", topic[0]), body.get("description", topic[1]), "active", now, now))
            cur.execute("""
                INSERT INTO watch_topic_projects (topic_id, project_id, tenant_id, auto_sync, linked_at)
                VALUES (%s,%s,%s,%s,%s)
            """, (topic_id, project_id, auth.tenant_id, True, now))
            conn.commit()
    return JSONResponse(status_code=201, content={"project_id": project_id})


@router.post("/watch-topics/{topic_id}/promote/case")
async def promote_to_case(topic_id: str, body: dict = {}, auth: UnifiedAuthInfo = Depends(unified_auth)):
    from storage.signals.cases import create_case
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT title, description FROM watch_topics WHERE topic_id = %s AND tenant_id = %s", (topic_id, auth.tenant_id))
            topic = cur.fetchone()
            if not topic:
                return JSONResponse(status_code=404, content={"error": "Topic not found"})
    case = create_case(tenant_id=auth.tenant_id, title=body.get("title", topic[0]),
                       description=body.get("description", topic[1]), priority="medium")
    case_id = case.get("case_id")
    if case_id:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO watch_topic_cases (topic_id, case_id, tenant_id, auto_sync, linked_at)
                    VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
                """, (topic_id, case_id, auth.tenant_id, True, datetime.now(timezone.utc)))
                conn.commit()
    return JSONResponse(status_code=201, content={"case_id": case_id})


# ─── Project Linking ─────────────────────────────────────────────────────────

@router.get("/watch-topics/{topic_id}/projects")
async def list_linked_projects(topic_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT wtp.project_id, p.name, p.status, wtp.auto_sync, wtp.linked_at
                FROM watch_topic_projects wtp
                JOIN projects p ON p.project_id = wtp.project_id AND p.tenant_id = wtp.tenant_id
                WHERE wtp.topic_id = %s AND wtp.tenant_id = %s
            """, (topic_id, auth.tenant_id))
            projects = [{"project_id": r[0], "name": r[1], "status": r[2], "auto_sync": r[3],
                         "linked_at": r[4].isoformat() if r[4] else None} for r in cur.fetchall()]
    return {"projects": projects}


@router.post("/watch-topics/{topic_id}/projects")
async def link_project(topic_id: str, body: LinkProjectRequest, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO watch_topic_projects (topic_id, project_id, tenant_id, auto_sync, linked_at)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
            """, (topic_id, body.project_id, auth.tenant_id, True, datetime.now(timezone.utc)))
            conn.commit()
    return JSONResponse(status_code=201, content={"linked": True})


@router.delete("/watch-topics/{topic_id}/projects/{project_id}")
async def unlink_project(topic_id: str, project_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM watch_topic_projects WHERE topic_id = %s AND project_id = %s AND tenant_id = %s",
                       (topic_id, project_id, auth.tenant_id))
            conn.commit()
    return {"unlinked": True}


@router.patch("/watch-topics/{topic_id}/projects/{project_id}")
async def update_project_sync(topic_id: str, project_id: str, body: SyncUpdateRequest, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE watch_topic_projects SET auto_sync = %s WHERE topic_id = %s AND project_id = %s AND tenant_id = %s",
                       (body.auto_sync, topic_id, project_id, auth.tenant_id))
            conn.commit()
    return {"updated": True}


# ─── Case Linking ────────────────────────────────────────────────────────────

@router.get("/watch-topics/{topic_id}/cases")
async def list_linked_cases(topic_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT wtc.case_id, c.title, c.status, wtc.auto_sync, wtc.linked_at
                FROM watch_topic_cases wtc
                JOIN cases c ON c.case_id = wtc.case_id AND c.tenant_id = wtc.tenant_id
                WHERE wtc.topic_id = %s AND wtc.tenant_id = %s
            """, (topic_id, auth.tenant_id))
            cases = [{"case_id": r[0], "title": r[1], "status": r[2], "auto_sync": r[3],
                      "linked_at": r[4].isoformat() if r[4] else None} for r in cur.fetchall()]
    return {"cases": cases}


@router.post("/watch-topics/{topic_id}/cases")
async def link_case(topic_id: str, body: LinkCaseRequest, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO watch_topic_cases (topic_id, case_id, tenant_id, auto_sync, linked_at)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
            """, (topic_id, body.case_id, auth.tenant_id, True, datetime.now(timezone.utc)))
            conn.commit()
    return JSONResponse(status_code=201, content={"linked": True})


@router.delete("/watch-topics/{topic_id}/cases/{case_id}")
async def unlink_case(topic_id: str, case_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM watch_topic_cases WHERE topic_id = %s AND case_id = %s AND tenant_id = %s",
                       (topic_id, case_id, auth.tenant_id))
            conn.commit()
    return {"unlinked": True}


@router.patch("/watch-topics/{topic_id}/cases/{case_id}")
async def update_case_sync(topic_id: str, case_id: str, body: SyncUpdateRequest, auth: UnifiedAuthInfo = Depends(unified_auth)):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE watch_topic_cases SET auto_sync = %s WHERE topic_id = %s AND case_id = %s AND tenant_id = %s",
                       (body.auto_sync, topic_id, case_id, auth.tenant_id))
            conn.commit()
    return {"updated": True}


# ─── Entity Promotion ────────────────────────────────────────────────────────

@router.post("/watch-topics/{topic_id}/entities/{entity_id}/promote")
async def promote_entity_to_contact(topic_id: str, entity_id: str, body: dict = {},
                                     auth: UnifiedAuthInfo = Depends(unified_auth)):
    contact_id = f"contact_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    name = body.get("full_name", body.get("name", entity_id))
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO contacts (contact_id, tenant_id, contact_type, primary_name, status,
                    metadata, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """, (contact_id, auth.tenant_id, "person", name, "active", json.dumps(body), now, now))
            conn.commit()
    return JSONResponse(status_code=201, content={"contact_id": contact_id, "primary_name": name})


# ─── Entity Resolution & Merge/Split ────────────────────────────────────────

@router.post("/entities/merge")
async def merge_entities_endpoint(body: dict, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Merge multiple entities into one canonical winner."""
    winner_id = body.get("winner_id")
    loser_ids = body.get("loser_ids", [])
    reason = body.get("reason", "manual merge")

    if not winner_id or not loser_ids:
        return JSONResponse(status_code=400, content={"error": "winner_id and loser_ids required"})

    from core.gdu.entity_resolution import merge_entities
    result = merge_entities(
        winner_id=winner_id, loser_ids=loser_ids,
        tenant_id=auth.tenant_id, merged_by="user",
        reason=reason,
    )
    return result


@router.post("/entities/split")
async def split_entity_endpoint(body: dict, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Split an entity into two, moving specified edges to the new entity."""
    entity_id = body.get("entity_id")
    new_name = body.get("new_name")
    new_type = body.get("new_type")

    if not entity_id or not new_name:
        return JSONResponse(status_code=400, content={"error": "entity_id and new_name required"})

    from core.gdu.entity_resolution import resolve_entity
    result = resolve_entity(
        tenant_id=auth.tenant_id,
        name=new_name,
        entity_type=new_type or "unknown",
        source_pipeline="manual_split",
    )
    return {
        "original_entity_id": entity_id,
        "new_entity_id": result.entity_id,
        "action": result.action,
    }


@router.get("/entities/{entity_id}/duplicates")
async def find_entity_duplicates(entity_id: int, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Find potential duplicate entities for dedup review."""
    from services.api.src._entity_redirect import resolve_entity_or_410
    with _get_conn() as conn:
        # Don't surface duplicates of a reclassified row — the dossier
        # has been superseded; the UI should be sent to the new substrate.
        entity_id = resolve_entity_or_410(conn, entity_id)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT canonical_name, canonical_type, embedding, aliases
                FROM entities WHERE entity_id = %s AND tenant_id = %s
            """, (entity_id, auth.tenant_id))
            row = cur.fetchone()
            if not row:
                return JSONResponse(status_code=404, content={"error": "Entity not found"})

            name, etype, embedding, aliases = row

            candidates = []
            if embedding:
                cur.execute("""
                    SELECT entity_id, canonical_name, canonical_type, aliases,
                           signal_count, completeness_tier,
                           embedding <-> %s::vector as distance
                    FROM entities
                    WHERE tenant_id = %s AND entity_id != %s AND canonical_type = %s
                      AND embedding IS NOT NULL
                    ORDER BY embedding <-> %s::vector
                    LIMIT 10
                """, (embedding, auth.tenant_id, entity_id, etype, embedding))
                for r in cur.fetchall():
                    candidates.append({
                        "entity_id": r[0], "canonical_name": r[1],
                        "canonical_type": r[2], "aliases": r[3] or [],
                        "signal_count": r[4], "completeness_tier": r[5],
                        "distance": round(float(r[6]), 4),
                    })

    return {"entity_id": entity_id, "name": name, "candidates": candidates}


@router.get("/entities/{entity_id}/dossier")
async def get_entity_dossier(entity_id: int, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Get full canonical entity dossier with linked knowledge nodes."""
    from services.api.src._entity_redirect import resolve_entity_or_410
    with _get_conn() as conn:
        # Surface cross-category reclassifications as 410 so the UI
        # redirects the user to the right substrate (place/topic/event)
        # instead of rendering a ghost dossier.
        entity_id = resolve_entity_or_410(conn, entity_id)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT entity_id, canonical_type, canonical_name, normalized_name,
                       aliases, identifiers, completeness_score, completeness_tier,
                       first_seen, last_seen, signal_count, gdu_canonical,
                       admitted_under_active_set
                FROM entities WHERE entity_id = %s AND tenant_id = %s
            """, (entity_id, auth.tenant_id))
            row = cur.fetchone()
            if not row:
                return JSONResponse(status_code=404, content={"error": "Entity not found"})

            entity = {
                "entity_id": row[0], "canonical_type": row[1],
                "canonical_name": row[2], "normalized_name": row[3],
                "aliases": row[4] or [], "identifiers": row[5] or {},
                "completeness_score": float(row[6]) if row[6] is not None else None,
                "completeness_tier": row[7],
                "first_seen": row[8].isoformat() if row[8] else None,
                "last_seen": row[9].isoformat() if row[9] else None,
                "signal_count": row[10] or 0,
                "gdu_canonical": row[11],
                "active_set_version": row[12],
            }

            # Get linked knowledge nodes
            cur.execute("""
                SELECT node_id, title, description, epistemic_type, confidence,
                       entity_name, event_time, place_name, source_count,
                       enrichment_status
                FROM knowledge_nodes
                WHERE master_entity_id = %s AND tenant_id = %s
                ORDER BY created_at DESC LIMIT 50
            """, (entity_id, auth.tenant_id))
            entity["knowledge_nodes"] = [{
                "node_id": r[0], "title": r[1], "description": r[2],
                "epistemic_type": r[3], "confidence": float(r[4]) if r[4] else None,
                "entity_name": r[5], "event_time": r[6].isoformat() if r[6] else None,
                "place_name": r[7], "source_count": r[8],
                "enrichment_status": r[9],
            } for r in cur.fetchall()]

            # Get watch topics this entity appears in
            cur.execute("""
                SELECT wte.topic_id, wt.title, wte.mention_count
                FROM watch_topic_entities wte
                JOIN watch_topics wt ON wt.topic_id = wte.topic_id
                WHERE wte.canonical_entity_id = %s AND wte.tenant_id = %s
                ORDER BY wte.mention_count DESC
            """, (entity_id, auth.tenant_id))
            entity["topics"] = [{
                "topic_id": r[0], "title": r[1], "mention_count": r[2],
            } for r in cur.fetchall()]

            # Get enrichment queue status
            cur.execute("""
                SELECT enrichment_type, status, result, completed_at
                FROM entity_enrichment_queue
                WHERE entity_id = %s AND tenant_id = %s
                ORDER BY created_at DESC
            """, (entity_id, auth.tenant_id))
            entity["enrichment"] = [{
                "type": r[0], "status": r[1],
                "result": r[2] if r[1] == "complete" else None,
                "completed_at": r[3].isoformat() if r[3] else None,
            } for r in cur.fetchall()]

            # Get resolution history
            cur.execute("""
                SELECT candidate_name, action, confidence, match_reason,
                       resolution_method, source_pipeline, created_at
                FROM entity_resolution_log
                WHERE matched_entity_id = %s AND tenant_id = %s
                ORDER BY created_at DESC LIMIT 20
            """, (entity_id, auth.tenant_id))
            entity["resolution_history"] = [{
                "candidate_name": r[0], "action": r[1],
                "confidence": float(r[2]) if r[2] else None,
                "match_reason": r[3], "method": r[4],
                "pipeline": r[5],
                "timestamp": r[6].isoformat() if r[6] else None,
            } for r in cur.fetchall()]

    return entity


@router.get("/entities/review-queue")
async def entity_review_queue(auth: UnifiedAuthInfo = Depends(unified_auth),
                               limit: int = Query(20)):
    """Get entities flagged for dedup review (ambiguous resolution)."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT erl.log_id, erl.candidate_name, erl.candidate_type,
                       erl.matched_entity_id, erl.confidence, erl.match_reason,
                       erl.source_pipeline, erl.created_at,
                       e.canonical_name, e.aliases, e.completeness_tier
                FROM entity_resolution_log erl
                LEFT JOIN entities e ON e.entity_id = erl.matched_entity_id
                WHERE erl.tenant_id = %s AND erl.action = 'review'
                ORDER BY erl.created_at DESC LIMIT %s
            """, (auth.tenant_id, limit))
            items = [{
                "log_id": r[0], "candidate_name": r[1], "candidate_type": r[2],
                "matched_entity_id": r[3], "confidence": float(r[4]) if r[4] else None,
                "match_reason": r[5], "pipeline": r[6],
                "timestamp": r[7].isoformat() if r[7] else None,
                "existing_entity": {
                    "canonical_name": r[8], "aliases": r[9] or [],
                    "completeness_tier": r[10],
                } if r[8] else None,
            } for r in cur.fetchall()]
    return {"review_items": items, "count": len(items)}


@router.post("/entities/enrich/{entity_id}")
async def trigger_entity_enrichment(entity_id: int,
                                     auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Manually trigger enrichment for a specific entity."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT canonical_type FROM entities
                WHERE entity_id = %s AND tenant_id = %s
            """, (entity_id, auth.tenant_id))
            row = cur.fetchone()
            if not row:
                return JSONResponse(status_code=404, content={"error": "Entity not found"})

            from core.gdu.entity_resolution import _load_entityvocab
            _, enrichment_types, _, _ = _load_entityvocab()
            types_for_entity = enrichment_types.get("enrichment_types", {}).get(row[0], [])

            queued = 0
            for et in types_for_entity:
                cur.execute("""
                    INSERT INTO entity_enrichment_queue
                        (entity_id, tenant_id, enrichment_type, priority)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                """, (entity_id, auth.tenant_id, et["type"], et.get("priority", 5)))
                queued += 1

            conn.commit()

    return {"entity_id": entity_id, "enrichment_tasks_queued": queued}


@router.get("/entities/{entity_id}/enrichment")
async def list_entity_enrichment(entity_id: int,
                                 auth: UnifiedAuthInfo = Depends(unified_auth)):
    """List available enrichment types for an entity + most-recent run per type.

    Used by the dossier UI to render per-type "Run" buttons with last-run state.
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT canonical_type FROM entities
                WHERE entity_id = %s AND tenant_id = %s
            """, (entity_id, auth.tenant_id))
            row = cur.fetchone()
            if not row:
                return JSONResponse(status_code=404, content={"error": "Entity not found"})
            entity_type = row[0]

            from core.gdu.entity_resolution import _load_entityvocab
            _, enrichment_types, _, _ = _load_entityvocab()
            types_for_entity = enrichment_types.get("enrichment_types", {}).get(entity_type, [])

            cur.execute("""
                SELECT DISTINCT ON (enrichment_type)
                    enrichment_type, status, priority, attempt_count,
                    created_at, started_at, completed_at, error
                FROM entity_enrichment_queue
                WHERE entity_id = %s AND tenant_id = %s
                ORDER BY enrichment_type, created_at DESC
            """, (entity_id, auth.tenant_id))
            last_runs = {r[0]: {
                "status": r[1], "priority": r[2], "attempt_count": r[3],
                "created_at": r[4].isoformat() if r[4] else None,
                "started_at": r[5].isoformat() if r[5] else None,
                "completed_at": r[6].isoformat() if r[6] else None,
                "error": (r[7] or "")[:300] if r[7] else None,
            } for r in cur.fetchall()}

    available = [{
        "type": et["type"],
        "description": et.get("description", ""),
        "priority": et.get("priority", 5),
        "sources": et.get("sources", []),
        "last_run": last_runs.get(et["type"]),
    } for et in types_for_entity]

    return {
        "entity_id": entity_id,
        "entity_type": entity_type,
        "available": available,
    }


@router.post("/entities/{entity_id}/enrich/{enrichment_type}")
async def trigger_single_enrichment(entity_id: int, enrichment_type: str,
                                    auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Queue a single enrichment task for an entity.

    Operator-triggered from the dossier UI. Validates that the requested
    enrichment_type is declared in entityvocab for the entity's canonical_type.
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT canonical_type FROM entities
                WHERE entity_id = %s AND tenant_id = %s
            """, (entity_id, auth.tenant_id))
            row = cur.fetchone()
            if not row:
                return JSONResponse(status_code=404, content={"error": "Entity not found"})
            entity_type = row[0]

            from core.gdu.entity_resolution import _load_entityvocab
            _, enrichment_types, _, _ = _load_entityvocab()
            valid = enrichment_types.get("enrichment_types", {}).get(entity_type, [])
            match = next((et for et in valid if et["type"] == enrichment_type), None)
            if match is None:
                return JSONResponse(status_code=400, content={
                    "error": (f"enrichment_type '{enrichment_type}' is not valid for "
                              f"entity_type '{entity_type}'"),
                    "valid_types": [et["type"] for et in valid],
                })

            cur.execute("""
                INSERT INTO entity_enrichment_queue
                    (entity_id, tenant_id, enrichment_type, priority)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING queue_id
            """, (entity_id, auth.tenant_id, enrichment_type, match.get("priority", 5)))
            res = cur.fetchone()
            conn.commit()

    if res:
        return {"entity_id": entity_id, "enrichment_type": enrichment_type,
                "queue_id": res[0], "queued": True}
    return {"entity_id": entity_id, "enrichment_type": enrichment_type,
            "queued": False, "reason": "already pending or running"}


@router.post("/watch-topics/{topic_id}/cleanup")
async def cleanup_topic(topic_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Clean up topic entities: merge demonyms, expand abbreviations, remove news sources."""
    from core.extraction.entity_cleanup import cleanup_topic_entities
    result = cleanup_topic_entities(topic_id, auth.tenant_id)
    return {"topic_id": topic_id, **result}


@router.post("/watch-topics/{topic_id}/geocode")
async def geocode_topic_entities(topic_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Geocode all location entities in a topic that lack coordinates."""
    from core.extraction.geocoder import backfill_coordinates
    updated = backfill_coordinates(topic_id, auth.tenant_id)
    return {"topic_id": topic_id, "geocoded": updated}
