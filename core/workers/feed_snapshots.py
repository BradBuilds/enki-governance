"""
Feed Intelligence Snapshots — periodic rollups of feed data for pattern detection.

Captures the full network state at a point in time:
  - Entity mention counts with rank changes
  - Co-occurrence edges between entities (who appeared with whom)
  - Topic clusters with velocity
  - Emergence / surge / cooling detection

Runs as a scheduled job: daily at midnight, weekly on Sunday, monthly on 1st.
Reads ONLY from signals.facets — never touches research/entity tables.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger("enki.workers.feed_snapshots")


def build_snapshot(
    period: str = "daily",
    tenant_id: str = "default",
    dsn: str | None = None,
) -> dict:
    """Build a feed intelligence snapshot for the given period.

    Args:
        period: 'daily' (24h), 'weekly' (7d), or 'monthly' (30d)

    Returns summary dict with counts.
    """
    from storage.postgres.db import get_connection

    now = datetime.now(timezone.utc)
    if period == "daily":
        period_start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        period_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "weekly":
        # Last 7 complete days
        period_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        period_start = period_end - timedelta(days=7)
    elif period == "monthly":
        period_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        period_start = period_end - timedelta(days=30)
    else:
        raise ValueError(f"Unknown period: {period}")

    # Prior period for comparison
    prior_duration = period_end - period_start
    prior_start = period_start - prior_duration
    prior_end = period_start

    log.info("Building %s snapshot: %s to %s", period, period_start, period_end)

    # ─── Fetch articles with facets ──────────────────────────────────
    articles = []
    noise_types = (
        "signal.geospatial.aircraft_position",
        "signal.geospatial.vessel_position",
        "signal.geospatial.position_report",
        "signal.device.sensor_reading",
        "signal.infrastructure.sensor_reading",
    )

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Current period articles
            placeholders = ",".join(["%s"] * len(noise_types))
            cur.execute(f"""
                SELECT signal_id, payload, facets, metadata, timestamp
                FROM signals
                WHERE tenant_id = %s
                  AND timestamp >= %s AND timestamp < %s
                  AND signal_type NOT IN ({placeholders})
                  AND (metadata->>'_topic_id' IS NULL OR metadata->>'_topic_id' = '')
                ORDER BY timestamp DESC
                LIMIT 5000
            """, (tenant_id, period_start, period_end, *noise_types))

            for r in cur.fetchall():
                payload = r[1] or {}
                facets = r[2] or {}
                metadata = r[3] or {}
                # Fall back to metadata-embedded facets
                if not facets or facets == {}:
                    facets = metadata.get("facets", {})

                headline = payload.get("title") or payload.get("headline") or ""
                if not headline or len(headline) < 10:
                    continue

                source = payload.get("source") or payload.get("feed_title") or ""
                articles.append({
                    "signal_id": r[0],
                    "headline": headline,
                    "source": source,
                    "facets": facets,
                    "timestamp": r[4].isoformat() if r[4] else None,
                })

            # Prior period entity counts (for comparison)
            cur.execute(f"""
                SELECT facets
                FROM signals
                WHERE tenant_id = %s
                  AND timestamp >= %s AND timestamp < %s
                  AND signal_type NOT IN ({placeholders})
                  AND (metadata->>'_topic_id' IS NULL OR metadata->>'_topic_id' = '')
                  AND facets IS NOT NULL AND facets != '{{}}'::jsonb
                LIMIT 5000
            """, (tenant_id, prior_start, prior_end, *noise_types))

            prior_entity_counts: Counter = Counter()
            for r in cur.fetchall():
                f = r[0] or {}
                for code in f.get("countries", []):
                    prior_entity_counts[("country", code)] += 1
                for p in f.get("people", []):
                    prior_entity_counts[("person", p)] += 1
                for o in f.get("organizations", []):
                    prior_entity_counts[("organization", o)] += 1

    log.info("Fetched %d articles for %s snapshot", len(articles), period)

    # ─── Compute entity counts ───────────────────────────────────────
    entity_counts: Counter = Counter()
    entity_labels: dict[tuple[str, str], str] = {}
    sources: set = set()
    category_counts: Counter = Counter()

    for art in articles:
        f = art["facets"]
        if art["source"]:
            sources.add(art["source"])

        cat = f.get("category", "General")
        category_counts[cat] += 1

        country_labels = f.get("country_labels", {})
        for code in f.get("countries", []):
            entity_counts[("country", code)] += 1
            entity_labels[("country", code)] = country_labels.get(code, code)
        for p in f.get("people", []):
            entity_counts[("person", p)] += 1
            entity_labels[("person", p)] = p
        for o in f.get("organizations", []):
            entity_counts[("organization", o)] += 1
            entity_labels[("organization", o)] = o

    # Build ranked entity list with trend comparison
    top_entities = []
    current_ranked = entity_counts.most_common(50)

    # Build prior ranks for comparison
    prior_ranked = prior_entity_counts.most_common(100)
    prior_rank_map = {key: i + 1 for i, (key, _) in enumerate(prior_ranked)}
    prior_count_map = dict(prior_entity_counts)

    for rank, ((etype, evalue), count) in enumerate(current_ranked, 1):
        prev_count = prior_count_map.get((etype, evalue), 0)
        prev_rank = prior_rank_map.get((etype, evalue))

        if prev_count == 0:
            trend = "new"
            delta_pct = 100
        elif count > prev_count * 1.5:
            trend = "up"
            delta_pct = round(((count - prev_count) / prev_count) * 100)
        elif count < prev_count * 0.5:
            trend = "down"
            delta_pct = round(((count - prev_count) / prev_count) * 100)
        else:
            trend = "stable"
            delta_pct = round(((count - prev_count) / max(prev_count, 1)) * 100)

        top_entities.append({
            "name": entity_labels.get((etype, evalue), evalue),
            "type": etype,
            "value": evalue,
            "count": count,
            "rank": rank,
            "prev_rank": prev_rank,
            "prev_count": prev_count,
            "trend": trend,
            "delta_pct": delta_pct,
        })

    # ─── Emergence / surge / cooling ─────────────────────────────────
    emerged = [e for e in top_entities if e["trend"] == "new" and e["count"] >= 3]
    surged = [e for e in top_entities if e["trend"] == "up" and e["delta_pct"] >= 50]
    cooled = []
    for (etype, evalue), prev_count in prior_entity_counts.most_common(30):
        curr_count = entity_counts.get((etype, evalue), 0)
        if prev_count >= 5 and curr_count < prev_count * 0.5:
            cooled.append({
                "name": entity_labels.get((etype, evalue), evalue),
                "type": etype,
                "prev_count": prev_count,
                "count": curr_count,
                "delta_pct": round(((curr_count - prev_count) / prev_count) * 100),
            })

    # ─── Co-occurrence edges ─────────────────────────────────────────
    cooccurrence: Counter = Counter()
    edge_samples: dict = {}

    for art in articles:
        f = art["facets"]
        # Collect all entities in this article
        entities = []
        for code in f.get("countries", []):
            entities.append(("country", code, entity_labels.get(("country", code), code)))
        for p in f.get("people", []):
            entities.append(("person", p, p))
        for o in f.get("organizations", []):
            entities.append(("organization", o, o))

        # Pairwise co-occurrence
        for i in range(len(entities)):
            for j in range(i + 1, len(entities)):
                e1, e2 = entities[i], entities[j]
                # Canonical ordering
                key = (min(e1[:2], e2[:2]), max(e1[:2], e2[:2]))
                cooccurrence[key] += 1
                if key not in edge_samples:
                    edge_samples[key] = (art["headline"][:200], e1[2], e2[2], e1[0], e2[0])

    # Build prior co-occurrence for delta
    # (simplified — just compare counts)
    top_edges = cooccurrence.most_common(100)

    # ─── Topic clusters (simplified from facets/grouped) ─────────────
    # Use keyword overlap for clustering
    from storage.signals.facets import extract_keywords, _STOPWORDS
    import re

    keyword_index: dict[str, list[int]] = defaultdict(list)
    for i, art in enumerate(articles):
        kws = art["facets"].get("topic_keywords", [])
        for kw in kws:
            keyword_index[kw].append(i)

    assigned: set = set()
    clusters = []
    order = sorted(range(len(articles)),
                   key=lambda i: len(articles[i]["facets"].get("topic_keywords", [])),
                   reverse=True)

    for idx in order:
        if idx in assigned:
            continue
        art = articles[idx]
        kw_set = set(art["facets"].get("topic_keywords", []))
        candidates: Counter = Counter()
        for kw in kw_set:
            for other_idx in keyword_index[kw]:
                if other_idx != idx and other_idx not in assigned:
                    candidates[other_idx] += 1

        cluster_indices = [idx]
        assigned.add(idx)
        for other_idx, overlap in candidates.most_common():
            if overlap >= 2 and other_idx not in assigned:
                cluster_indices.append(other_idx)
                assigned.add(other_idx)

        if len(cluster_indices) >= 2:
            cluster_arts = [articles[i] for i in cluster_indices]
            cluster_sources = set(a["source"] for a in cluster_arts if a["source"])
            clusters.append({
                "headline": cluster_arts[0]["headline"],
                "article_count": len(cluster_arts),
                "source_count": len(cluster_sources),
                "category": cluster_arts[0]["facets"].get("category", "General"),
                "keywords": list(kw_set)[:5],
            })

    clusters.sort(key=lambda c: -c["article_count"])

    # ─── Write snapshot ──────────────────────────────────────────────
    snapshot_id = None
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO feed_snapshots
                    (tenant_id, period, period_start, period_end,
                     total_articles, total_sources, total_entities, total_topics,
                     top_entities, top_clusters, emerged, surged, cooled,
                     category_counts)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, period, period_start)
                DO UPDATE SET
                    total_articles = EXCLUDED.total_articles,
                    top_entities = EXCLUDED.top_entities,
                    top_clusters = EXCLUDED.top_clusters,
                    emerged = EXCLUDED.emerged,
                    surged = EXCLUDED.surged,
                    cooled = EXCLUDED.cooled,
                    category_counts = EXCLUDED.category_counts,
                    created_at = NOW()
                RETURNING snapshot_id
            """, (
                tenant_id, period, period_start, period_end,
                len(articles), len(sources), len(entity_counts), len(clusters),
                json.dumps(top_entities[:30]),
                json.dumps(clusters[:20]),
                json.dumps(emerged[:10]),
                json.dumps(surged[:10]),
                json.dumps(cooled[:10]),
                json.dumps(dict(category_counts)),
            ))
            snapshot_id = cur.fetchone()[0]

            # Write co-occurrence edges (top 200)
            cur.execute("DELETE FROM feed_snapshot_edges WHERE snapshot_id = %s", (snapshot_id,))

            for (e1_key, e2_key), count in top_edges[:200]:
                if count < 2:
                    continue
                sample = edge_samples.get((e1_key, e2_key))
                if not sample:
                    continue
                headline, name1, name2, type1, type2 = sample
                strength = min(count / max(len(articles), 1) * 10, 1.0)

                cur.execute("""
                    INSERT INTO feed_snapshot_edges
                        (snapshot_id, source_name, source_type, target_name, target_type,
                         article_count, strength, sample_headline)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    snapshot_id, name1, type1, name2, type2,
                    count, round(strength, 3), headline,
                ))

        conn.commit()

    summary = {
        "snapshot_id": snapshot_id,
        "period": period,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "articles": len(articles),
        "entities": len(entity_counts),
        "clusters": len(clusters),
        "edges": min(len(top_edges), 200),
        "emerged": len(emerged),
        "surged": len(surged),
        "cooled": len(cooled),
    }
    log.info("Snapshot built: %s", json.dumps(summary))
    return summary


def build_all_snapshots(tenant_id: str = "default", dsn: str | None = None):
    """Build daily, weekly, and monthly snapshots."""
    results = {}
    for period in ("daily", "weekly", "monthly"):
        try:
            results[period] = build_snapshot(period, tenant_id, dsn)
        except Exception as e:
            log.error("Failed to build %s snapshot: %s", period, e)
            results[period] = {"error": str(e)}
    return results
