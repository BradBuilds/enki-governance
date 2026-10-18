"""
Signal clusterer — collapse duplicate news stories at the substrate level.

Same story across many sources. AP, Reuters, BBC, Axios, Fox all post
"Vale warns Iran clock is ticking" within the same hour. Without
clustering, the feed shows 6 separate items. With clustering: one
cluster "Vale-Iran clock ticking warning · 6 articles · 6 sources".

Distinct from the existing watch_topic-scoped `article_clusterer`:
this one works on the universal `signals` table (every news-typed
signal regardless of topic membership). It populates `signal_clusters`
and sets `signals.cluster_id`.

Matching strategy per signal:
  1. Tokenize the title (drop stopwords, source-suffix patterns)
  2. Pull entity_ids mentioned in this signal (from entity_mentions)
  3. Compute fingerprint = sha1(top_entities + day_bucket + top_tokens)
  4. Look for existing cluster within ±36 hours:
       a. exact fingerprint match → cheap path
       b. entity overlap (Jaccard) ≥ 0.5 AND ≥2 shared keywords → fuzzy
  5. Attach if matched; else create new cluster with this signal as canonical

Public surface:
  cluster_one(signal_rid) -> dict
  run_batch(limit, max_age_hours) -> dict
  start_worker / stop_worker / get_status   (supervisor-compatible)
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.signal_clusterer")

_STOP_EVENT = threading.Event()
_WORKER_THREAD: Optional[threading.Thread] = None

_STATUS = {
    "running": False, "phase": "stopped",
    "processed": 0, "clustered_into_existing": 0,
    "new_clusters": 0, "skipped": 0, "errors": 0,
    "last_run": None,
}

TICK_INTERVAL = 30
BATCH_SIZE = 100
MATCH_WINDOW_HOURS = 36

_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "to", "of", "in", "on", "at",
    "for", "with", "by", "from", "as", "is", "are", "was", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "can", "could", "may", "might", "shall", "should", "must",
    "this", "that", "these", "those", "it", "its", "he", "she", "they",
    "them", "their", "his", "her", "him", "we", "us", "our", "you", "your",
    "i", "my", "me", "what", "which", "who", "whom", "where", "when",
    "why", "how", "not", "no", "so", "if", "than", "then", "out", "up",
    "down", "off", "over", "after", "before", "during", "into", "onto",
    "between", "amid", "via", "say", "says", "said",
    "vs", "vs.", "amp", "live", "update", "updates", "breaking", "latest",
    "top", "news", "report", "reports", "main", "english", "stories",
    "new", "old",
})


def _tokenize_title(title: str) -> list[str]:
    if not title:
        return []
    s = re.split(r"\s+[—\-:|]\s+", title)[0]  # drop source suffix
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9'’]+", s.lower())
    return [t for t in tokens if t not in _STOPWORDS and len(t) >= 2]


def _day_bucket(ts) -> str:
    return ts.strftime("%Y-%m-%d") if ts else ""


def _fingerprint(entity_ids: list[int], tokens: list[str], when) -> str:
    ents = sorted(entity_ids)[:3]
    toks = sorted(tokens)[:5]
    raw = f"{','.join(map(str, ents))}|{_day_bucket(when)}|{','.join(toks)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _entity_ids_for_signal(conn, signal_rid: int) -> list[int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT entity_id FROM entity_mentions "
            " WHERE signal_rid = %s AND entity_id IS NOT NULL LIMIT 20",
            (signal_rid,),
        )
        return [r[0] for r in cur.fetchall() if r[0]]


def _find_matching_cluster(
    conn, *, tenant_id: str, when, entity_ids: list[int],
    tokens: list[str], fingerprint: str,
) -> Optional[int]:
    if when is None:
        return None
    with conn.cursor() as cur:
        # Pass 1: exact fingerprint match
        cur.execute(
            """
            SELECT cluster_id FROM signal_clusters
             WHERE tenant_id = %s
               AND fingerprint_hash = %s
               AND last_seen > %s - INTERVAL '36 hours'
               AND first_seen < %s + INTERVAL '36 hours'
             ORDER BY last_seen DESC LIMIT 1
            """,
            (tenant_id, fingerprint, when, when),
        )
        r = cur.fetchone()
        if r:
            return r[0]
        # Pass 2a: entity overlap (when both have entities)
        candidates: list = []
        if entity_ids and tokens:
            cur.execute(
                """
                SELECT cluster_id, entity_ids, title_keywords
                  FROM signal_clusters
                 WHERE tenant_id = %s
                   AND last_seen > %s - INTERVAL '36 hours'
                   AND first_seen < %s + INTERVAL '36 hours'
                   AND entity_ids && %s::bigint[]
                 ORDER BY last_seen DESC LIMIT 50
                """,
                (tenant_id, when, when, entity_ids),
            )
            candidates = cur.fetchall()
        # Pass 2b: title-token-only match (when no entities; or as fallback)
        # This is the unlock for most news signals — they don't have
        # entity_mentions populated yet, but their titles share content
        # words that identify the same story.
        if not candidates and tokens:
            cur.execute(
                """
                SELECT cluster_id, entity_ids, title_keywords
                  FROM signal_clusters
                 WHERE tenant_id = %s
                   AND last_seen > %s - INTERVAL '36 hours'
                   AND first_seen < %s + INTERVAL '36 hours'
                   AND title_keywords && %s::text[]
                 ORDER BY last_seen DESC LIMIT 50
                """,
                (tenant_id, when, when, tokens[:6]),
            )
            candidates = cur.fetchall()
    if not candidates:
        return None
    in_ents = set(entity_ids)
    in_toks = set(tokens)
    best = None
    best_score = 0.0
    for cluster_id, c_ents, c_toks in candidates:
        c_ents = set(c_ents or [])
        c_toks = set(c_toks or [])
        if not c_toks:
            continue
        ent_overlap = (
            len(in_ents & c_ents) / max(len(in_ents | c_ents), 1)
            if in_ents and c_ents else 0.0
        )
        tok_overlap = len(in_toks & c_toks) / max(len(in_toks | c_toks), 1)
        shared_tokens = len(in_toks & c_toks)
        # Accept if EITHER signal is strong:
        #   - entity overlap ≥ 0.5 AND ≥1 shared tokens
        #   - OR token overlap ≥ 0.45 AND ≥3 shared tokens (title-only path)
        if (ent_overlap >= 0.5 and shared_tokens >= 1) or \
           (tok_overlap >= 0.45 and shared_tokens >= 3):
            score = 0.6 * tok_overlap + 0.4 * ent_overlap
            if score > best_score:
                best_score = score
                best = cluster_id
    return best


def _attach_to_cluster(conn, cluster_id: int, signal_rid: int, when) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO signal_cluster_memberships (signal_rid, cluster_id)
            VALUES (%s, %s)
            ON CONFLICT (signal_rid) DO NOTHING
            """,
            (signal_rid, cluster_id),
        )
        cur.execute(
            """
            UPDATE signal_clusters c
               SET last_seen   = GREATEST(last_seen, %s),
                   first_seen  = LEAST(first_seen, %s),
                   article_count = (SELECT COUNT(*) FROM signal_cluster_memberships WHERE cluster_id = c.cluster_id),
                   source_count  = (SELECT COUNT(DISTINCT s.payload->>'source')
                                      FROM signals s
                                      JOIN signal_cluster_memberships m ON m.signal_rid = s.rid
                                     WHERE m.cluster_id = c.cluster_id),
                   updated_at  = NOW()
             WHERE c.cluster_id = %s
            """,
            (when, when, cluster_id),
        )


def _create_cluster(conn, *, tenant_id: str, signal_rid: int, when,
                    title: str, entity_ids: list[int], tokens: list[str],
                    fingerprint: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO signal_clusters
                (tenant_id, canonical_signal_rid, topic_summary,
                 first_seen, last_seen,
                 article_count, source_count,
                 entity_ids, title_keywords, fingerprint_hash)
            VALUES (%s, %s, %s, %s, %s, 1, 1, %s, %s, %s)
            RETURNING cluster_id
            """,
            (tenant_id, signal_rid, title, when, when,
             entity_ids, tokens[:20], fingerprint),
        )
        cluster_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO signal_cluster_memberships (signal_rid, cluster_id)
            VALUES (%s, %s)
            ON CONFLICT (signal_rid) DO NOTHING
            """,
            (signal_rid, cluster_id),
        )
    return cluster_id


def cluster_one(signal_rid: int) -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.rid, s.tenant_id, s.timestamp,
                       s.payload->>'title' AS title,
                       m.cluster_id
                  FROM signals s
                  LEFT JOIN signal_cluster_memberships m ON m.signal_rid = s.rid
                 WHERE s.rid = %s
                """,
                (signal_rid,),
            )
            row = cur.fetchone()
        if not row:
            return {"error": "signal not found"}
        rid, tenant_id, when, title, existing_cluster = row
        if existing_cluster:
            return {"cluster_id": existing_cluster, "action": "already_clustered"}
        if not title:
            return {"error": "no title", "action": "skipped"}
        tokens = _tokenize_title(title)
        entity_ids = _entity_ids_for_signal(conn, rid)
        if not tokens and not entity_ids:
            return {"error": "no signal content", "action": "skipped"}
        fp = _fingerprint(entity_ids, tokens, when)
        existing = _find_matching_cluster(
            conn, tenant_id=tenant_id, when=when,
            entity_ids=entity_ids, tokens=tokens, fingerprint=fp,
        )
        if existing:
            _attach_to_cluster(conn, existing, rid, when)
            conn.commit()
            return {"cluster_id": existing, "action": "matched"}
        cluster_id = _create_cluster(
            conn, tenant_id=tenant_id, signal_rid=rid, when=when,
            title=title, entity_ids=entity_ids, tokens=tokens, fingerprint=fp,
        )
        conn.commit()
        return {"cluster_id": cluster_id, "action": "created"}


def run_batch(limit: int = BATCH_SIZE, max_age_hours: int = 720) -> dict:
    counts = {"processed": 0, "matched": 0, "created": 0,
              "skipped": 0, "errors": 0}
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.rid FROM signals s
                 WHERE NOT EXISTS (
                     SELECT 1 FROM signal_cluster_memberships m WHERE m.signal_rid = s.rid
                 )
                   AND s.payload->>'title' IS NOT NULL
                   AND s.timestamp > NOW() - INTERVAL '720 hours'
                 ORDER BY s.timestamp DESC
                 LIMIT %s
                """,
                (limit,),
            )
            rids = [r[0] for r in cur.fetchall()]
    for rid in rids:
        try:
            r = cluster_one(rid)
            action = r.get("action", "skipped")
            counts["matched"] += (action == "matched")
            counts["created"] += (action == "created")
            counts["skipped"] += (action not in ("matched", "created"))
            counts["processed"] += 1
        except Exception as e:
            log.warning("cluster_one(%s) failed: %s", rid, e)
            counts["errors"] += 1
    return counts


# ─── Daemon shell (supervisor-compatible) ─────────────────────────────

def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default") -> dict:
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_worker_loop, daemon=True, name="signal_clusterer",
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    _STATUS["phase"] = "running"
    return {"status": "started"}


def stop_worker() -> dict:
    _STOP_EVENT.set()
    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"
    return {"status": "stopped"}


def _worker_loop() -> None:
    while not _STOP_EVENT.is_set():
        try:
            r = run_batch(limit=BATCH_SIZE)
            _STATUS["processed"] += r["processed"]
            _STATUS["clustered_into_existing"] += r["matched"]
            _STATUS["new_clusters"] += r["created"]
            _STATUS["skipped"] += r["skipped"]
            _STATUS["errors"] += r["errors"]
            _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
            _STATUS["phase"] = (
                "idle" if r["processed"] == 0 else f"clustered {r['processed']}"
            )
        except Exception as e:
            log.warning("clusterer tick failed: %s", e)
            _STATUS["errors"] += 1
        for _ in range(TICK_INTERVAL):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)
    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"
