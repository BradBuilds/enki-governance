"""
Intelligence Feed — rich signal cards for the dashboard.

GET /feed — Returns signals with extracted headlines, summaries, thumbnails,
            coordinates, and action buttons based on signal type.

Each signal type has a card renderer that knows how to extract the most
useful fields from its payload. The frontend renders these as visual cards
with images, maps, engagement metrics, and one-click actions.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, Query
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

log = logging.getLogger("enki.feed")
router = APIRouter(prefix="/feed", tags=["feed"])


@router.get("")
async def get_feed(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    limit: int = Query(50, le=200),
    after: Optional[str] = None,
    before: Optional[str] = None,
    signal_type: Optional[str] = None,
):
    """Rich intelligence feed — signals rendered as visual cards.

    Supports cursor-based pagination via `before` parameter (ISO timestamp).
    Pass the oldest card's timestamp as `before` to load the next page.
    """
    from storage.postgres.db import get_connection

    now = datetime.now(timezone.utc)
    after_dt = datetime.fromisoformat(after) if after else now - timedelta(days=7)
    before_dt = datetime.fromisoformat(before) if before else now

    clauses = ["tenant_id = %s", "timestamp >= %s", "timestamp < %s"]
    params: list = [auth.tenant_id, after_dt, before_dt]

    # Exclude noise signals (raw telemetry, positions) from feed
    noise_types = [
        "signal.geospatial.aircraft_position",
        "signal.geospatial.vessel_position",
        "signal.geospatial.position_report",
        "signal.device.sensor_reading",
        "signal.infrastructure.sensor_reading",
    ]
    if not signal_type:
        for nt in noise_types:
            clauses.append("signal_type != %s")
            params.append(nt)

    # Exclude topic-specific articles — they belong in their topic, not the main feed
    # Topic-specific connectors stamp _topic_id in the signal metadata
    clauses.append("(metadata->>'_topic_id' IS NULL OR metadata->>'_topic_id' = '')")

    if signal_type:
        if signal_type.endswith(".*"):
            clauses.append("signal_type LIKE %s")
            params.append(signal_type.replace(".*", ".%"))
        else:
            clauses.append("signal_type = %s")
            params.append(signal_type)

    params.append(limit)

    cards = []
    rid_by_signal: dict[str, int] = {}
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT rid, signal_id, signal_type, severity, device_id,
                           timestamp, payload, latitude, longitude, metadata
                    FROM signals
                    WHERE {' AND '.join(clauses)}
                    ORDER BY timestamp DESC
                    LIMIT %s
                """, params)

                for r in cur.fetchall():
                    card = _render_card(
                        signal_id=r[1],
                        signal_type=r[2],
                        severity=r[3],
                        device_id=r[4],
                        timestamp=r[5],
                        payload=r[6] or {},
                        lat=r[7],
                        lon=r[8],
                        metadata=r[9] or {},
                    )
                    card["rid"] = r[0]
                    rid_by_signal[card["signal_id"]] = r[0]
                    cards.append(card)

    except Exception as e:
        log.error(f"Feed query failed: {e}")
        return {"sections": {}, "cards": [], "error": str(e)}

    # Sort by timestamp
    cards.sort(key=lambda c: c.get("timestamp") or "", reverse=True)
    cards = cards[:limit]

    rids = [c["rid"] for c in cards if c.get("rid") is not None]

    # Cross-reference with watch topics — tag each card with matched topic(s)
    if rids:
        try:
            with get_connection() as conn2:
                with conn2.cursor() as cur2:
                    cur2.execute("""
                        SELECT wta.signal_rid, wt.topic_id, wt.title
                        FROM watch_topic_articles wta
                        JOIN watch_topics wt ON wt.topic_id = wta.topic_id
                        WHERE wta.signal_rid = ANY(%s)
                    """, (rids,))
                    topic_map: dict[int, list] = {}
                    for r in cur2.fetchall():
                        rid = r[0]
                        if rid not in topic_map:
                            topic_map[rid] = []
                        topic_map[rid].append({"topic_id": r[1], "title": r[2]})

                    for card in cards:
                        card["topics"] = topic_map.get(card.get("rid"), [])
        except Exception:
            pass  # Non-fatal — feed works without topic tags

    # Cross-reference library_signals — server-side library chips on each card.
    # Populated by core/workers/library_router.py via keyword + entity matching.
    if rids:
        try:
            with get_connection() as conn_ls:
                with conn_ls.cursor() as cur_ls:
                    cur_ls.execute("""
                        SELECT ls.signal_rid, l.library_id, l.canonical_name,
                               l.focal_term, l.base60_address,
                               ls.match_kind, ls.match_score
                          FROM library_signals ls
                          JOIN libraries l ON l.library_id = ls.library_id
                         WHERE ls.signal_rid = ANY(%s)
                    """, (rids,))
                    lib_map: dict[int, list] = {}
                    for r in cur_ls.fetchall():
                        rid = r[0]
                        if rid not in lib_map:
                            lib_map[rid] = []
                        focal = r[3]
                        canonical = r[2]
                        title = focal if focal else (
                            canonical.split("/", 1)[1] if canonical and "/" in canonical else canonical
                        )
                        lib_map[rid].append({
                            "library_id": r[1],
                            "title": title,
                            "address": r[4],
                            "match_kind": r[5],
                            "score": r[6],
                        })
                    for card in cards:
                        card["matched_libraries"] = lib_map.get(card.get("rid"), [])
        except Exception as e:
            log.warning(f"library_signals join failed: {e}")

    # Cross-reference entity_mentions — split into subjects (who/what is in the
    # story) and sources (publishers being cited). The mass_media canonical_type
    # is the "source" axis; everything else is a subject.
    SOURCE_TYPES = {"mass_media", "publisher", "media_outlet"}
    if rids:
        try:
            with get_connection() as conn3:
                with conn3.cursor() as cur3:
                    cur3.execute("""
                        SELECT em.signal_rid, e.entity_id, e.canonical_name,
                               e.canonical_type, em.confidence
                        FROM entity_mentions em
                        JOIN entities e ON e.entity_id = em.entity_id
                        WHERE em.signal_rid = ANY(%s)
                          AND e.canonical_name IS NOT NULL
                          AND e.canonical_name != ''
                        ORDER BY em.signal_rid, em.confidence DESC NULLS LAST
                    """, (rids,))
                    by_rid: dict[int, dict] = {}
                    for r in cur3.fetchall():
                        sig_rid, ent_id, name, ctype, conf = r
                        if sig_rid not in by_rid:
                            by_rid[sig_rid] = {"subjects": [], "sources": []}
                        bucket = "sources" if (ctype or "").lower() in SOURCE_TYPES else "subjects"
                        # Dedup within bucket by entity_id
                        if not any(e["entity_id"] == ent_id for e in by_rid[sig_rid][bucket]):
                            by_rid[sig_rid][bucket].append({
                                "entity_id": ent_id,
                                "name": name,
                                "type": ctype or "unknown",
                            })
                    for card in cards:
                        ents = by_rid.get(card.get("rid"), {"subjects": [], "sources": []})
                        # Cap to keep payload small
                        card["entities"] = {
                            "subjects": ents["subjects"][:12],
                            "sources": ents["sources"][:4],
                        }
        except Exception as e:
            log.warning(f"entity_mentions join failed: {e}")
            for card in cards:
                card["entities"] = {"subjects": [], "sources": []}

    # Drop internal rid before returning
    for card in cards:
        card.pop("rid", None)

    # Group into sections by domain
    sections: dict[str, list] = {}
    domain_order = ["security", "content", "environmental", "financial", "geospatial", "infrastructure", "device"]
    for card in cards:
        domain = card.get("domain", "other")
        if domain not in sections:
            sections[domain] = []
        sections[domain].append(card)

    # Sort sections: domains with critical/high severity first, then by domain_order
    def section_priority(domain: str) -> int:
        sec_cards = sections.get(domain, [])
        has_critical = any(c.get("severity") in ("critical", "high") for c in sec_cards)
        base = domain_order.index(domain) if domain in domain_order else len(domain_order)
        return (0 if has_critical else 1, base)

    sorted_sections = {}
    for domain in sorted(sections.keys(), key=section_priority):
        sorted_sections[domain] = sections[domain]

    # Cursor for next page
    oldest_ts = cards[-1]["timestamp"] if cards else None

    return {
        "sections": sorted_sections,
        "cards": cards,
        "count": len(cards),
        "next_cursor": oldest_ts,
        "has_more": len(cards) == limit,
    }


# ─── Article clusters (deduped news feed) ──────────────────────────────

@router.get("/clusters")
def feed_clusters(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    hours: int = 24,
    limit: int = 50,
    min_sources: int = 1,
):
    """Deduped news feed: one row per cluster (story), not per article.
    Multiple sources reporting the same story collapse into a single
    cluster with `article_count` + `source_count` so frequency surfaces
    immediately. Sort: most-recently-updated first.
    """
    from storage.postgres.db import get_connection
    out: list[dict] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.cluster_id, c.topic_summary, c.first_seen, c.last_seen,
                       c.article_count, c.source_count, c.entity_ids,
                       c.title_keywords,
                       s.payload->>'title' AS canonical_title,
                       s.payload->>'source' AS canonical_source,
                       s.payload->>'link' AS canonical_link,
                       s.payload->>'thumbnail_url' AS thumbnail,
                       s.payload->>'summary' AS summary
                  FROM signal_clusters c
                  LEFT JOIN signals s ON s.rid = c.canonical_signal_rid
                 WHERE c.tenant_id = %s
                   AND c.last_seen > NOW() - INTERVAL '1 hour' * %s
                   AND c.source_count >= %s
                 ORDER BY c.last_seen DESC
                 LIMIT %s
                """,
                (auth.tenant_id, hours, min_sources, limit),
            )
            rows = cur.fetchall()

            # Resolve entity names in one query — gather all entity_ids,
            # look up canonical_name + type, then re-attach to each cluster.
            all_entity_ids: set[int] = set()
            for r in rows:
                for eid in (r[6] or []):
                    all_entity_ids.add(eid)
            entity_lookup: dict[int, dict] = {}
            if all_entity_ids:
                cur.execute(
                    """
                    SELECT entity_id, canonical_name, canonical_type, signal_count
                      FROM entities
                     WHERE entity_id = ANY(%s)
                       AND NOT (COALESCE(envelope, '{}'::jsonb) ? 'reclassification')
                    """,
                    (list(all_entity_ids),),
                )
                for er in cur.fetchall():
                    entity_lookup[er[0]] = {
                        "entity_id": er[0],
                        "name": er[1],
                        "type": er[2],
                        "signal_count": er[3] or 0,
                    }

            for r in rows:
                # Top 6 entities per cluster, ranked by signal_count
                cluster_ents = [
                    entity_lookup[eid] for eid in (r[6] or []) if eid in entity_lookup
                ]
                cluster_ents.sort(key=lambda e: -e["signal_count"])
                out.append({
                    "cluster_id": r[0],
                    "headline": r[8] or r[1],
                    "summary": (r[12] or "")[:280],
                    "canonical_source": r[9],
                    "canonical_link": r[10],
                    "thumbnail_url": r[11],
                    "first_seen": r[2].isoformat() if r[2] else None,
                    "last_seen": r[3].isoformat() if r[3] else None,
                    "article_count": r[4],
                    "source_count": r[5],
                    "entities": cluster_ents[:6],
                    "entity_ids": r[6] or [],
                    "keywords": (r[7] or [])[:10],
                })
    return {"clusters": out, "count": len(out)}


@router.get("/clusters/{cluster_id}")
def feed_cluster_detail(
    cluster_id: int,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """All articles in a cluster — for expanding to see every source that
    covered the story."""
    from storage.postgres.db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cluster_id, topic_summary, first_seen, last_seen,
                       article_count, source_count, entity_ids, title_keywords,
                       canonical_signal_rid
                  FROM signal_clusters WHERE cluster_id = %s AND tenant_id = %s
                """,
                (cluster_id, auth.tenant_id),
            )
            r = cur.fetchone()
            if not r:
                return {"error": "cluster not found"}
            cluster = {
                "cluster_id": r[0], "topic_summary": r[1],
                "first_seen": r[2].isoformat() if r[2] else None,
                "last_seen": r[3].isoformat() if r[3] else None,
                "article_count": r[4], "source_count": r[5],
                "entity_ids": r[6] or [], "keywords": r[7] or [],
                "canonical_signal_rid": r[8],
            }
            cur.execute(
                """
                SELECT rid, timestamp,
                       payload->>'title', payload->>'source',
                       payload->>'link', payload->>'summary'
                  FROM signals s
                  JOIN signal_cluster_memberships m ON m.signal_rid = s.rid
                 WHERE m.cluster_id = %s
                 ORDER BY s.timestamp DESC
                """,
                (cluster_id,),
            )
            articles = [
                {"signal_rid": a[0],
                 "timestamp": a[1].isoformat() if a[1] else None,
                 "title": a[2], "source": a[3], "link": a[4],
                 "summary": (a[5] or "")[:280]}
                for a in cur.fetchall()
            ]
    return {"cluster": cluster, "articles": articles}


# ─── Heatmap (article-locations) ──────────────────────────────────────────

@router.get("/heatmap")
async def get_heatmap(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    hours: int = Query(24, ge=1, le=168),
    min_articles: int = Query(1, ge=1, le=20),
):
    """Geographic heat of where the news IS — aggregated from entity_mentions
    of geographic entities (country / city / region / facility) extracted
    from RSS article text. Locations are article-accurate, NOT publisher-
    HQ-accurate: a Reuters article about Tehran resolves to Tehran.
    """
    from storage.postgres.db import get_connection

    points: list[dict] = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT e.entity_id, e.canonical_name, e.canonical_type,
                           AVG(em.latitude)::float AS lat,
                           AVG(em.longitude)::float AS lon,
                           COUNT(DISTINCT em.signal_rid) AS article_count,
                           COUNT(*) AS mention_count
                      FROM entity_mentions em
                      JOIN entities e ON e.entity_id = em.entity_id
                      JOIN signals s ON s.rid = em.signal_rid
                     WHERE em.tenant_id = %s
                       AND em.latitude IS NOT NULL
                       AND em.longitude IS NOT NULL
                       AND s.signal_type = 'signal.content.rss_entry'
                       AND s.timestamp > NOW() - (%s || ' hours')::interval
                       AND NOT (e.envelope ? 'merged_into')
                     GROUP BY e.entity_id, e.canonical_name, e.canonical_type
                    HAVING COUNT(DISTINCT em.signal_rid) >= %s
                     ORDER BY article_count DESC, mention_count DESC
                     LIMIT 500
                    """,
                    (auth.tenant_id, str(hours), min_articles),
                )
                for r in cur.fetchall():
                    points.append({
                        "entity_id": r[0],
                        "name": r[1],
                        "type": r[2] or "unknown",
                        "lat": r[3],
                        "lon": r[4],
                        "article_count": r[5],
                        "mention_count": r[6],
                    })
    except Exception as e:
        log.error(f"/feed/heatmap failed: {e}")
        return {"points": [], "error": str(e)}

    return {
        "window_hours": hours,
        "point_count": len(points),
        "points": points,
    }


@router.get("/articles-near")
async def get_articles_near(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    entity_id: Optional[int] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    radius_km: float = Query(50.0, gt=0, le=2000),
    hours: int = Query(24, ge=1, le=168),
    limit: int = Query(40, ge=1, le=200),
):
    """Articles mentioning a specific location entity, OR articles within
    radius_km of (lat, lon). Powers click-on-heat-spot → see articles."""
    from storage.postgres.db import get_connection

    if entity_id is None and (lat is None or lon is None):
        return {"articles": [], "error": "provide entity_id OR lat+lon"}

    articles: list[dict] = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                if entity_id is not None:
                    cur.execute(
                        """
                        SELECT DISTINCT s.rid, s.signal_id, s.timestamp,
                               s.payload->>'title' AS title,
                               s.payload->>'source' AS source,
                               s.payload->>'link' AS link,
                               s.payload->>'category' AS category,
                               s.payload->>'thumbnail_url' AS thumbnail,
                               s.payload->>'summary' AS summary
                          FROM entity_mentions em
                          JOIN signals s ON s.rid = em.signal_rid
                         WHERE em.entity_id = %s
                           AND em.tenant_id = %s
                           AND s.signal_type = 'signal.content.rss_entry'
                           AND s.timestamp > NOW() - (%s || ' hours')::interval
                         ORDER BY s.timestamp DESC
                         LIMIT %s
                        """,
                        (entity_id, auth.tenant_id, str(hours), limit),
                    )
                else:
                    # Spherical-distance filter using haversine approximation
                    cur.execute(
                        """
                        SELECT DISTINCT s.rid, s.signal_id, s.timestamp,
                               s.payload->>'title' AS title,
                               s.payload->>'source' AS source,
                               s.payload->>'link' AS link,
                               s.payload->>'category' AS category,
                               s.payload->>'thumbnail_url' AS thumbnail,
                               s.payload->>'summary' AS summary
                          FROM entity_mentions em
                          JOIN signals s ON s.rid = em.signal_rid
                         WHERE em.latitude IS NOT NULL
                           AND em.longitude IS NOT NULL
                           AND em.tenant_id = %s
                           AND s.signal_type = 'signal.content.rss_entry'
                           AND s.timestamp > NOW() - (%s || ' hours')::interval
                           AND (
                             111.045 * sqrt(
                               power(em.latitude - %s, 2) +
                               power((em.longitude - %s) * cos(radians(%s)), 2)
                             )
                           ) <= %s
                         ORDER BY s.timestamp DESC
                         LIMIT %s
                        """,
                        (auth.tenant_id, str(hours), lat, lon, lat, radius_km, limit),
                    )

                for r in cur.fetchall():
                    summary = (r[8] or "")[:200]
                    articles.append({
                        "signal_id": r[1],
                        "timestamp": r[2].isoformat() if r[2] else None,
                        "headline": r[3],
                        "source": r[4],
                        "link": r[5],
                        "category": r[6],
                        "thumbnail_url": r[7],
                        "summary": summary,
                    })
    except Exception as e:
        log.error(f"/feed/articles-near failed: {e}")
        return {"articles": [], "error": str(e)}

    return {
        "article_count": len(articles),
        "articles": articles,
    }


# ─── Sectioned feed (by category) ─────────────────────────────────────────

@router.get("/sections")
async def get_feed_sections(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    hours: int = Query(48, ge=1, le=168),
    per_section: int = Query(8, ge=1, le=30),
    include_empty: bool = Query(True),
    categories: Optional[str] = Query(default=None,
        description="comma-separated category whitelist (e.g. 'defense,tech,world_news')"),
):
    """Top-N cards per RSS category, plus enabled-but-empty categories.

    Used by /feed (magazine layout) and /hub (configured buckets). When
    `categories` is supplied, only those categories are returned — used by
    the Hub to request just its configured buckets.
    """
    from storage.postgres.db import get_connection

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=hours)

    category_whitelist: Optional[list[str]] = None
    if categories:
        category_whitelist = [c.strip() for c in categories.split(",") if c.strip()]

    # Step 1: enumerate categories that exist in the catalog (catalog + currently-
    # enabled connectors) so we can show empty sections for new categories.
    catalog_categories: set[str] = set()
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT category FROM source_catalog WHERE deprecated_at IS NULL"
                )
                for r in cur.fetchall():
                    if r[0]:
                        catalog_categories.add(r[0])
    except Exception:
        pass

    # Step 2: fetch top-N cards per category from signals.
    sections: dict[str, dict] = {}
    rid_pool: list[int] = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cat_filter_sql = ""
                cat_filter_params: list = []
                if category_whitelist:
                    cat_filter_sql = " AND category = ANY(%s)"
                    cat_filter_params = [category_whitelist]
                cur.execute(
                    f"""
                    WITH ranked AS (
                        SELECT rid, signal_id, signal_type, severity, device_id,
                               timestamp, payload, latitude, longitude, metadata,
                               COALESCE(payload->>'category',
                                        metadata->>'_connector_category',
                                        'uncategorized') AS category,
                               ROW_NUMBER() OVER (
                                 PARTITION BY COALESCE(
                                     payload->>'category',
                                     metadata->>'_connector_category',
                                     'uncategorized')
                                 ORDER BY timestamp DESC
                               ) AS rn
                          FROM signals
                         WHERE tenant_id = %s
                           AND timestamp >= %s
                           AND signal_type = 'signal.content.rss_entry'
                    )
                    SELECT category, rid, signal_id, signal_type, severity, device_id,
                           timestamp, payload, latitude, longitude, metadata
                      FROM ranked
                     WHERE rn <= %s{cat_filter_sql}
                     ORDER BY category, timestamp DESC
                    """,
                    (auth.tenant_id, window_start, per_section, *cat_filter_params),
                )
                for r in cur.fetchall():
                    cat = r[0] or "uncategorized"
                    card = _render_card(
                        signal_id=r[2],
                        signal_type=r[3],
                        severity=r[4],
                        device_id=r[5],
                        timestamp=r[6],
                        payload=r[7] or {},
                        lat=r[8],
                        lon=r[9],
                        metadata=r[10] or {},
                    )
                    card["rid"] = r[1]
                    rid_pool.append(r[1])
                    if cat not in sections:
                        sections[cat] = {"category": cat, "total_24h": 0, "cards": []}
                    sections[cat]["cards"].append(card)

                # Step 3: total counts per category in the same window.
                cur.execute(
                    """
                    SELECT COALESCE(payload->>'category',
                                    metadata->>'_connector_category',
                                    'uncategorized') AS category,
                           COUNT(*) AS total
                      FROM signals
                     WHERE tenant_id = %s
                       AND timestamp >= %s
                       AND signal_type = 'signal.content.rss_entry'
                     GROUP BY category
                    """,
                    (auth.tenant_id, window_start),
                )
                for r in cur.fetchall():
                    cat = r[0]
                    if category_whitelist and cat not in category_whitelist:
                        continue
                    if cat in sections:
                        sections[cat]["total_24h"] = r[1]
                    elif include_empty:
                        sections[cat] = {"category": cat, "total_24h": r[1], "cards": []}
    except Exception as e:
        log.error(f"/feed/sections query failed: {e}")
        return {"sections": [], "error": str(e)}

    # Step 4: fill in empty sections for catalog-known categories with no signals.
    if include_empty:
        for cat in catalog_categories:
            if category_whitelist and cat not in category_whitelist:
                continue
            if cat not in sections:
                sections[cat] = {"category": cat, "total_24h": 0, "cards": []}

    # Step 5: entity_mentions join — same logic as /feed.
    SOURCE_TYPES = {"mass_media", "publisher", "media_outlet"}
    if rid_pool:
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT em.signal_rid, e.entity_id, e.canonical_name,
                               e.canonical_type
                          FROM entity_mentions em
                          JOIN entities e ON e.entity_id = em.entity_id
                         WHERE em.signal_rid = ANY(%s)
                           AND e.canonical_name IS NOT NULL
                           AND e.canonical_name != ''
                           AND NOT (e.envelope ? 'merged_into')
                         ORDER BY em.signal_rid, em.confidence DESC NULLS LAST
                        """,
                        (rid_pool,),
                    )
                    by_rid: dict[int, dict] = {}
                    for r in cur.fetchall():
                        sig_rid, ent_id, name, ctype = r
                        if sig_rid not in by_rid:
                            by_rid[sig_rid] = {"subjects": [], "sources": []}
                        bucket = "sources" if (ctype or "").lower() in SOURCE_TYPES else "subjects"
                        if not any(e["entity_id"] == ent_id for e in by_rid[sig_rid][bucket]):
                            by_rid[sig_rid][bucket].append({
                                "entity_id": ent_id,
                                "name": name,
                                "type": ctype or "unknown",
                            })
                    for sec in sections.values():
                        for card in sec["cards"]:
                            ents = by_rid.get(card.get("rid"), {"subjects": [], "sources": []})
                            card["entities"] = {
                                "subjects": ents["subjects"][:8],
                                "sources": ents["sources"][:3],
                            }
        except Exception as e:
            log.warning(f"entity_mentions join failed in /sections: {e}")

    # Step 5b: library_signals join — server-side library chips per card.
    if rid_pool:
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT ls.signal_rid, l.library_id, l.canonical_name,
                               l.focal_term, l.base60_address,
                               ls.match_kind, ls.match_score
                          FROM library_signals ls
                          JOIN libraries l ON l.library_id = ls.library_id
                         WHERE ls.signal_rid = ANY(%s)
                        """,
                        (rid_pool,),
                    )
                    lib_map: dict[int, list] = {}
                    for r in cur.fetchall():
                        rid = r[0]
                        if rid not in lib_map:
                            lib_map[rid] = []
                        focal = r[3]
                        canonical = r[2]
                        title = focal if focal else (
                            canonical.split("/", 1)[1] if canonical and "/" in canonical else canonical
                        )
                        lib_map[rid].append({
                            "library_id": r[1],
                            "title": title,
                            "address": r[4],
                            "match_kind": r[5],
                            "score": r[6],
                        })
                    for sec in sections.values():
                        for card in sec["cards"]:
                            card["matched_libraries"] = lib_map.get(card.get("rid"), [])
        except Exception as e:
            log.warning(f"library_signals join failed in /sections: {e}")

    # Drop internal rid before returning
    for sec in sections.values():
        for card in sec["cards"]:
            card.pop("rid", None)

    # Step 6: sort sections — categories with content first (by recency of newest
    # card, then by volume); empty ones last. Within a category, cards already
    # come back timestamp DESC from the window function.
    def sort_key(sec):
        has_content = 1 if sec["cards"] else 0
        # Newest card timestamp (or 0 for empty)
        newest = sec["cards"][0]["timestamp"] if sec["cards"] else ""
        return (-has_content, -sec["total_24h"], newest)

    sorted_sections = sorted(sections.values(), key=sort_key)

    return {
        "window_hours": hours,
        "section_count": len(sorted_sections),
        "sections": sorted_sections,
    }


# ─── Aggregates ───────────────────────────────────────────────────────────

@router.get("/aggregates")
async def get_aggregates(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    hours: int = Query(24, ge=1, le=168),
    top_n: int = Query(20, ge=1, le=100),
):
    """Top subjects, top cited sources, and category volumes for the window.

    Subjects = people / orgs / countries / agencies / etc. — who/what is being
    reported on.
    Sources = mass_media canonical types extracted from articles — the
    publishers being cited (Reuters, AP, NYT in body text).
    By category = connector-level RSS category volumes for the same window.
    """
    from storage.postgres.db import get_connection

    SOURCE_TYPES = ("mass_media", "publisher", "media_outlet")

    result = {
        "window_hours": hours,
        "top_subjects": [],
        "top_sources": [],
        "by_category": [],
        "by_outlet": [],
    }

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Top subjects (excluding mass_media)
                cur.execute(
                    """
                    SELECT e.entity_id, e.canonical_name, e.canonical_type,
                           COUNT(DISTINCT em.signal_rid) AS mention_count,
                           COUNT(DISTINCT s.device_id) AS source_count
                    FROM entity_mentions em
                    JOIN entities e ON e.entity_id = em.entity_id
                    JOIN signals s ON s.rid = em.signal_rid
                    WHERE em.tenant_id = %s
                      AND em.signal_rid IS NOT NULL
                      AND s.timestamp > NOW() - (%s || ' hours')::interval
                      AND LOWER(COALESCE(e.canonical_type, '')) NOT IN %s
                      AND e.canonical_name IS NOT NULL
                      AND e.canonical_name != ''
                    GROUP BY e.entity_id, e.canonical_name, e.canonical_type
                    ORDER BY mention_count DESC, source_count DESC
                    LIMIT %s
                    """,
                    (auth.tenant_id, str(hours), SOURCE_TYPES, top_n),
                )
                for r in cur.fetchall():
                    result["top_subjects"].append({
                        "entity_id": r[0],
                        "name": r[1],
                        "type": r[2] or "unknown",
                        "mention_count": r[3],
                        "source_count": r[4],
                    })

                # Top cited sources (mass_media entities mentioned in articles)
                cur.execute(
                    """
                    SELECT e.entity_id, e.canonical_name, e.canonical_type,
                           COUNT(DISTINCT em.signal_rid) AS mention_count
                    FROM entity_mentions em
                    JOIN entities e ON e.entity_id = em.entity_id
                    JOIN signals s ON s.rid = em.signal_rid
                    WHERE em.tenant_id = %s
                      AND em.signal_rid IS NOT NULL
                      AND s.timestamp > NOW() - (%s || ' hours')::interval
                      AND LOWER(COALESCE(e.canonical_type, '')) IN %s
                    GROUP BY e.entity_id, e.canonical_name, e.canonical_type
                    ORDER BY mention_count DESC
                    LIMIT %s
                    """,
                    (auth.tenant_id, str(hours), SOURCE_TYPES, top_n),
                )
                for r in cur.fetchall():
                    result["top_sources"].append({
                        "entity_id": r[0],
                        "name": r[1],
                        "type": r[2] or "unknown",
                        "mention_count": r[3],
                    })

                # By category (RSS connector category — auto/political/world_news/etc.)
                cur.execute(
                    """
                    SELECT COALESCE(payload->>'category', metadata->>'_connector_category', 'uncategorized') AS cat,
                           COUNT(*) AS signal_count,
                           COUNT(DISTINCT device_id) AS source_count
                    FROM signals
                    WHERE tenant_id = %s
                      AND timestamp > NOW() - (%s || ' hours')::interval
                      AND signal_type = 'signal.content.rss_entry'
                    GROUP BY cat
                    ORDER BY signal_count DESC
                    """,
                    (auth.tenant_id, str(hours)),
                )
                for r in cur.fetchall():
                    result["by_category"].append({
                        "category": r[0],
                        "signal_count": r[1],
                        "source_count": r[2],
                    })

                # By outlet (publisher domain — for fox-vs-cnn-style comparison)
                cur.execute(
                    """
                    SELECT COALESCE(payload->>'source', payload->>'feed_title', device_id) AS outlet,
                           COUNT(*) AS article_count
                    FROM signals
                    WHERE tenant_id = %s
                      AND timestamp > NOW() - (%s || ' hours')::interval
                      AND signal_type = 'signal.content.rss_entry'
                    GROUP BY outlet
                    ORDER BY article_count DESC
                    LIMIT %s
                    """,
                    (auth.tenant_id, str(hours), top_n),
                )
                for r in cur.fetchall():
                    result["by_outlet"].append({
                        "outlet": r[0],
                        "article_count": r[1],
                    })

    except Exception as e:
        log.error(f"feed/aggregates failed: {e}")
        result["error"] = str(e)

    return result


def _render_card(
    signal_id: str,
    signal_type: str,
    severity: str,
    device_id: str,
    timestamp,
    payload: dict,
    lat: Optional[float],
    lon: Optional[float],
    metadata: dict,
) -> dict:
    """Render a signal into a rich card based on its type."""

    # Base card
    card = {
        "signal_id": signal_id,
        "signal_type": signal_type,
        "severity": severity,
        "device_id": device_id,
        "timestamp": timestamp.isoformat() if timestamp else None,
        "headline": None,
        "summary": None,
        "source": None,
        "source_url": None,
        "thumbnail_url": None,
        "map_url": None,
        "latitude": lat,
        "longitude": lon,
        "badges": [],
        "metrics": {},
        "actions": ["dismiss", "add_to_case"],
        "domain": signal_type.split(".")[1] if "." in signal_type else "unknown",
    }

    # Route to type-specific renderer
    if "content.news" in signal_type:
        _render_news(card, payload, device_id)
    elif "environmental.seismic" in signal_type:
        _render_earthquake(card, payload)
    elif "environmental.weather_alert" in signal_type:
        _render_weather_alert(card, payload)
    elif "environmental.weather" in signal_type:
        _render_weather(card, payload)
    elif "environmental.air_quality" in signal_type:
        _render_air_quality(card, payload)
    elif "content.social_post" in signal_type:
        _render_social(card, payload)
    elif "content.rss_entry" in signal_type:
        _render_rss(card, payload)
    elif "geospatial.aircraft_position" in signal_type:
        _render_aircraft(card, payload)
    elif "geospatial.vessel_position" in signal_type:
        _render_vessel(card, payload)
    elif "geospatial.position_report" in signal_type:
        _render_position(card, payload)
    elif "financial.balance_update" in signal_type:
        _render_crypto(card, payload)
    elif "financial.transaction" in signal_type:
        _render_transaction(card, payload)
    elif "security" in signal_type:
        _render_security(card, payload)
    elif "infrastructure" in signal_type or "device" in signal_type:
        _render_sensor(card, payload)
    else:
        _render_generic(card, payload)

    # Add map thumbnail for any signal with coordinates
    if card["latitude"] and card["longitude"] and not card["map_url"]:
        card["map_url"] = _static_map_url(card["latitude"], card["longitude"])
        if "track_on_map" not in card["actions"]:
            card["actions"].append("track_on_map")

    return card


# ─── Type-Specific Renderers ──────────────────────────────────────────────

def _render_news(card: dict, payload: dict, device_id: str):
    """Render news signal (HN Algolia, GDELT, RSS)."""
    # Hacker News (Algolia API)
    if payload.get("title"):
        card["headline"] = payload["title"]
        card["source"] = payload.get("url", "").split("/")[2] if payload.get("url") else "Hacker News"
        card["source_url"] = payload.get("url") or f"https://news.ycombinator.com/item?id={payload.get('objectID', '')}"
        card["metrics"] = {
            "points": payload.get("points"),
            "comments": payload.get("num_comments"),
        }
        card["badges"].append({"label": card["source"], "color": "blue"})
        if payload.get("points") and payload["points"] > 200:
            card["badges"].append({"label": "trending", "color": "amber"})
        card["actions"].extend(["watch_topic", "open_link"])

    # GDELT
    elif payload.get("title") and payload.get("socialimage"):
        card["headline"] = payload["title"]
        card["source"] = payload.get("domain", payload.get("source", ""))
        card["source_url"] = payload.get("url")
        card["thumbnail_url"] = payload.get("thumbnail_url") or payload.get("socialimage")
        card["summary"] = payload.get("seendate", "")
        lang = payload.get("language", "")
        if lang and lang != "English":
            card["badges"].append({"label": lang, "color": "purple"})

        # Article content for in-app reading
        card["article_preview"] = {
            "full_text": payload.get("full_text", ""),
            "first_paragraph": payload.get("first_paragraph", ""),
            "author": payload.get("author", ""),
            "og_description": payload.get("og_description", ""),
            "word_count": payload.get("word_count", 0),
        }

        card["actions"].extend(["view_article", "watch_topic", "open_link"])

    # Fallback for news without known structure
    elif payload.get("value") and isinstance(payload["value"], (int, str)):
        card["headline"] = f"Story #{payload['value']}"
        card["source"] = device_id.replace("default_", "").replace("_", " ").title()
    else:
        _render_generic(card, payload)


def _render_earthquake(card: dict, payload: dict):
    """Render USGS earthquake signal."""
    props = payload.get("properties", payload)
    mag = props.get("mag") or payload.get("mag")
    place = props.get("place") or payload.get("place", "Unknown location")

    card["headline"] = f"M{mag} — {place}" if mag else place
    card["source"] = "USGS"
    card["source_url"] = props.get("url") or payload.get("url")
    card["severity"] = "critical" if mag and float(mag) >= 6 else "high" if mag and float(mag) >= 4.5 else "medium"

    card["metrics"] = {
        "magnitude": mag,
        "depth_km": props.get("depth") or payload.get("depth"),
        "felt": props.get("felt"),
        "tsunami": props.get("tsunami"),
    }

    if mag:
        color = "red" if float(mag) >= 6 else "amber" if float(mag) >= 4.5 else "emerald"
        card["badges"].append({"label": f"M{mag}", "color": color})
    if props.get("tsunami"):
        card["badges"].append({"label": "tsunami warning", "color": "red"})

    # Extract coordinates from GeoJSON geometry
    geom = payload.get("geometry", {})
    if isinstance(geom, dict) and geom.get("coordinates"):
        coords = geom["coordinates"]
        if len(coords) >= 2:
            card["longitude"] = coords[0]
            card["latitude"] = coords[1]

    card["actions"].extend(["track_on_map", "create_alert"])


def _render_weather_alert(card: dict, payload: dict):
    """Render NASA EONET or NWS weather alert."""
    # NASA EONET
    if payload.get("title") and payload.get("categories"):
        card["headline"] = payload["title"]
        cats = payload.get("categories", [])
        cat_name = cats[0].get("title", "") if cats else ""
        card["source"] = "NASA EONET"
        card["source_url"] = payload.get("link")
        card["badges"].append({"label": cat_name, "color": "amber"})

        # Extract latest coordinates from geometry array
        geom = payload.get("geometry", [])
        if isinstance(geom, list) and geom:
            latest = geom[-1]
            coords = latest.get("coordinates", [])
            if len(coords) >= 2:
                card["longitude"] = coords[0]
                card["latitude"] = coords[1]
            mag = latest.get("magnitudeValue")
            unit = latest.get("magnitudeUnit", "")
            if mag:
                card["metrics"]["magnitude"] = f"{mag} {unit}"
                card["badges"].append({"label": f"{mag} {unit}", "color": "red"})

        # Summary from sources
        sources = payload.get("sources", [])
        if sources:
            card["summary"] = f"Source: {sources[0].get('id', '')}"
            card["source_url"] = sources[0].get("url") or card["source_url"]

        card["actions"].extend(["track_on_map"])

    # NWS Alert
    elif payload.get("properties", {}).get("headline"):
        props = payload.get("properties", {})
        card["headline"] = props.get("headline", "Weather Alert")
        card["summary"] = props.get("description", "")[:200]
        card["source"] = "NWS"
        card["severity"] = props.get("severity", "moderate").lower()
        card["badges"].append({"label": props.get("event", "Alert"), "color": "amber"})
    else:
        card["headline"] = payload.get("title", "Weather Event")
        card["source"] = "Environmental"


def _render_weather(card: dict, payload: dict):
    """Render weather reading."""
    card["headline"] = f"{payload.get('temperature', '?')}° — {payload.get('conditions', 'Weather')}"
    card["source"] = payload.get("station", "Weather")


def _render_air_quality(card: dict, payload: dict):
    """Render air quality signal."""
    aqi = payload.get("aqi") or payload.get("pm25")
    card["headline"] = f"AQI {aqi}" if aqi else "Air Quality Reading"
    card["source"] = "Air Quality"


def _render_social(card: dict, payload: dict):
    """Render social media post (Reddit, Twitter, Mastodon, etc.)."""
    # Reddit JSON format
    data = payload.get("data", payload)
    card["headline"] = data.get("title", payload.get("title", "Social Post"))
    card["source"] = f"r/{data.get('subreddit', '')}" if data.get("subreddit") else "Social"
    card["source_url"] = data.get("url") or payload.get("url")
    card["thumbnail_url"] = data.get("thumbnail") if data.get("thumbnail", "").startswith("http") else None
    card["metrics"] = {
        "score": data.get("score") or data.get("ups"),
        "comments": data.get("num_comments"),
    }
    if data.get("score") and data["score"] > 1000:
        card["badges"].append({"label": "popular", "color": "amber"})
    card["actions"].extend(["watch_topic", "open_link"])


def _render_rss(card: dict, payload: dict):
    """Render RSS entry with cached thumbnail and article preview."""
    card["headline"] = payload.get("title", "RSS Entry")
    card["source"] = payload.get("source") or payload.get("feed_title") or payload.get("feed_source") or "RSS"
    card["source_url"] = payload.get("link") or payload.get("url")
    card["category"] = payload.get("category")
    card["region"] = payload.get("region")
    card["language"] = payload.get("language")
    card["summary"] = (payload.get("summary") or payload.get("og_description") or payload.get("description") or "")[:200]

    # Cached thumbnail (local proxy URL) or external fallback
    if payload.get("thumbnail_url"):
        card["thumbnail_url"] = payload["thumbnail_url"]
    elif payload.get("image_url"):
        card["thumbnail_url"] = payload["image_url"]

    # Article content for in-app reading (never leave the platform)
    card["article_preview"] = {
        "full_text": payload.get("full_text", ""),
        "first_paragraph": payload.get("first_paragraph", ""),
        "author": payload.get("author", ""),
        "og_description": payload.get("og_description", ""),
        "word_count": payload.get("word_count", 0),
    }

    card["actions"].extend(["view_article", "watch_topic", "open_link"])


def _render_aircraft(card: dict, payload: dict):
    """Render ADS-B aircraft position."""
    callsign = payload.get("flight", payload.get("callsign", "")).strip()
    reg = payload.get("r", payload.get("registration", ""))
    alt = payload.get("alt_baro") or payload.get("altitude")
    speed = payload.get("gs") or payload.get("speed")
    card["headline"] = f"{callsign or reg or 'Aircraft'} — {alt}ft" if alt else callsign or "Aircraft"
    card["source"] = "ADS-B"
    card["metrics"] = {"altitude_ft": alt, "speed_kts": speed, "heading": payload.get("track")}
    card["badges"].append({"label": "aircraft", "color": "cyan"})
    card["actions"].extend(["track_on_map"])


def _render_vessel(card: dict, payload: dict):
    """Render AIS vessel position."""
    props = payload.get("properties", payload)
    name = props.get("name", payload.get("shipName", ""))
    mmsi = props.get("mmsi", payload.get("mmsi", ""))
    speed = props.get("sog") or payload.get("speed")
    card["headline"] = name or f"MMSI {mmsi}" or "Vessel"
    card["source"] = "AIS"
    card["metrics"] = {"speed_kts": speed, "heading": props.get("cog"), "mmsi": mmsi}
    card["badges"].append({"label": "vessel", "color": "cyan"})
    card["actions"].extend(["track_on_map"])


def _render_position(card: dict, payload: dict):
    """Render GPS position report."""
    label = payload.get("label", payload.get("device_id", "GPS"))
    card["headline"] = f"Position: {label}"
    card["source"] = "GPS"
    card["actions"].extend(["track_on_map"])


def _render_crypto(card: dict, payload: dict):
    """Render crypto/financial market data."""
    # CoinGecko format: {"bitcoin": {"usd": 65000, "usd_24h_change": -2.5}}
    for coin, data in payload.items():
        if isinstance(data, dict) and "usd" in data:
            price = data.get("usd", 0)
            change = data.get("usd_24h_change", 0)
            direction = "up" if change >= 0 else "down"
            card["headline"] = f"{coin.upper()} ${price:,.2f}"
            card["summary"] = f"{'▲' if change >= 0 else '▼'} {abs(change):.1f}% 24h"
            card["source"] = "CoinGecko"
            card["metrics"] = {"price_usd": price, "change_24h": change, "market_cap": data.get("usd_market_cap")}
            color = "emerald" if change >= 0 else "red"
            card["badges"].append({"label": f"{'▲' if change >= 0 else '▼'}{abs(change):.1f}%", "color": color})
            break
    else:
        card["headline"] = "Market Data"
        card["source"] = "Financial"
    card["actions"] = ["dismiss"]


def _render_transaction(card: dict, payload: dict):
    """Render financial transaction."""
    amount = payload.get("amount", 0)
    desc = payload.get("description", payload.get("merchant", "Transaction"))
    card["headline"] = f"${abs(amount):,.2f} — {desc}"
    card["source"] = payload.get("account", "Financial")
    card["badges"].append({"label": "debit" if amount < 0 else "credit", "color": "red" if amount < 0 else "emerald"})


def _render_security(card: dict, payload: dict):
    """Render security signal."""
    card["headline"] = payload.get("title") or payload.get("event_type", "Security Event")
    card["summary"] = payload.get("description", "")[:200]
    card["source"] = payload.get("source", "Security")
    card["severity"] = payload.get("severity", "medium")
    card["badges"].append({"label": "security", "color": "red"})
    card["actions"].extend(["create_alert"])


def _render_sensor(card: dict, payload: dict):
    """Render sensor/infrastructure signal."""
    reading = payload.get("temperature") or payload.get("value") or payload.get("reading")
    card["headline"] = f"Sensor: {reading}" if reading else "Sensor Reading"
    card["source"] = payload.get("device_id", "Sensor")


def _render_generic(card: dict, payload: dict):
    """Fallback for unknown signal types."""
    # Try to find a title/headline in common fields
    for key in ("title", "headline", "name", "subject", "event", "message"):
        if payload.get(key):
            card["headline"] = str(payload[key])[:200]
            break
    else:
        # Use first string value as headline
        for k, v in payload.items():
            if isinstance(v, str) and len(v) > 5:
                card["headline"] = f"{k}: {v[:100]}"
                break

    card["source"] = payload.get("source", card["device_id"].replace("default_", "").replace("_", " ").title())


# ─── Utilities ──────────────────────────────────────────────────────────────

def _static_map_url(lat: float, lon: float, zoom: int = 6, w: int = 200, h: int = 120) -> str:
    """Generate a static map thumbnail URL (free, no API key)."""
    return f"https://staticmap.openstreetmap.de/staticmap.php?center={lat},{lon}&zoom={zoom}&size={w}x{h}&markers={lat},{lon},red-pushpin"
