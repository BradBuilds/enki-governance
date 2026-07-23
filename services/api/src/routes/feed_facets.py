"""
Feed Facets — dynamic categorization, filtering, and trend analysis.

GET /feed/facets          — All facets with counts and trend indicators
GET /feed/facets/trend    — Hourly trend data for a specific facet
GET /feed/facets/grouped  — Feed cards grouped by topic with dedup
"""

from __future__ import annotations

import json
import re
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

try:
    # Reuse the existing conflict heuristic (opposing word pairs + credibility gap)
    from routes.feed_graph import _detect_conflicts as _fg_detect_conflicts
except Exception:  # pragma: no cover — defensive import so the feed never 500s
    _fg_detect_conflicts = None

_HTML_TAG_RE = re.compile(r'<[^>]+>')
_WHITESPACE_RE = re.compile(r'\s+')
_URL_RE = re.compile(r'https?://\S+')
_ARTICLE_URL_RE = re.compile(r'Article URL:\s*', re.IGNORECASE)
_COMMENTS_URL_RE = re.compile(r'Comments\s*URL:\s*\S*', re.IGNORECASE)
_COMMENTS_RE = re.compile(r'\bComments:\s*\S*', re.IGNORECASE)
_POINTS_RE = re.compile(r'\bPoints:\s*\d+', re.IGNORECASE)
_NUM_COMMENTS_RE = re.compile(r'#\s*Comments', re.IGNORECASE)
_HN_SUBMITTED_RE = re.compile(r'Submitted\s+by\s+\S+', re.IGNORECASE)
_READ_MORE_RE = re.compile(r'\b(?:read\s+more|continue\s+reading|click\s+here)\.*', re.IGNORECASE)
_AT_HN_RE = re.compile(r'at\s+Hacker\s+News', re.IGNORECASE)
_TRAILING_PUNCT_RE = re.compile(r'^[\s\-—–\|·#\.,;:]+|[\s\-—–\|·#]+$')
_STANDALONE_HASH_RE = re.compile(r'(?<!\w)#(?!\w)')


def _strip_html(text: str) -> str:
    """Strip HTML tags, URLs, RSS boilerplate, and HN metadata junk."""
    if not text:
        return ""
    cleaned = _HTML_TAG_RE.sub(' ', text)
    cleaned = _URL_RE.sub('', cleaned)
    cleaned = _COMMENTS_URL_RE.sub('', cleaned)
    cleaned = _ARTICLE_URL_RE.sub('', cleaned)
    cleaned = _COMMENTS_RE.sub('', cleaned)
    cleaned = _POINTS_RE.sub('', cleaned)
    cleaned = _NUM_COMMENTS_RE.sub('', cleaned)
    cleaned = _HN_SUBMITTED_RE.sub('', cleaned)
    cleaned = _READ_MORE_RE.sub('', cleaned)
    cleaned = _AT_HN_RE.sub('', cleaned)
    cleaned = _STANDALONE_HASH_RE.sub('', cleaned)
    # Decode common HTML entities
    cleaned = (cleaned
        .replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
        .replace('&quot;', '"').replace('&#39;', "'").replace('&nbsp;', ' ')
        .replace('&mdash;', '—').replace('&ndash;', '–'))
    cleaned = _WHITESPACE_RE.sub(' ', cleaned).strip()
    cleaned = _TRAILING_PUNCT_RE.sub('', cleaned)
    # If only a few chars left, probably all junk — return empty
    if len(cleaned) < 15:
        return ""
    return cleaned

log = logging.getLogger("enki.feed_facets")
router = APIRouter(prefix="/feed", tags=["feed"])


@router.get("/facets")
async def get_facets(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    hours: int = Query(24, le=168),
):
    """Return all facets with counts and trend direction.

    Each facet type returns its top values sorted by count, with a
    trend indicator (up/down/stable) comparing the recent window
    to the prior window of equal length.
    """
    from storage.postgres.db import get_connection

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    prior_start = since - timedelta(hours=hours)

    result: dict[str, list] = {}

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Current window counts
                cur.execute("""
                    SELECT facet_type, facet_value, facet_label,
                           SUM(count) as total,
                           MAX(sample_signal_id) as sample_id
                    FROM facet_counts
                    WHERE tenant_id = %s AND window_start >= %s
                    GROUP BY facet_type, facet_value, facet_label
                    ORDER BY facet_type, total DESC
                """, (auth.tenant_id, since))

                current: dict[str, list] = {}
                for r in cur.fetchall():
                    ft, fv, fl, total, sample = r
                    if ft not in current:
                        current[ft] = []
                    current[ft].append({
                        "value": fv,
                        "label": fl or fv,
                        "count": total,
                        "sample_signal_id": sample,
                    })

                # Prior window counts for trend calculation
                cur.execute("""
                    SELECT facet_type, facet_value, SUM(count) as total
                    FROM facet_counts
                    WHERE tenant_id = %s
                      AND window_start >= %s AND window_start < %s
                    GROUP BY facet_type, facet_value
                """, (auth.tenant_id, prior_start, since))

                prior: dict[str, dict[str, int]] = {}
                for r in cur.fetchall():
                    ft, fv, total = r
                    if ft not in prior:
                        prior[ft] = {}
                    prior[ft][fv] = total

                # Calculate trends
                for ft, entries in current.items():
                    for entry in entries:
                        prev = prior.get(ft, {}).get(entry["value"], 0)
                        curr = entry["count"]
                        if prev == 0:
                            entry["trend"] = "new" if curr > 0 else "stable"
                        elif curr > prev * 1.5:
                            entry["trend"] = "up"
                        elif curr < prev * 0.5:
                            entry["trend"] = "down"
                        else:
                            entry["trend"] = "stable"

                        # Delta percentage
                        if prev > 0:
                            entry["delta_pct"] = round(((curr - prev) / prev) * 100)
                        else:
                            entry["delta_pct"] = 100 if curr > 0 else 0

                    # Limit to top 20 per facet type
                    result[ft] = entries[:20]

                # Enrich top entities with conflict counts and tier
                all_labels = []
                for entries in result.values():
                    for e in entries:
                        all_labels.append(e["label"])

                if all_labels:
                    cur.execute("""
                        SELECT canonical_name, conflict_count, completeness_tier, verified_at
                        FROM entities
                        WHERE tenant_id = %s AND canonical_name = ANY(%s)
                          AND NOT (COALESCE(envelope, '{}'::jsonb) ? 'reclassification')
                    """, (auth.tenant_id, all_labels))
                    enrichment = {}
                    for r in cur.fetchall():
                        enrichment[r[0]] = {
                            "conflict_count": r[1] or 0,
                            "tier": r[2] or "stub",
                            "verified": r[3] is not None,
                        }
                    for entries in result.values():
                        for e in entries:
                            ent = enrichment.get(e["label"], {})
                            e["conflict_count"] = ent.get("conflict_count", 0)
                            e["tier"] = ent.get("tier", "stub")
                            e["verified"] = ent.get("verified", False)

    except Exception as e:
        log.error("Facet query failed: %s", e, exc_info=True)
        return {"facets": {}, "error": str(e)}

    return {
        "facets": result,
        "window_hours": hours,
        "since": since.isoformat(),
    }


@router.get("/facets/trend")
async def get_facet_trend(
    facet_type: str = Query(...),
    facet_value: str = Query(...),
    hours: int = Query(72, le=168),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Return hourly trend data for a specific facet value."""
    from storage.postgres.db import get_connection

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)

    buckets = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT window_start, count, facet_label
                    FROM facet_counts
                    WHERE tenant_id = %s
                      AND facet_type = %s
                      AND facet_value = %s
                      AND window_start >= %s
                    ORDER BY window_start ASC
                """, (auth.tenant_id, facet_type, facet_value, since))

                label = facet_value
                for r in cur.fetchall():
                    buckets.append({
                        "hour": r[0].isoformat(),
                        "count": r[1],
                    })
                    if r[2]:
                        label = r[2]

    except Exception as e:
        log.error("Facet trend query failed: %s", e)
        return {"buckets": [], "error": str(e)}

    # Compute summary stats
    total = sum(b["count"] for b in buckets)
    recent_24h = sum(b["count"] for b in buckets if
                     datetime.fromisoformat(b["hour"]) >= now - timedelta(hours=24))
    prior_24h = sum(b["count"] for b in buckets if
                    since <= datetime.fromisoformat(b["hour"]) < now - timedelta(hours=24))

    if prior_24h > 0:
        delta_pct = round(((recent_24h - prior_24h) / prior_24h) * 100)
    else:
        delta_pct = 100 if recent_24h > 0 else 0

    # Velocity: accelerating, decelerating, steady
    if len(buckets) >= 6:
        recent_6h = sum(b["count"] for b in buckets[-6:])
        prior_6h = sum(b["count"] for b in buckets[-12:-6]) if len(buckets) >= 12 else 0
        if prior_6h > 0 and recent_6h > prior_6h * 1.5:
            velocity = "accelerating"
        elif prior_6h > 0 and recent_6h < prior_6h * 0.5:
            velocity = "decelerating"
        else:
            velocity = "steady"
    else:
        velocity = "steady"

    return {
        "facet_type": facet_type,
        "facet_value": facet_value,
        "label": label,
        "buckets": buckets,
        "total": total,
        "total_24h": recent_24h,
        "delta_vs_prior_24h": f"{'+' if delta_pct >= 0 else ''}{delta_pct}%",
        "velocity": velocity,
    }


@router.get("/facets/grouped")
async def get_grouped_feed(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    limit: int = Query(100, le=300),
    category: Optional[str] = None,
    country: Optional[str] = None,
    person: Optional[str] = None,
    organization: Optional[str] = None,
    event_type: Optional[str] = None,
    group_by: str = Query("topic", regex="^(topic|country|category|person|organization)$"),
    hours: int = Query(24, le=168),
    exclude_watched: bool = Query(True),
):
    """Feed cards grouped by topic (or other facet) with smart dedup.

    Returns topic clusters where similar articles are grouped together.
    If exclude_watched=True (default), articles whose entities match an active
    watch topic are excluded — keeping the feed focused on DISCOVERY.
    """
    from storage.postgres.db import get_connection

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)

    # Fetch watched topic KEYWORDS (only) to exclude from general feed.
    # We deliberately do NOT include extracted entities — that would
    # filter out everything (every country, person, org mentioned in the topic).
    # Only the explicit keywords the user defined for the watch topic count.
    watched_keywords: set[str] = set()
    if exclude_watched:
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT LOWER(unnest(keywords)) FROM watch_topics
                        WHERE tenant_id = %s AND status = 'active'
                    """, (auth.tenant_id,))
                    for r in cur.fetchall():
                        if r[0] and len(r[0]) >= 4:  # min 4 chars to avoid common words
                            watched_keywords.add(r[0])
        except Exception as e:
            log.debug("Failed to load watched keywords: %s", e)

    # Build WHERE clauses (use s. prefix — query aliases signals as s)
    clauses = ["s.tenant_id = %s", "s.timestamp >= %s"]
    params: list = [auth.tenant_id, since]

    # Exclude noise
    noise_types = [
        "signal.geospatial.aircraft_position",
        "signal.geospatial.vessel_position",
        "signal.geospatial.position_report",
        "signal.device.sensor_reading",
        "signal.infrastructure.sensor_reading",
    ]
    for nt in noise_types:
        clauses.append("s.signal_type != %s")
        params.append(nt)

    # Exclude topic-specific articles
    clauses.append("(s.metadata->>'_topic_id' IS NULL OR s.metadata->>'_topic_id' = '')")

    # Facet filters (check both signals.facets and enrichment entities)
    if category:
        clauses.append("(s.facets->>'category' = %s OR se.event_type = %s)")
        params.extend([category, category])
    if country:
        clauses.append("s.facets->'countries' ? %s")
        params.append(country)
    if person:
        clauses.append("s.facets->'people' ? %s")
        params.append(person)
    if organization:
        clauses.append("s.facets->'organizations' ? %s")
        params.append(organization)
    if event_type:
        clauses.append("(s.facets->>'event_type' = %s OR se.event_type = %s)")
        params.extend([event_type, event_type])

    # Fetch many more articles than the requested group limit, so that
    # clustering can produce a useful number of grouped topics.
    # If user asks for 100 groups, we need ~10x raw articles to feed clustering.
    fetch_limit = max(limit * 10, 500)
    params.append(fetch_limit)

    cards = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT s.signal_id, s.signal_type, s.severity, s.device_id,
                           s.timestamp, s.payload, s.latitude, s.longitude,
                           s.metadata, s.facets,
                           se.entities AS enrich_entities,
                           se.event_type AS enrich_event_type,
                           se.importance AS enrich_importance,
                           se.summary AS enrich_summary
                    FROM signals s
                    LEFT JOIN LATERAL (
                        SELECT entities, event_type, importance, summary
                        FROM signal_enrichments
                        WHERE signal_rid = s.rid AND tier >= 2
                        ORDER BY extracted_at DESC LIMIT 1
                    ) se ON TRUE
                    WHERE {' AND '.join(clauses)}
                    ORDER BY s.timestamp DESC
                    LIMIT %s
                """, params)

                for r in cur.fetchall():
                    payload = r[5] or {}
                    headline = _strip_html(
                        payload.get("title")
                        or payload.get("headline")
                        or payload.get("name")
                        or ""
                    )
                    if not headline or len(headline) < 10:
                        continue
                    if headline.startswith("Story #"):
                        continue

                    summary = _strip_html(
                        payload.get("summary")
                        or payload.get("og_description")
                        or payload.get("description")
                        or ""
                    )[:300]

                    facets = r[9] or {}
                    metadata = r[8] or {}
                    enrich_entities = r[10] or []
                    enrich_event_type = r[11]
                    enrich_importance = r[12]
                    enrich_summary = r[13]

                    # Fall back to metadata-embedded facets for older signals
                    if not facets or facets == {}:
                        facets = metadata.get("facets", {})

                    # Build facets from enrichment data if signals.facets is empty
                    if (not facets or facets == {}) and enrich_entities:
                        built_facets: dict = {}
                        people = []
                        orgs = []
                        countries = []
                        keywords = []
                        for ent in enrich_entities:
                            ent_name = ent.get("name", "")
                            ent_type = ent.get("type", "")
                            if ent_type == "person" and ent_name:
                                people.append(ent_name)
                            elif ent_type == "organization" and ent_name:
                                orgs.append(ent_name)
                            elif ent_type == "location" and ent_name:
                                countries.append(ent_name)
                            if ent_name and ent_type not in ("quote", "quantity"):
                                keywords.append(ent_name)
                        if people: built_facets["people"] = people
                        if orgs: built_facets["organizations"] = orgs
                        if countries: built_facets["countries"] = countries
                        if enrich_event_type: built_facets["category"] = enrich_event_type
                        if keywords: built_facets["topic_keywords"] = keywords[:10]
                        facets = built_facets

                    # Use enrichment summary if payload summary is empty
                    if not summary and enrich_summary:
                        summary = _strip_html(enrich_summary)[:300]

                    # Exclude articles that match watched topics (keeps feed for discovery)
                    if watched_keywords:
                        article_entities: set[str] = set()
                        for p in facets.get("people", []):
                            article_entities.add(p.lower())
                        for o in facets.get("organizations", []):
                            article_entities.add(o.lower())
                        for c in facets.get("countries", []):
                            label = facets.get("country_labels", {}).get(c, c)
                            article_entities.add(label.lower())
                        # Also check headline for keyword matches
                        headline_lower = headline.lower()
                        if article_entities & watched_keywords:
                            continue
                        if any(kw in headline_lower for kw in watched_keywords if len(kw) >= 4):
                            continue

                    # Extract intelligence from enrichment for display
                    enrichment_facts = []
                    enrichment_claims = []
                    enrichment_entities_display = []
                    if enrich_entities:
                        for ent in enrich_entities:
                            etype = ent.get("type", "")
                            if etype == "fact":
                                enrichment_facts.append({
                                    "kind": ent.get("kind", "general"),
                                    "fact": ent.get("fact", ""),
                                    "value": ent.get("value", ""),
                                    "subject": ent.get("subject", ""),
                                })
                            elif etype == "claim":
                                enrichment_claims.append({
                                    "claim": ent.get("claim", ""),
                                    "source": ent.get("source", ""),
                                    "confidence": ent.get("confidence", "medium"),
                                })
                            elif etype in ("person", "organization") and ent.get("name"):
                                enrichment_entities_display.append({
                                    "name": ent["name"],
                                    "type": etype,
                                    "role": ent.get("role_in_article", ""),
                                })

                    cards.append({
                        "signal_id": r[0],
                        "signal_type": r[1],
                        "severity": r[2],
                        "device_id": r[3],
                        "timestamp": r[4].isoformat() if r[4] else None,
                        "headline": headline,
                        "summary": summary,
                        "source": payload.get("source") or payload.get("feed_title") or "",
                        "source_url": payload.get("link") or payload.get("url") or "",
                        "thumbnail_url": payload.get("thumbnail_url") or payload.get("socialimage") or "",
                        "domain": r[1].split(".")[1] if "." in r[1] else "unknown",
                        "facets": facets,
                        "importance": enrich_importance,
                        "atomic_facts": enrichment_facts[:5],
                        "claims": enrichment_claims[:3],
                        "enriched_entities": enrichment_entities_display[:6],
                        "why_it_matters": (enrich_summary or "").split(" | ")[-1] if enrich_summary and " | " in (enrich_summary or "") else None,
                        "metrics": {
                            "points": payload.get("points"),
                            "comments": payload.get("num_comments"),
                            "score": payload.get("score"),
                        },
                    })

    except Exception as e:
        log.error("Grouped feed query failed: %s", e, exc_info=True)
        return {"groups": [], "error": str(e)}

    # ─── Cluster into groups ─────────────────────────────────────────
    all_groups = _cluster_by_topic(cards) if group_by == "topic" else _group_by_facet(cards, group_by)
    groups = all_groups[:limit]

    return {
        "groups": groups,
        "total_articles": len(cards),
        "total_groups": len(all_groups),
        "filters": {
            "category": category,
            "country": country,
            "person": person,
            "organization": organization,
            "event_type": event_type,
        },
        "group_by": group_by,
        "window_hours": hours,
    }


def _cluster_by_topic(cards: list[dict]) -> list[dict]:
    """Cluster cards by shared keywords into topic groups."""
    from collections import Counter, defaultdict

    if not cards:
        return []

    # Build keyword index
    kw_index: dict[str, list[int]] = defaultdict(list)
    for i, card in enumerate(cards):
        keywords = card.get("facets", {}).get("topic_keywords", [])
        for kw in keywords:
            kw_index[kw].append(i)

    assigned: set[int] = set()
    clusters: list[dict] = []

    # Sort by keyword count (richest first)
    order = sorted(range(len(cards)), key=lambda i: len(cards[i].get("facets", {}).get("topic_keywords", [])), reverse=True)

    for idx in order:
        if idx in assigned:
            continue

        card = cards[idx]
        kw_set = set(card.get("facets", {}).get("topic_keywords", []))

        # Find candidates with 2+ shared keywords
        candidates = Counter()
        for kw in kw_set:
            for other_idx in kw_index[kw]:
                if other_idx != idx and other_idx not in assigned:
                    candidates[other_idx] += 1

        cluster_indices = [idx]
        cluster_keywords = Counter(card.get("facets", {}).get("topic_keywords", []))
        assigned.add(idx)

        for other_idx, overlap in candidates.most_common():
            if overlap >= 2 and other_idx not in assigned:
                cluster_indices.append(other_idx)
                assigned.add(other_idx)
                for kw in cards[other_idx].get("facets", {}).get("topic_keywords", []):
                    cluster_keywords[kw] += 1

        cluster_cards = [cards[i] for i in cluster_indices]

        # Sort within cluster by engagement
        cluster_cards.sort(key=lambda c: (
            (c.get("metrics", {}).get("points") or 0) +
            (c.get("metrics", {}).get("score") or 0)
        ), reverse=True)

        # Representative headline and metadata
        top_card = cluster_cards[0]
        sources = list(set(c["source"] for c in cluster_cards if c["source"]))
        top_kw = [kw for kw, _ in cluster_keywords.most_common(5)]

        # Aggregate facets across cluster
        all_countries = set()
        all_people = set()
        all_orgs = set()
        categories = Counter()
        for c in cluster_cards:
            f = c.get("facets", {})
            all_countries.update(f.get("countries", []))
            all_people.update(f.get("people", []))
            all_orgs.update(f.get("organizations", []))
            if f.get("category"):
                categories[f["category"]] += 1

        # Frequency: articles per hour in this cluster
        timestamps = [c["timestamp"] for c in cluster_cards if c["timestamp"]]
        if len(timestamps) >= 2:
            newest = datetime.fromisoformat(timestamps[0])
            oldest = datetime.fromisoformat(timestamps[-1])
            span_hours = max((newest - oldest).total_seconds() / 3600, 1)
            velocity = round(len(cluster_cards) / span_hours, 1)
        else:
            velocity = 0

        # Detect conflicts within this cluster (opposing claims, credibility gaps).
        # Each article dict already carries headline + summary, which is what
        # _detect_conflicts expects.
        cluster_conflicts = None
        if _fg_detect_conflicts and len(cluster_cards) >= 2:
            try:
                cluster_conflicts = _fg_detect_conflicts(cluster_cards)
            except Exception:
                cluster_conflicts = None

        cluster = {
            "id": f"cluster_{top_card['signal_id'][:12]}",
            "headline": top_card["headline"],
            "summary": top_card["summary"],
            "source_url": top_card.get("source_url", ""),
            "conflicts": cluster_conflicts,
            "thumbnail_url": next((c["thumbnail_url"] for c in cluster_cards if c["thumbnail_url"]), ""),
            "article_count": len(cluster_cards),
            "source_count": len(sources),
            "sources": sources[:5],
            "keywords": top_kw,
            "category": categories.most_common(1)[0][0] if categories else "General",
            "event_type": top_card.get("facets", {}).get("event_type"),
            "countries": sorted(all_countries),
            "people": sorted(all_people)[:5],
            "organizations": sorted(all_orgs)[:5],
            "severity": "high" if any(c["severity"] in ("critical", "high") for c in cluster_cards) else "informational",
            "timestamp": top_card["timestamp"],
            "oldest_timestamp": cluster_cards[-1]["timestamp"] if cluster_cards else None,
            "velocity": velocity,  # articles per hour
            "articles": cluster_cards[:5],  # Top 5 for display, rest are collapsed
            "total_engagement": sum(
                (c.get("metrics", {}).get("points") or 0) +
                (c.get("metrics", {}).get("score") or 0) +
                (c.get("metrics", {}).get("comments") or 0)
                for c in cluster_cards
            ),
            # Enrichment intelligence (aggregated from cluster articles)
            "importance": max((c.get("importance") or 0 for c in cluster_cards), default=0),
            "atomic_facts": [f for c in cluster_cards for f in (c.get("atomic_facts") or [])[:3]][:8],
            "claims": [cl for c in cluster_cards for cl in (c.get("claims") or [])[:2]][:5],
            "enriched_entities": [e for c in cluster_cards for e in (c.get("enriched_entities") or [])[:3]][:8],
            "why_it_matters": next((c.get("why_it_matters") for c in cluster_cards if c.get("why_it_matters")), None),
        }
        clusters.append(cluster)

    # Sort: most sources first, then engagement
    clusters.sort(key=lambda c: (-c["source_count"], -c["total_engagement"]))
    return clusters


def _group_by_facet(cards: list[dict], facet_key: str) -> list[dict]:
    """Group cards by a single facet dimension."""
    from collections import defaultdict

    groups_map: dict[str, list[dict]] = defaultdict(list)

    for card in cards:
        facets = card.get("facets", {})
        if facet_key == "country":
            values = facets.get("countries", [])
        elif facet_key == "person":
            values = facets.get("people", [])
        elif facet_key == "organization":
            values = facets.get("organizations", [])
        elif facet_key == "category":
            values = [facets.get("category")] if facets.get("category") else []
        else:
            values = []

        for val in values:
            if val:
                groups_map[val].append(card)

    groups = []
    for value, group_cards in sorted(groups_map.items(), key=lambda x: -len(x[1])):
        group_cards.sort(key=lambda c: c.get("timestamp") or "", reverse=True)
        groups.append({
            "id": f"group_{facet_key}_{value}",
            "facet_type": facet_key,
            "facet_value": value,
            "label": value,
            "article_count": len(group_cards),
            "articles": group_cards[:5],
            "timestamp": group_cards[0]["timestamp"] if group_cards else None,
        })

    return groups[:30]


# ─── Feed Snapshots ──────────────────────────────────────────────────────


@router.get("/snapshots")
async def list_snapshots(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    period: str = Query("daily", regex="^(daily|weekly|monthly)$"),
    limit: int = Query(30, le=90),
):
    """List feed intelligence snapshots."""
    from storage.postgres.db import get_connection

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT snapshot_id, period, period_start, period_end,
                           total_articles, total_sources, total_entities, total_topics,
                           top_entities, emerged, surged, cooled, category_counts,
                           created_at
                    FROM feed_snapshots
                    WHERE tenant_id = %s AND period = %s
                    ORDER BY period_start DESC
                    LIMIT %s
                """, (auth.tenant_id, period, limit))

                snapshots = []
                for r in cur.fetchall():
                    snapshots.append({
                        "snapshot_id": r[0],
                        "period": r[1],
                        "period_start": r[2].isoformat() if r[2] else None,
                        "period_end": r[3].isoformat() if r[3] else None,
                        "total_articles": r[4],
                        "total_sources": r[5],
                        "total_entities": r[6],
                        "total_topics": r[7],
                        "top_entities": r[8] or [],
                        "emerged": r[9] or [],
                        "surged": r[10] or [],
                        "cooled": r[11] or [],
                        "category_counts": r[12] or {},
                        "created_at": r[13].isoformat() if r[13] else None,
                    })

        return {"snapshots": snapshots, "period": period}

    except Exception as e:
        log.error("Snapshot list failed: %s", e)
        return {"snapshots": [], "error": str(e)}


@router.get("/snapshots/{snapshot_id}/network")
async def get_snapshot_network(
    snapshot_id: int,
    auth: UnifiedAuthInfo = Depends(unified_auth),
    min_articles: int = Query(2, le=50),
):
    """Get co-occurrence network for a specific snapshot.
    Includes per-node conflict counts and entity_id linkage to canonical entities.
    """
    from storage.postgres.db import get_connection

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT source_name, source_type, target_name, target_type,
                           article_count, strength, prev_strength, delta,
                           sample_headline
                    FROM feed_snapshot_edges
                    WHERE snapshot_id = %s AND article_count >= %s
                    ORDER BY article_count DESC
                """, (snapshot_id, min_articles))

                edges = []
                nodes_set: dict[str, dict] = {}
                for r in cur.fetchall():
                    edges.append({
                        "source": r[0], "source_type": r[1],
                        "target": r[2], "target_type": r[3],
                        "article_count": r[4], "strength": r[5],
                        "prev_strength": r[6], "delta": r[7],
                        "sample_headline": r[8],
                    })
                    nodes_set[r[0]] = {"name": r[0], "type": r[1]}
                    nodes_set[r[2]] = {"name": r[2], "type": r[3]}

                # Enrich each node with conflict count + entity metadata
                node_names = list(nodes_set.keys())
                if node_names:
                    cur.execute("""
                        SELECT canonical_name, conflict_count, completeness_tier,
                               last_researched_at, verified_at
                        FROM entities
                        WHERE tenant_id = %s AND canonical_name = ANY(%s)
                          AND NOT (COALESCE(envelope, '{}'::jsonb) ? 'reclassification')
                    """, (auth.tenant_id, node_names))
                    for r in cur.fetchall():
                        if r[0] in nodes_set:
                            nodes_set[r[0]]["conflict_count"] = r[1] or 0
                            nodes_set[r[0]]["tier"] = r[2] or "stub"
                            nodes_set[r[0]]["verified"] = r[4] is not None
                            nodes_set[r[0]]["last_researched"] = r[3].isoformat() if r[3] else None

        return {
            "snapshot_id": snapshot_id,
            "nodes": list(nodes_set.values()),
            "edges": edges,
        }

    except Exception as e:
        log.error("Snapshot network failed: %s", e)
        return {"nodes": [], "edges": [], "error": str(e)}


@router.get("/live/network")
async def get_live_network(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    hours: int = Query(24, ge=1, le=720),
    min_articles: int = Query(1, ge=1, le=50),
):
    """Live entity co-occurrence network, computed on-the-fly from the
    knowledge graph instead of cached feed_snapshots.

    Used by the Intelligence Network view so the graph populates
    immediately as articles are ingested — no snapshot-build delay.
    Returns the same shape as /snapshots/:id/network so the frontend
    can drop this in as a replacement.

    Co-occurrence = two entities/places appear on knowledge_edges as
    `mentions` targets of the same document/article.
    """
    from storage.postgres.db import get_connection

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # 1. Fetch all distinct (article, entity/place/topic) mention
                #    pairs within the window, then pair them up per article
                #    into undirected co-occurrence edges.
                cur.execute(
                    """
                    WITH mentions AS (
                        SELECT DISTINCT ke.source_node_id AS article_id,
                               ke.target_node_id AS node_id,
                               kn.node_type, kn.subtype, kn.title
                          FROM knowledge_edges ke
                          JOIN knowledge_nodes kn ON kn.node_id = ke.target_node_id
                          JOIN knowledge_nodes art ON art.node_id = ke.source_node_id
                         WHERE ke.relationship = 'mentions'
                           AND kn.node_type IN ('entity', 'topic')
                           AND art.node_type IN ('document', 'source')
                           AND kn.tenant_id = %(tenant)s
                           AND art.tenant_id = %(tenant)s
                           AND art.created_at > NOW() - (%(hours)s || ' hours')::interval
                    ),
                    pairs AS (
                        SELECT a.node_id AS src_id, a.node_type AS src_type,
                               a.subtype AS src_sub, a.title AS src_title,
                               b.node_id AS tgt_id, b.node_type AS tgt_type,
                               b.subtype AS tgt_sub, b.title AS tgt_title,
                               COUNT(DISTINCT a.article_id) AS article_count
                          FROM mentions a
                          JOIN mentions b
                            ON a.article_id = b.article_id
                           AND a.node_id < b.node_id
                         GROUP BY a.node_id, a.node_type, a.subtype, a.title,
                                  b.node_id, b.node_type, b.subtype, b.title
                    )
                    SELECT src_id, src_type, src_sub, src_title,
                           tgt_id, tgt_type, tgt_sub, tgt_title,
                           article_count
                      FROM pairs
                     WHERE article_count >= %(min_art)s
                     ORDER BY article_count DESC
                     LIMIT 1000
                    """,
                    {"tenant": auth.tenant_id, "hours": hours, "min_art": min_articles},
                )
                rows = cur.fetchall()

                # Map envelope types → the legend's types (country/person/org).
                # place.country → country, entity.person → person, etc.
                def map_type(node_type: str, subtype: str | None) -> str:
                    """Map node_type+subtype to display category."""
                    if node_type == "topic":
                        return "topic"
                    # Everything else is 'entity' — use subtype for display
                    sub = (subtype or "").lower()
                    if sub in ("person",):
                        return "person"
                    if sub in ("organization", "org", "military_unit", "government_agency",
                               "university", "mass_media", "research_institute"):
                        return "organization"
                    if sub in ("country", "region"):
                        return "country"
                    if sub in ("city", "facility", "archaeological_site", "strait",
                               "bay", "gulf", "ocean", "mountain", "river", "lake",
                               "location"):
                        return "place"
                    if sub in ("aircraft", "helicopter", "vessel", "vehicle", "weapon"):
                        return "equipment"
                    return sub or "entity"

                edges = []
                # Dedup by normalized name — prevents "Egypt" appearing twice
                # when it exists as both entity:2888 and place:256cea6e0911
                nodes_set: dict[str, dict] = {}  # normalized_name → node data

                def _dedup_key(name: str) -> str:
                    """Normalize for dedup: lowercase, strip articles/suffixes."""
                    k = name.lower().strip()
                    # Strip leading articles
                    for prefix in ("the ", "a ", "an "):
                        if k.startswith(prefix):
                            k = k[len(prefix):]
                    return k

                def _add_node(name: str, node_id: str, node_type: str, subtype: str | None, count: int):
                    """Add or merge a node into the deduped set."""
                    key = _dedup_key(name)
                    mapped = map_type(node_type, subtype)
                    if key in nodes_set:
                        # Merge: keep the higher-count version, sum counts
                        nodes_set[key]["count"] += count
                        # Prefer entity with a real node_id over place hash
                        if node_id.startswith("entity:") and not nodes_set[key]["node_id"].startswith("entity:"):
                            nodes_set[key]["node_id"] = node_id
                    else:
                        nodes_set[key] = {
                            "name": name, "type": mapped,
                            "node_id": node_id, "count": count,
                        }

                for r in rows:
                    (src_id, src_type, src_sub, src_title,
                     tgt_id, tgt_type, tgt_sub, tgt_title,
                     article_count) = r
                    src_name = src_title or src_id
                    tgt_name = tgt_title or tgt_id
                    src_mapped = map_type(src_type, src_sub)
                    tgt_mapped = map_type(tgt_type, tgt_sub)
                    edges.append({
                        "source":      src_name, "source_type": src_mapped,
                        "target":      tgt_name, "target_type": tgt_mapped,
                        "article_count": article_count,
                        "strength":  1.0, "prev_strength": 0.0, "delta": 0.0,
                        "sample_headline": "",
                    })
                    _add_node(src_name, src_id, src_type, src_sub, article_count)
                    _add_node(tgt_name, tgt_id, tgt_type, tgt_sub, article_count)

                # Solitary entities — ALWAYS fetch, not just when co-occurrence is
                # empty. An entity mentioned in 5 articles but never co-occurring
                # with another entity should still appear as a node in the network.
                if True:
                    cur.execute(
                        """
                        SELECT kn.node_id, kn.node_type, kn.subtype, kn.title,
                               COUNT(ke.edge_id) AS article_count
                          FROM knowledge_nodes kn
                          LEFT JOIN knowledge_edges ke
                            ON ke.target_node_id = kn.node_id AND ke.relationship = 'mentions'
                         WHERE kn.tenant_id = %s
                           AND kn.node_type IN ('entity', 'topic')
                           AND kn.created_at > NOW() - (%s || ' hours')::interval
                         GROUP BY kn.node_id, kn.node_type, kn.subtype, kn.title
                         HAVING COUNT(ke.edge_id) >= %s
                         ORDER BY article_count DESC LIMIT 100
                        """,
                        (auth.tenant_id, hours, min_articles),
                    )
                    for r in cur.fetchall():
                        name = r[3] or r[0]
                        _add_node(name, r[0], r[1], r[2], r[4] or 0)

        # Enrich nodes with profile data (description, thumbnail, key fields)
        node_ids = [n["node_id"] for n in nodes_set.values() if n.get("node_id")]
        if node_ids:
            try:
                with get_connection() as conn2:
                    with conn2.cursor() as cur2:
                        placeholders = ",".join(["%s"] * len(node_ids))
                        cur2.execute(f"""
                            SELECT node_id, subtype, completeness_tier,
                                   envelope->'profile'->>'description' as description,
                                   envelope->'profile'->>'thumbnail' as thumbnail,
                                   envelope->'profile'->>'nationality' as nationality,
                                   envelope->'profile'->>'date_of_birth' as dob,
                                   envelope->'profile'->>'occupation' as occupation,
                                   envelope->'profile'->>'population' as population,
                                   envelope->'profile'->>'capital' as capital,
                                   envelope->'profile'->>'head_of_state' as head_of_state,
                                   envelope->'profile'->>'wikipedia_url' as wiki_url
                            FROM knowledge_nodes
                            WHERE node_id IN ({placeholders})
                        """, node_ids)
                        for r in cur2.fetchall():
                            nid = r[0]
                            # Find the node in nodes_set by node_id
                            for n in nodes_set.values():
                                if n.get("node_id") == nid:
                                    n["subtype"] = r[1]
                                    n["tier"] = r[2]
                                    if r[3]: n["description"] = r[3][:150]
                                    if r[4]: n["thumbnail"] = r[4]
                                    if r[5]: n["nationality"] = r[5]
                                    if r[6]: n["dob"] = r[6]
                                    if r[7]: n["occupation"] = r[7]
                                    if r[8]: n["population"] = r[8]
                                    if r[9]: n["capital"] = r[9]
                                    if r[10]: n["head_of_state"] = r[10]
                                    if r[11]: n["wiki_url"] = r[11]
                                    break
            except Exception as e:
                log.debug("Profile enrichment failed: %s", e)

        # Top entities for the Trends tab
        ranked = sorted(nodes_set.values(), key=lambda n: -(n.get("count") or 0))
        top_entities = []
        for rank, n in enumerate(ranked[:50], start=1):
            top_entities.append({
                "rank":     rank,
                "name":     n["name"],
                "type":     n["type"],
                "count":    n.get("count", 0),
                "node_id":  n.get("node_id"),
                "prev_rank": None,
            })

        return {
            "live": True,
            "window_hours": hours,
            "nodes": list(nodes_set.values()),
            "edges": edges,
            # Trend fields — live data has no historical baseline to compute
            # emerged/surged/cooled against, so those stay empty until a
            # previous snapshot exists for comparison. top_entities works now.
            "top_entities": top_entities,
            "emerged":      [],
            "surged":       [],
            "cooled":       [],
            "snapshot_id":  "live",
            "period":       "live",
            "articles":     sum(1 for _ in []),     # filled below for symmetry
        }

    except Exception as e:
        log.error("Live network failed: %s", e)
        return {"nodes": [], "edges": [], "top_entities": [], "emerged": [], "surged": [], "cooled": [], "error": str(e)}


@router.post("/snapshots/build")
async def trigger_snapshot_build(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    period: str = Query("daily", regex="^(daily|weekly|monthly|all)$"),
):
    """Manually trigger a snapshot build."""
    from core.workers.feed_snapshots import build_snapshot, build_all_snapshots

    try:
        if period == "all":
            results = build_all_snapshots(auth.tenant_id)
        else:
            results = {period: build_snapshot(period, auth.tenant_id)}
        return {"status": "complete", "results": results}
    except Exception as e:
        log.error("Snapshot build failed: %s", e)
        return {"status": "error", "error": str(e)}


@router.post("/graph/expand")
async def expand_graph_node(
    body: dict,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Expand a graph node: research if needed, return connections.

    Body: {"name": "Iran", "type": "country"}

    If entity has never been researched, triggers deep research (Wikidata,
    REST Countries, etc.) and creates entity_relationships.
    If already researched, returns existing relationships + fresh co-occurrence.
    Only adds NEW data — never re-fetches what we already have.
    """
    from storage.postgres.db import get_connection

    name = body.get("name", "")
    entity_type = body.get("type", "location")
    if not name:
        return {"error": "name required"}

    # Map feed facet types to research entity types
    type_map = {"country": "location", "person": "person", "organization": "organization"}
    research_type = type_map.get(entity_type, entity_type)

    # Check if we have existing relationships for this entity
    existing_edges = []
    entity_id = None
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Find entity (skip reclassified — they're on another substrate)
                cur.execute("""
                    SELECT entity_id FROM entities
                    WHERE tenant_id = %s AND normalized_name = LOWER(%s)
                      AND NOT (COALESCE(envelope, '{}'::jsonb) ? 'reclassification')
                    LIMIT 1
                """, (auth.tenant_id, name))
                row = cur.fetchone()
                if row:
                    entity_id = row[0]

                    # Get existing relationships
                    cur.execute("""
                        SELECT r.relationship, r.relationship_group, r.confidence,
                               r.source, r.is_current, r.article_count,
                               se.canonical_name as source_name, se.canonical_type as source_type,
                               te.canonical_name as target_name, te.canonical_type as target_type
                        FROM entity_relationships r
                        JOIN entities se ON se.entity_id = r.source_entity_id
                        JOIN entities te ON te.entity_id = r.target_entity_id
                        WHERE r.tenant_id = %s
                          AND (r.source_entity_id = %s OR r.target_entity_id = %s)
                        ORDER BY r.confidence DESC
                        LIMIT 50
                    """, (auth.tenant_id, entity_id, entity_id))

                    for r in cur.fetchall():
                        # Determine the "other" entity
                        if r[6].lower() == name.lower():
                            other_name, other_type = r[8], r[9]
                        else:
                            other_name, other_type = r[6], r[7]

                        existing_edges.append({
                            "relationship": r[0],
                            "group": r[1],
                            "confidence": r[2],
                            "source": r[3],
                            "is_current": r[4],
                            "article_count": r[5],
                            "target_name": other_name,
                            "target_type": other_type,
                        })
    except Exception as e:
        log.debug("Entity lookup failed: %s", e)

    # Freshness check — skip research if recently done
    recently_researched = False
    if entity_id:
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT MAX(created_at) FROM entity_relationships
                        WHERE tenant_id = %s AND
                              (source_entity_id = %s OR target_entity_id = %s)
                    """, (auth.tenant_id, entity_id, entity_id))
                    row = cur.fetchone()
                    if row and row[0]:
                        from datetime import datetime, timezone, timedelta
                        age = datetime.now(timezone.utc) - row[0]
                        recently_researched = age < timedelta(hours=24)
        except Exception:
            pass

    # If no existing relationships AND not recently researched, trigger research
    researched = False
    if len(existing_edges) < 3 and not recently_researched:
        try:
            if research_type == "location":
                from core.research.deep_profile import deep_research_location
                deep_research_location(name, auth.tenant_id)
                researched = True
            elif research_type == "person":
                from core.research.deep_profile import deep_research_person
                deep_research_person(name, auth.tenant_id)
                researched = True
            elif research_type == "organization":
                from core.research.deep_profile import deep_research_organization
                deep_research_organization(name, auth.tenant_id)
                researched = True
        except Exception as e:
            log.warning("Research failed for %s: %s", name, e)

        # Re-fetch relationships after research
        if researched:
            try:
                with get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT entity_id FROM entities
                            WHERE tenant_id = %s AND normalized_name = LOWER(%s)
                              AND NOT (COALESCE(envelope, '{}'::jsonb) ? 'reclassification')
                            LIMIT 1
                        """, (auth.tenant_id, name))
                        row = cur.fetchone()
                        if row:
                            entity_id = row[0]
                            cur.execute("""
                                SELECT r.relationship, r.relationship_group, r.confidence,
                                       r.source, r.is_current, r.article_count,
                                       se.canonical_name, se.canonical_type,
                                       te.canonical_name, te.canonical_type
                                FROM entity_relationships r
                                JOIN entities se ON se.entity_id = r.source_entity_id
                                JOIN entities te ON te.entity_id = r.target_entity_id
                                WHERE r.tenant_id = %s
                                  AND (r.source_entity_id = %s OR r.target_entity_id = %s)
                                ORDER BY r.confidence DESC
                                LIMIT 50
                            """, (auth.tenant_id, entity_id, entity_id))

                            existing_edges = []
                            for r in cur.fetchall():
                                if r[6].lower() == name.lower():
                                    other_name, other_type = r[8], r[9]
                                else:
                                    other_name, other_type = r[6], r[7]
                                existing_edges.append({
                                    "relationship": r[0],
                                    "group": r[1],
                                    "confidence": r[2],
                                    "source": r[3],
                                    "is_current": r[4],
                                    "article_count": r[5],
                                    "target_name": other_name,
                                    "target_type": other_type,
                                })
            except Exception:
                pass

    # ── Check for existing contact (read-only, no auto-creation) ──────────
    # Contacts are the curated layer — only created when a user explicitly
    # promotes an entity. Knowledge nodes are the automatic layer.
    contact_id = None
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT contact_id FROM contacts
                    WHERE tenant_id = %s AND LOWER(primary_name) = LOWER(%s)
                    LIMIT 1
                """, (auth.tenant_id, name))
                row = cur.fetchone()
                if row:
                    contact_id = row[0]
    except Exception as e:
        log.debug("Contact lookup failed: %s", e)

    # ── Persist research profile to contact enrichment ───────────────────
    profile = None
    if contact_id and researched:
        try:
            from services.api.src.routes.entity_profile import research_entity
            # Get the full profile from research (reuse cached data)
            profile_body = {"name": name, "entity_type": research_type}
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # We're already in an async context
                    profile_res = await research_entity(profile_body, auth.tenant_id)
                else:
                    profile_res = asyncio.run(research_entity(profile_body, auth.tenant_id))
            except RuntimeError:
                profile_res = await research_entity(profile_body, auth.tenant_id)

            if isinstance(profile_res, dict) and "profile" in profile_res:
                profile = profile_res["profile"]
                # Persist enrichment data to contact
                import json
                with get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE contacts SET
                                enrichment = %s::jsonb,
                                nationality = COALESCE(nationality, %s),
                                date_of_birth = CASE WHEN date_of_birth IS NULL AND %s != ''
                                    THEN %s::date ELSE date_of_birth END,
                                occupation = COALESCE(occupation, %s),
                                employer = COALESCE(employer, %s),
                                notes = COALESCE(NULLIF(notes, ''), '') || CASE WHEN %s != '' THEN E'\n\n' || %s ELSE '' END,
                                updated_at = NOW()
                            WHERE contact_id = %s AND tenant_id = %s
                        """, (
                            json.dumps(profile),
                            profile.get("nationality") or None,
                            profile.get("date_of_birth") or "",
                            profile.get("date_of_birth") or None,
                            profile.get("current_role") or None,
                            profile.get("organization") or None,
                            profile.get("biography") or "",
                            profile.get("biography") or "",
                            contact_id, auth.tenant_id,
                        ))
                        conn.commit()
                log.info("Persisted research profile to contact %s", contact_id)
        except Exception as e:
            log.warning("Profile persist failed for %s: %s", name, e)

    # Build graph nodes and edges for the frontend
    nodes = [{"name": name, "type": entity_type}]
    edges = []
    for edge in existing_edges:
        nodes.append({"name": edge["target_name"], "type": edge["target_type"]})
        edges.append({
            "source": name,
            "target": edge["target_name"],
            "relationship": edge["relationship"],
            "group": edge["group"],
            "confidence": edge["confidence"],
            "data_source": edge["source"],
            "article_count": edge.get("article_count"),
        })

    # Deduplicate nodes
    seen = set()
    unique_nodes = []
    for n in nodes:
        if n["name"] not in seen:
            seen.add(n["name"])
            unique_nodes.append(n)

    return {
        "name": name,
        "type": entity_type,
        "entity_id": entity_id,
        "contact_id": contact_id,
        "researched": researched,
        "profile": profile,
        "nodes": unique_nodes,
        "edges": edges,
        "total_connections": len(edges),
        "recently_researched": recently_researched,
    }


@router.get("/intelligence-brief")
async def get_intelligence_brief(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    hours: int = Query(24, le=168),
):
    """Intelligence brief — verified facts, emerging patterns, contested claims.

    Aggregates enrichment data across all articles in the time window to
    surface cross-referenced facts, entity co-occurrence patterns, and
    claims where sources disagree.
    """
    from storage.postgres.db import get_connection
    from collections import defaultdict

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)

    verified_facts = []
    patterns = []
    contested_claims = []
    key_quotes = []
    stats = {"articles": 0, "enriched": 0, "facts": 0, "claims": 0, "entities": 0}

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Count articles in window
                cur.execute("""
                    SELECT COUNT(*) FROM signals
                    WHERE tenant_id = %s AND timestamp >= %s
                      AND signal_type LIKE 'signal.content.%%'
                """, (auth.tenant_id, since))
                stats["articles"] = cur.fetchone()[0]

                # Get all enrichments in window
                cur.execute("""
                    SELECT se.signal_rid, se.entities, se.summary, se.importance
                    FROM signal_enrichments se
                    JOIN signals s ON s.rid = se.signal_rid
                    WHERE se.tenant_id = %s AND se.method = 'llm'
                      AND s.timestamp >= %s
                """, (auth.tenant_id, since))

                all_facts = defaultdict(list)      # fact_text → [signal_rids]
                all_claims = defaultdict(list)      # claim_text → [{source, confidence, rid}]
                entity_cooccurrence = defaultdict(set)  # entity_name → set of signal_rids
                quotes = []

                for row in cur.fetchall():
                    signal_rid, entities, summary, importance = row
                    stats["enriched"] += 1
                    if not entities:
                        continue

                    article_entities = []

                    for ent in entities:
                        etype = ent.get("type", "")

                        if etype == "fact":
                            fact_text = ent.get("fact", "")
                            # Filter out metadata garbage — only keep substantive facts
                            garbage_words = {"article", "published", "publication date",
                                           "author", "byline", "source", "headline",
                                           "written by", "reported by", "updated"}
                            is_garbage = any(gw in fact_text.lower() for gw in garbage_words)
                            if fact_text and len(fact_text) > 15 and not is_garbage:
                                all_facts[fact_text.lower().strip()].append({
                                    "rid": signal_rid,
                                    "value": ent.get("value", ""),
                                    "kind": ent.get("kind", "general"),
                                    "fact": fact_text,
                                })
                                stats["facts"] += 1

                        elif etype == "claim":
                            claim_text = ent.get("claim", "")
                            if claim_text and len(claim_text) > 10:
                                all_claims[claim_text.lower().strip()].append({
                                    "rid": signal_rid,
                                    "source": ent.get("source", ""),
                                    "confidence": ent.get("confidence", "medium"),
                                    "claim": claim_text,
                                })
                                stats["claims"] += 1

                        elif etype == "quote":
                            quotes.append({
                                "speaker": ent.get("name", ""),
                                "text": ent.get("text", ""),
                                "rid": signal_rid,
                            })

                        elif etype in ("person", "organization", "location"):
                            name = ent.get("name", "")
                            if name:
                                entity_cooccurrence[name].add(signal_rid)
                                article_entities.append(name)
                                stats["entities"] += 1

                # ─── Verified Facts: same fact in 2+ articles ────────
                for fact_key, occurrences in sorted(
                    all_facts.items(), key=lambda x: -len(x[1])
                ):
                    if len(occurrences) >= 2:
                        verified_facts.append({
                            "fact": occurrences[0]["fact"],
                            "value": occurrences[0]["value"],
                            "kind": occurrences[0]["kind"],
                            "source_count": len(occurrences),
                            "status": "verified",
                        })
                    elif len(occurrences) == 1:
                        # Single-source facts — include top ones by importance
                        if len(verified_facts) < 15:
                            verified_facts.append({
                                "fact": occurrences[0]["fact"],
                                "value": occurrences[0]["value"],
                                "kind": occurrences[0]["kind"],
                                "source_count": 1,
                                "status": "single_source",
                            })

                verified_facts = verified_facts[:10]

                # ─── Contested Claims: same topic, different confidence ─
                for claim_key, occurrences in sorted(
                    all_claims.items(), key=lambda x: -len(x[1])
                ):
                    sources = [o["source"] for o in occurrences if o["source"]]
                    confidences = [o["confidence"] for o in occurrences]
                    is_contested = len(set(confidences)) > 1 or "low" in confidences

                    contested_claims.append({
                        "claim": occurrences[0]["claim"],
                        "source_count": len(occurrences),
                        "sources": list(set(sources))[:4],
                        "status": "contested" if is_contested else "supported",
                        "confidence": max(confidences, key=lambda c: {"high": 3, "medium": 2, "low": 1}.get(c, 0)),
                    })

                contested_claims = sorted(
                    contested_claims,
                    key=lambda c: (0 if c["status"] == "contested" else 1, -c["source_count"])
                )[:8]

                # ─── Emerging Patterns: entity clusters ──────────────
                # Find entities mentioned in 3+ articles = a "thread"
                hot_entities = sorted(
                    entity_cooccurrence.items(),
                    key=lambda x: -len(x[1])
                )

                # Group co-occurring entities into patterns
                used_entities = set()
                for name, rids in hot_entities[:20]:
                    if name in used_entities or len(rids) < 2:
                        continue

                    # Find other entities that share articles with this one
                    related = []
                    for other_name, other_rids in hot_entities:
                        if other_name != name and other_name not in used_entities:
                            shared = rids & other_rids
                            if len(shared) >= 2:
                                related.append(other_name)
                                used_entities.add(other_name)

                    if related:
                        used_entities.add(name)
                        patterns.append({
                            "primary": name,
                            "related": related[:4],
                            "article_count": len(rids),
                            "thread": f"{name} + {' + '.join(related[:2])}",
                        })

                patterns = patterns[:6]

                # ─── Key Quotes ──────────────────────────────────────
                key_quotes = [q for q in quotes if q["text"] and len(q["text"]) > 20][:5]

    except Exception as e:
        log.error("Intelligence brief failed: %s", e, exc_info=True)
        return {"error": str(e)}

    return {
        "window_hours": hours,
        "since": since.isoformat(),
        "stats": stats,
        "verified_facts": verified_facts,
        "contested_claims": contested_claims,
        "patterns": patterns,
        "key_quotes": key_quotes,
    }
