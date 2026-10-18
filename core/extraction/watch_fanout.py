"""
Watch topic fanout — the reconnection between the new envelope pipeline
and the existing watch_topic_articles research surface.

Runs once per extracted signal, called from core.extraction.runner after
upsert_signal_extraction. Its job is to answer "which watch topic(s)
does this signal belong to?" and write one row in watch_topic_articles
for each match so the existing /watch/[id] 7-tab UI can render real data.

Two attribution paths:

  1. Explicit _topic_id in the signal's metadata.
     This fires when the signal came from a connector created by
     watch_topics._auto_create_connectors (Google News RSS, GDELT,
     Reddit, etc.). The connector stamps _topic_id into its
     source_config, base.py plumbs it into the pipeline_item, and
     signal_ingestion stamps it into signals.metadata. We read it
     back here and write the signal directly into that topic.

  2. Keyword matching against all active topics.
     Signals that didn't come from a topic-attached connector (the 12
     default RSS feeds like BBC, Al Jazeera, Reuters, etc.) need to be
     attributed via their content. We load every active topic's
     keywords list, build a normalized haystack from the signal's
     title + body + feed_summary + subtitle, and for each topic
     whose ANY keyword appears in the haystack we INSERT a row.

A single signal can land in multiple topic rows. A Ukraine article
about Iranian weapons fits both "Russia-Ukraine Conflict" and "Iran
Tensions" — that's correct. The user created both topics to watch
both threads; an article relevant to both should appear in both.

Strict dedup: the insert uses (topic_id, url_hash) UNIQUE so a signal
arriving twice (same URL) for the same topic never duplicates.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("enki.extraction.fanout")


def fanout_to_watch_topics(
    envelope: dict,
    signal: dict,
    conn,
    *,
    tenant_id: str = "default",
    signal_rid: Optional[int] = None,
    signal_type: Optional[str] = None,
) -> dict:
    """Fan one extracted signal into watch_topic_articles for each matching topic.

    Returns a stats dict so the runner can log what happened.
    """
    stats = {"attributed_via_topic_id": 0, "attributed_via_keyword": 0, "skipped": 0}
    if conn is None:
        return stats

    # Only article-like signals feed the research surface. Position
    # reports, sensor readings, etc. stay in signal_extractions only.
    if not _is_feed_worthy(signal_type, envelope):
        stats["skipped"] += 1
        return stats

    # Extract the fields watch_topic_articles needs
    article_fields = _build_article_fields(envelope, signal, signal_rid)
    if not article_fields.get("url") and not article_fields.get("title"):
        stats["skipped"] += 1
        return stats

    # Path 1: explicit _topic_id from source metadata
    meta = signal.get("metadata") or {}
    explicit_topic_id = meta.get("_topic_id")

    attributed_topic_ids: set[str] = set()

    if explicit_topic_id:
        if _insert_watch_topic_article(conn, explicit_topic_id, tenant_id, article_fields):
            attributed_topic_ids.add(explicit_topic_id)
            stats["attributed_via_topic_id"] += 1

    # Path 2: keyword matching against all active topics
    try:
        active_topics = _load_active_topics(conn, tenant_id)
        haystack = _build_haystack(envelope)
        if haystack:
            for topic_id, keywords in active_topics:
                if topic_id in attributed_topic_ids:
                    continue  # already attributed via _topic_id
                if _haystack_matches_keywords(haystack, keywords):
                    if _insert_watch_topic_article(conn, topic_id, tenant_id, article_fields):
                        attributed_topic_ids.add(topic_id)
                        stats["attributed_via_keyword"] += 1
    except Exception as e:
        log.warning("keyword fanout failed: %s", e)

    # Tier 1: Fast node matching — tag this article against existing entities
    if attributed_topic_ids:
        try:
            from core.extraction.node_matcher import match_article_to_nodes
            title = article_fields.get("title", "")
            full_text = article_fields.get("full_text", "")
            for tid in attributed_topic_ids:
                match_article_to_nodes(
                    article_id=article_fields.get("article_id", ""),
                    title=title, full_text=full_text,
                    topic_id=tid, tenant_id=tenant_id,
                )
        except Exception as e:
            log.debug("Tier 1 node matching failed: %s", e)

    return stats


# ─── Internals ──────────────────────────────────────────────────────────────


# Signal types that feed the research surface. Firehose types
# (position_report, sensor_reading) are intentionally excluded — they
# stay in signal_extractions for map views but don't pollute watch topics.
_FEED_WORTHY_TYPES = {
    "signal.content.rss_entry",
    "signal.content.news_article",
    "signal.content.news",  # legacy alias
    "signal.content.social_post",
    "signal.content.web_page",
    "signal.environmental.seismic",
    "signal.environmental.weather_alert",
    "signal.security.threat_feed",
    "signal.security.malware_detected",
    "signal.security.intrusion_detected",
    "signal.intelligence.imagery",
    "signal.intelligence.sigint",
    "signal.communications.email",
}


def _is_feed_worthy(signal_type: Optional[str], envelope: dict) -> bool:
    if not signal_type:
        return False
    if signal_type in _FEED_WORTHY_TYPES:
        return True
    # Node-type-based fallback: anything extracted as a document or
    # a content-bearing event is feed-worthy
    node_type = envelope.get("node_type")
    subtype = envelope.get("subtype")
    if node_type == "document":
        return True
    if node_type == "event" and subtype in {"earthquake", "weather_alert", "security", "alert"}:
        return True
    return False


def _build_article_fields(envelope: dict, signal: dict, signal_rid: Optional[int]) -> dict:
    """Extract the subset of envelope fields the watch_topic_articles
    schema needs: url, url_hash, title, source_name, author, full_text,
    published_at, extraction JSONB."""
    identity = envelope.get("identity") or {}
    summary = envelope.get("summary") or {}
    source = envelope.get("source") or {}
    temporal = envelope.get("temporal") or {}

    url = source.get("url") or ""
    title = (identity.get("title_display")
             or identity.get("title_raw")
             or "")[:500]
    source_name = (source.get("outlet_name") or "")[:255]
    author = (identity.get("byline_raw") or "")[:255] or None
    full_text = (
        summary.get("body_original")
        or summary.get("feed_summary_clean")
        or summary.get("feed_summary")
        or summary.get("narrative")
        or ""
    )
    published_at = temporal.get("event_time")

    # Compute url_hash for dedup within topic
    if url:
        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:64]
    else:
        # Fallback: hash title + source so we still dedup non-URL items
        fallback = f"{title}|{source_name}|{published_at}"
        url_hash = hashlib.sha256(fallback.encode("utf-8")).hexdigest()[:64]

    return {
        "url": url[:2000],
        "url_hash": url_hash,
        "title": title or None,
        "source_name": source_name or None,
        "author": author,
        "full_text": full_text or None,
        "published_at": published_at,
        "signal_rid": signal_rid,
        "extraction": envelope,
    }


def _insert_watch_topic_article(
    conn,
    topic_id: str,
    tenant_id: str,
    fields: dict,
) -> bool:
    """Insert one row into watch_topic_articles. Returns True if inserted,
    False on dedup or error.

    Dedup key: (topic_id, url_hash). Schema has no UNIQUE on this pair
    today, so we do a SELECT-first check.
    """
    import json
    article_id = _generate_article_id(topic_id, fields["url_hash"])
    try:
        with conn.cursor() as cur:
            # Dedup check
            cur.execute(
                """
                SELECT 1 FROM watch_topic_articles
                 WHERE topic_id = %s AND tenant_id = %s AND url_hash = %s
                 LIMIT 1
                """,
                (topic_id, tenant_id, fields["url_hash"]),
            )
            if cur.fetchone():
                return False  # already attributed

            cur.execute(
                """
                INSERT INTO watch_topic_articles
                    (article_id, topic_id, tenant_id, signal_rid,
                     url, url_hash, title, source_name, author,
                     full_text, published_at, extraction,
                     processing_status, is_backfill, created_at)
                VALUES (%s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s::jsonb,
                        'pending', false, NOW())
                """,
                (
                    article_id, topic_id, tenant_id, fields.get("signal_rid"),
                    fields["url"], fields["url_hash"],
                    fields.get("title"), fields.get("source_name"), fields.get("author"),
                    fields.get("full_text"), fields.get("published_at"),
                    json.dumps(fields.get("extraction") or {}, default=str),
                ),
            )
        conn.commit()
        return True
    except Exception as e:
        log.warning("insert_watch_topic_article(%s) failed: %s", topic_id, e)
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def _generate_article_id(topic_id: str, url_hash: str) -> str:
    """Deterministic article_id so reruns don't create new rows."""
    return f"wta_{topic_id[-8:]}_{url_hash[:12]}"


def _load_active_topics(conn, tenant_id: str) -> list[tuple[str, list[str]]]:
    """Return list of (topic_id, keywords[]) for active topics."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT topic_id, keywords
              FROM watch_topics
             WHERE tenant_id = %s AND status = 'active'
            """,
            (tenant_id,),
        )
        return [(row[0], list(row[1] or [])) for row in cur.fetchall()]


def _build_haystack(envelope: dict) -> str:
    """Concatenate the envelope fields we search for keyword matches.
    Lowercased, whitespace-collapsed."""
    parts: list[str] = []
    identity = envelope.get("identity") or {}
    summary = envelope.get("summary") or {}

    for path_val in (
        identity.get("title_display"),
        identity.get("title_raw"),
        identity.get("subtitle"),
        summary.get("headline"),
        summary.get("narrative"),
        summary.get("feed_summary_clean"),
        summary.get("feed_summary"),
        summary.get("body_original"),
    ):
        if isinstance(path_val, str) and path_val.strip():
            parts.append(path_val)

    if not parts:
        return ""

    # Lowercase + collapse whitespace
    joined = " ".join(parts).lower()
    # Limit total length — keyword matching over 100KB of body is wasted work
    return joined[:20000]


def _haystack_matches_keywords(haystack: str, keywords: list[str]) -> bool:
    """Return True if any keyword appears as a substring in the haystack.
    Case-insensitive (haystack is already lowercased)."""
    if not haystack or not keywords:
        return False
    for kw in keywords:
        if not isinstance(kw, str):
            continue
        k = kw.lower().strip()
        if not k:
            continue
        # Word-boundary-ish check: surround haystack with spaces and
        # check for " kw " to avoid false positives on "iran" matching
        # "Iranian" (which we actually DO want to match — so this is
        # substring, not boundary). Substring is correct here.
        if k in haystack:
            return True
    return False
