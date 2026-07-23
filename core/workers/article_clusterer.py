"""
Article Collapse Worker — groups related articles into story clusters.

Runs as a daemon thread. Finds watch_topic_articles without a cluster_id
and assigns them to existing clusters or creates new ones.

Clustering strategy:
  1. Extract keywords from article title
  2. Compare against all existing clusters for the same topic
  3. If 3+ keyword overlap → assign to that cluster, update cluster stats
  4. If no match → create a new cluster from this article
  5. Cross-day: clusters span multiple days (same story, different dates)

This replaces per-day clustering in the timeline assembly. The timeline
now just reads pre-computed clusters.

Worker pattern matches existing workers (feed_enrichment, node_builder).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.article_clusterer")

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "articles_clustered": 0,
    "clusters_created": 0,
    "clusters_merged": 0,
    "errors": 0,
}

CHECK_INTERVAL = 30
BATCH_SIZE = 50
MIN_KEYWORD_OVERLAP = 3

# Stop words for keyword extraction
_STOP = frozenset([
    "the", "a", "an", "in", "of", "to", "for", "and", "is", "on", "at",
    "by", "with", "from", "has", "was", "are", "that", "this", "it", "as",
    "be", "or", "but", "not", "its", "new", "how", "what", "why", "who",
    "more", "about", "after", "says", "said", "will", "can", "may", "could",
    "been", "into", "over", "than", "also", "just", "now", "out", "all",
    "up", "one", "two", "first", "last", "most", "very", "between",
    "through", "during", "before", "while", "where", "under",
])


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_worker_loop, args=(tenant_id,),
        name="article-clusterer", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    _STATUS["phase"] = "starting"
    log.info("Article clusterer started")
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
            processed = _process_batch(tenant_id)
            if processed > 0:
                backoff = CHECK_INTERVAL
                _STATUS["phase"] = "processing"
            else:
                backoff = min(backoff + 15, 180)
                _STATUS["phase"] = "idle — no unclustered articles"
        except Exception as e:
            log.warning("Article clusterer error: %s", e)
            _STATUS["errors"] += 1
            _STATUS["phase"] = f"error: {str(e)[:60]}"
            backoff = min(backoff * 2, 300)

        for _ in range(int(backoff)):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)

    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"


def _process_batch(tenant_id: str) -> int:
    """Find unclustered articles and assign them to story clusters."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Find topics with unclustered articles
            cur.execute("""
                SELECT DISTINCT topic_id
                FROM watch_topic_articles
                WHERE tenant_id = %s AND cluster_id IS NULL
                  AND title IS NOT NULL AND LENGTH(title) > 10
                LIMIT 5
            """, (tenant_id,))
            topics = [r[0] for r in cur.fetchall()]

    if not topics:
        return 0

    processed = 0
    for topic_id in topics:
        try:
            count = _cluster_topic(topic_id, tenant_id)
            processed += count
        except Exception as e:
            log.warning("Clustering failed for topic %s: %s", topic_id, e)
            _STATUS["errors"] += 1

    _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
    return processed


def _cluster_topic(topic_id: str, tenant_id: str) -> int:
    """Cluster all unclustered articles for a topic against existing clusters."""
    _STATUS["phase"] = f"clustering {topic_id}"

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Load existing clusters with their keywords
            cur.execute("""
                SELECT cluster_id, headline, keywords, article_count
                FROM watch_topic_clusters
                WHERE topic_id = %s AND tenant_id = %s
                ORDER BY article_count DESC
            """, (topic_id, tenant_id))

            existing_clusters: list[dict] = []
            for r in cur.fetchall():
                kw = r[2] if r[2] else []
                if not kw:
                    kw = list(_extract_keywords(r[1] or ""))
                existing_clusters.append({
                    "cluster_id": r[0],
                    "headline": r[1],
                    "keywords": set(kw),
                    "article_count": r[3] or 1,
                })

            # Load unclustered articles
            # Entity fields live inside the extraction JSONB, not as columns
            cur.execute("""
                SELECT article_id, title, source_name, published_at, url, extraction
                FROM watch_topic_articles
                WHERE topic_id = %s AND tenant_id = %s
                  AND cluster_id IS NULL
                  AND title IS NOT NULL AND LENGTH(title) > 10
                ORDER BY published_at DESC
                LIMIT %s
            """, (topic_id, tenant_id, BATCH_SIZE))

            unclustered = []
            for r in cur.fetchall():
                extraction = r[5] or {}
                if isinstance(extraction, str):
                    try:
                        extraction = json.loads(extraction)
                    except Exception:
                        extraction = {}
                unclustered.append({
                    "article_id": r[0],
                    "title": r[1],
                    "source_name": r[2],
                    "published_at": r[3],
                    "url": r[4],
                    "people": extraction.get("people", []),
                    "organizations": extraction.get("organizations", []),
                    "countries": extraction.get("countries", []),
                    "locations": extraction.get("locations", []),
                    "keywords": _extract_keywords(r[1] or ""),
                })

            if not unclustered:
                return 0

            assigned = 0

            for art in unclustered:
                best_match = None
                best_overlap = 0

                # Compare against existing clusters
                for cluster in existing_clusters:
                    overlap = len(art["keywords"] & cluster["keywords"])
                    if overlap >= MIN_KEYWORD_OVERLAP and overlap > best_overlap:
                        best_match = cluster
                        best_overlap = overlap

                if best_match:
                    # Assign to existing cluster
                    _assign_to_cluster(cur, art, best_match, topic_id, tenant_id)
                    best_match["article_count"] += 1
                    # Add article's keywords to cluster for future matching
                    best_match["keywords"] |= art["keywords"]
                    _STATUS["articles_clustered"] += 1
                else:
                    # Create new cluster from this article
                    new_cluster = _create_cluster(cur, art, topic_id, tenant_id)
                    existing_clusters.append(new_cluster)
                    _STATUS["clusters_created"] += 1
                    _STATUS["articles_clustered"] += 1

                assigned += 1

            conn.commit()

    log.info("Clustered %d articles for topic %s (%d clusters total)",
             assigned, topic_id, len(existing_clusters))
    return assigned


def _assign_to_cluster(cur, article: dict, cluster: dict,
                       topic_id: str, tenant_id: str):
    """Assign an article to an existing cluster and update cluster stats."""
    cluster_id = cluster["cluster_id"]

    # Update article
    cur.execute("""
        UPDATE watch_topic_articles
        SET cluster_id = %s, processing_status = 'clustered'
        WHERE article_id = %s
    """, (cluster_id, article["article_id"]))

    # Merge entities into cluster
    new_people = list(set(
        (cluster.get("_people") or []) + (article.get("people") or [])
    ))[:10]
    new_orgs = list(set(
        (cluster.get("_orgs") or []) + (article.get("organizations") or [])
    ))[:10]
    new_locations = list(set(
        (cluster.get("_locations") or []) + (article.get("locations") or [])
    ))[:10]

    # Count distinct sources
    cur.execute("""
        SELECT COUNT(DISTINCT source_name)
        FROM watch_topic_articles
        WHERE cluster_id = %s AND source_name IS NOT NULL
    """, (cluster_id,))
    source_count = cur.fetchone()[0] or 1

    # Update earliest event_date if this article is older
    cur.execute("""
        SELECT MIN(published_at) FROM watch_topic_articles WHERE cluster_id = %s
    """, (cluster_id,))
    earliest = cur.fetchone()[0]

    # Update cluster
    keywords_list = list(cluster["keywords"] | article["keywords"])
    cur.execute("""
        UPDATE watch_topic_clusters
        SET article_count = article_count + 1,
            source_count = %s,
            people = %s::jsonb,
            organizations = %s::jsonb,
            locations = %s::jsonb,
            keywords = %s,
            event_date = LEAST(event_date, %s),
            updated_at = NOW()
        WHERE cluster_id = %s
    """, (
        source_count,
        json.dumps(new_people),
        json.dumps(new_orgs),
        json.dumps(new_locations),
        keywords_list[:20],
        earliest,
        cluster_id,
    ))


def _create_cluster(cur, article: dict, topic_id: str,
                     tenant_id: str) -> dict:
    """Create a new cluster from a single article."""
    cluster_id = f"clust_{hashlib.sha1(article['title'].lower().encode()).hexdigest()[:12]}"
    keywords_list = list(article["keywords"])[:20]
    event_date = article["published_at"].date() if article.get("published_at") else None

    try:
        cur.execute("""
            INSERT INTO watch_topic_clusters
                (cluster_id, topic_id, tenant_id, headline, event_type,
                 event_date, source_count, article_count,
                 people, organizations, locations, keywords)
            VALUES (%s, %s, %s, %s, 'other', %s, 1, 1, %s, %s, %s, %s)
            ON CONFLICT (cluster_id) DO UPDATE SET
                article_count = watch_topic_clusters.article_count + 1,
                updated_at = NOW()
        """, (
            cluster_id, topic_id, tenant_id,
            article["title"][:500],
            event_date,
            json.dumps(article.get("people", [])[:5]),
            json.dumps(article.get("organizations", [])[:5]),
            json.dumps(article.get("locations", [])[:5]),
            keywords_list,
        ))
    except Exception as e:
        log.debug("Cluster insert failed: %s", e)
        return {"cluster_id": cluster_id, "headline": article["title"],
                "keywords": article["keywords"], "article_count": 1}

    # Assign article to this cluster
    cur.execute("""
        UPDATE watch_topic_articles
        SET cluster_id = %s, processing_status = 'clustered'
        WHERE article_id = %s
    """, (cluster_id, article["article_id"]))

    return {
        "cluster_id": cluster_id,
        "headline": article["title"],
        "keywords": article["keywords"],
        "article_count": 1,
    }


def _extract_keywords(text: str) -> set[str]:
    """Extract significant keywords from text for clustering."""
    words = re.sub(r"[^a-z0-9\s]", "", text.lower()).split()
    return {w for w in words if len(w) >= 3 and w not in _STOP}


# ─── One-shot: recluster entire topic ────────────────────────────────────────

def recluster_topic(topic_id: str, tenant_id: str = "default") -> dict:
    """Clear all clusters for a topic and rebuild from scratch.

    Used when clustering needs a fresh start (e.g., after changing
    parameters or fixing bad data).
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Clear existing cluster assignments
            cur.execute("""
                UPDATE watch_topic_articles
                SET cluster_id = NULL, processing_status = 'extracted'
                WHERE topic_id = %s AND tenant_id = %s
            """, (topic_id, tenant_id))

            # Delete existing clusters
            cur.execute("""
                DELETE FROM watch_topic_clusters
                WHERE topic_id = %s AND tenant_id = %s
            """, (topic_id, tenant_id))

        conn.commit()

    # Now cluster from scratch
    count = _cluster_topic(topic_id, tenant_id)
    return {"topic_id": topic_id, "articles_clustered": count}
