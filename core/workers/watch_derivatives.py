"""
watch_derivatives.py — Phase 4.7 background worker.

Computes ``node_metrics`` rows on a schedule. Pure read against
``entity_mentions`` + ``signals`` + ``connectors``; no writes to
governance state. The output is what the dashboard cards (Trending now,
Hotspots, Hype watch, Narrative push) and StoryCard render.

Run as a daemon thread or as a one-shot:

    from core.workers.watch_derivatives import compute_all
    compute_all(window_label='24h')

The supervisor scheduler can wire ``start_watch_derivatives()`` for
recurring 15-minute computation.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger("enki.workers.watch_derivatives")


_WINDOW_HOURS = {
    "1h": 1,
    "24h": 24,
    "7d": 24 * 7,
    "30d": 24 * 30,
}


def _shannon_entropy(counts: list[int]) -> float:
    total = sum(counts)
    if total <= 0:
        return 0.0
    out = 0.0
    for c in counts:
        if c <= 0:
            continue
        p = c / total
        out -= p * math.log2(p)
    return out


def compute_for_node(
    conn,
    node_id: str,
    tenant_id: str = "default",
    window_label: str = "24h",
) -> Optional[dict]:
    """Compute the metrics for one node and upsert into node_metrics.
    Returns the computed metrics dict (or None if the node has no
    mentions in the window)."""
    hours = _WINDOW_HOURS.get(window_label, 24)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    with conn.cursor() as cur:
        # Pull every mention of this node in window with its source
        cur.execute(
            """
            SELECT m.created_at, COALESCE(s.connector_id, 'unknown'),
                   m.latitude, m.longitude
              FROM entity_mentions m
              LEFT JOIN signals s ON s.rid = m.signal_rid
             WHERE m.node_id = %s
               AND m.tenant_id = %s
               AND m.created_at >= %s
            """,
            (node_id, tenant_id, since),
        )
        rows = cur.fetchall()

    if not rows:
        return None

    mention_count = len(rows)
    mph = mention_count / max(1, hours)

    # Frequency strip: 24 hourly buckets (or proportional buckets for non-24h)
    bucket_count = 24
    bucket_seconds = (hours * 3600) / bucket_count
    strip = [0] * bucket_count
    for created_at, _src, _lat, _lng in rows:
        ts = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
        delta = (ts - since).total_seconds()
        bi = max(0, min(bucket_count - 1, int(delta // bucket_seconds)))
        strip[bi] += 1

    # Velocity: ratio of last bucket's count to mean of earlier buckets.
    earlier_mean = sum(strip[:-1]) / max(1, len(strip) - 1) if len(strip) > 1 else 1
    velocity = strip[-1] / max(0.1, earlier_mean)

    # Spike factor: this window's mention rate vs 30-day baseline
    long_since = datetime.now(timezone.utc) - timedelta(days=30)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM entity_mentions
             WHERE node_id = %s AND tenant_id = %s AND created_at >= %s
            """,
            (node_id, tenant_id, long_since),
        )
        long_total = cur.fetchone()[0] or 0
    baseline_rate = long_total / max(1, 30 * 24)  # mph over 30 days
    spike_factor = mph / max(0.05, baseline_rate)

    # Source diversity
    src_counter: Counter[str] = Counter(r[1] for r in rows)
    src_counts = list(src_counter.values())
    entropy = _shannon_entropy(src_counts)
    dominant = src_counter.most_common(1)[0]
    dominant_source, dominant_count = dominant
    dominant_share = dominant_count / mention_count

    # Hype score = (1 - entropy_normalized) * dominant_share * (1 + spike_factor / 5)
    max_entropy = math.log2(len(src_counter)) if len(src_counter) > 1 else 1.0
    entropy_norm = entropy / max(1.0, max_entropy)
    hype = (1.0 - entropy_norm) * dominant_share * (1.0 + min(5.0, spike_factor) / 5.0)

    # Hotspot intensity (only meaningful for place nodes)
    hotspot = None
    coords = [(r[2], r[3]) for r in rows if r[2] is not None and r[3] is not None]
    if coords:
        # Number of unique grain-1 cells / total mentions — proxy for concentration
        grains = {(round(la, 1), round(ln, 1)) for la, ln in coords}
        # Concentration ratio inverted into intensity: more concentrated = higher
        hotspot = mention_count / max(1, len(grains))

    metrics = {
        "node_id": node_id,
        "tenant_id": tenant_id,
        "window_label": window_label,
        "mention_count": mention_count,
        "mentions_per_hour": round(mph, 3),
        "velocity": round(velocity, 3),
        "spike_factor": round(spike_factor, 3),
        "frequency_strip": strip,
        "source_entropy": round(entropy, 4),
        "dominant_source": dominant_source,
        "dominant_source_share": round(dominant_share, 4),
        "hype_score": round(hype, 4),
        "hotspot_intensity": round(hotspot, 4) if hotspot is not None else None,
    }

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO node_metrics (
                node_id, tenant_id, window_label,
                mention_count, mentions_per_hour, velocity, spike_factor,
                frequency_strip,
                source_entropy, dominant_source, dominant_source_share,
                hype_score, hotspot_intensity, computed_at
            ) VALUES (
                %(node_id)s, %(tenant_id)s, %(window_label)s,
                %(mention_count)s, %(mentions_per_hour)s, %(velocity)s, %(spike_factor)s,
                %(frequency_strip)s,
                %(source_entropy)s, %(dominant_source)s, %(dominant_source_share)s,
                %(hype_score)s, %(hotspot_intensity)s, NOW()
            )
            ON CONFLICT (node_id, window_label) DO UPDATE SET
                tenant_id             = EXCLUDED.tenant_id,
                mention_count         = EXCLUDED.mention_count,
                mentions_per_hour     = EXCLUDED.mentions_per_hour,
                velocity              = EXCLUDED.velocity,
                spike_factor          = EXCLUDED.spike_factor,
                frequency_strip       = EXCLUDED.frequency_strip,
                source_entropy        = EXCLUDED.source_entropy,
                dominant_source       = EXCLUDED.dominant_source,
                dominant_source_share = EXCLUDED.dominant_source_share,
                hype_score            = EXCLUDED.hype_score,
                hotspot_intensity     = EXCLUDED.hotspot_intensity,
                computed_at           = NOW()
            """,
            metrics,
        )
    return metrics


def compute_all(
    tenant_id: str = "default",
    window_label: str = "24h",
    limit_active_nodes: int = 5000,
) -> dict:
    """Compute metrics for every node with at least one mention in the
    window. Returns a summary dict."""
    from storage.postgres.db import get_connection
    hours = _WINDOW_HOURS.get(window_label, 24)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    computed = 0
    skipped = 0
    started = time.time()

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT m.node_id
                  FROM entity_mentions m
                 WHERE m.tenant_id = %s
                   AND m.created_at >= %s
                   AND m.node_id IS NOT NULL
                 LIMIT %s
                """,
                (tenant_id, since, limit_active_nodes),
            )
            node_ids = [r[0] for r in cur.fetchall()]

        for nid in node_ids:
            try:
                result = compute_for_node(conn, nid, tenant_id, window_label)
                if result is None:
                    skipped += 1
                else:
                    computed += 1
            except Exception as e:
                log.debug("compute_for_node(%s) failed: %s", nid, e)
                skipped += 1
        conn.commit()

    elapsed = time.time() - started
    log.info(
        "watch_derivatives: computed=%d skipped=%d window=%s elapsed=%.1fs",
        computed, skipped, window_label, elapsed,
    )
    return {
        "computed": computed,
        "skipped": skipped,
        "window_label": window_label,
        "elapsed_seconds": elapsed,
    }


# ── Daemon ─────────────────────────────────────────────────────────────────

_THREAD: Optional[threading.Thread] = None
_STOP = threading.Event()


def _loop(interval_seconds: int, tenant_id: str, window_label: str):
    while not _STOP.is_set():
        try:
            compute_all(tenant_id=tenant_id, window_label=window_label)
        except Exception as e:
            log.warning("watch_derivatives loop error: %s", e)
        _STOP.wait(interval_seconds)


def start_watch_derivatives(
    tenant_id: str = "default",
    window_label: str = "24h",
    interval_seconds: int = 900,
):
    """Start the background derivative computation thread."""
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        return _THREAD
    _STOP.clear()
    _THREAD = threading.Thread(
        target=_loop, args=(interval_seconds, tenant_id, window_label),
        name="watch_derivatives", daemon=True,
    )
    _THREAD.start()
    log.info("watch_derivatives started: every %ds, window=%s",
             interval_seconds, window_label)
    return _THREAD


def stop_watch_derivatives():
    _STOP.set()


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    print(compute_all())
