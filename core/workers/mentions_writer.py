"""
mentions_writer — async worker that consumes mentions_queue and writes
entity_mentions rows.

Architectural role: this is the worker that pulls (signal, envelope)
from `mentions_queue` and runs `upsert_mentions_from_envelope`. It
matches the existing supervised-worker pattern (watch_derivatives,
conflict_scanner, node_builder) so the system has one consistent
discipline for derived/aggregator writes.

Concurrency: the SELECT for the next batch uses
`FOR UPDATE SKIP LOCKED` so multiple workers (or one worker on
multiple connections) can process in parallel without stepping on
each other.

Retry policy:
    queued      → first attempt
    processing  → being worked
    error       → failed, retry-eligible after a 5-minute backoff,
                  up to MAX_ATTEMPTS times
    done        → terminal; pruned after RETENTION_DAYS

Run as a daemon thread:
    from core.workers.mentions_writer import start_mentions_writer
    start_mentions_writer(interval_seconds=2)
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger("enki.workers.mentions_writer")


# ── Tunables ───────────────────────────────────────────────────────────────

BATCH_SIZE        = 20           # rows claimed per pass
MAX_ATTEMPTS      = 3            # before we leave it 'error' permanently
RETRY_BACKOFF_MIN = 5            # minutes between retries on error rows
RETENTION_DAYS    = 7            # how long to keep 'done' rows for audit
PRUNE_EVERY_N     = 200          # run cleanup once every N polls


# ── Daemon control ─────────────────────────────────────────────────────────

_THREAD: Optional[threading.Thread] = None
_STOP   = threading.Event()


def _claim_batch(conn, batch_size: int = BATCH_SIZE) -> list[tuple]:
    """Atomically claim N rows: queued OR retry-eligible errors.

    Uses FOR UPDATE SKIP LOCKED so concurrent workers don't fight.
    Returns list of (queue_id, signal_rid, envelope, tenant_id, attempts).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH claimed AS (
                SELECT queue_id
                  FROM mentions_queue
                 WHERE (status = 'queued')
                    OR (status = 'error'
                        AND attempts < %s
                        AND COALESCE(updated_at, queued_at) < NOW() - %s::interval)
                 ORDER BY queued_at
                 LIMIT %s
                 FOR UPDATE SKIP LOCKED
            )
            UPDATE mentions_queue q
               SET status     = 'processing',
                   attempts   = q.attempts + 1,
                   locked_at  = NOW(),
                   updated_at = NOW()
              FROM claimed c
             WHERE q.queue_id = c.queue_id
            RETURNING q.queue_id, q.signal_rid, q.envelope, q.tenant_id, q.attempts
            """,
            (MAX_ATTEMPTS, f"{RETRY_BACKOFF_MIN} minutes", batch_size),
        )
        rows = cur.fetchall()
    conn.commit()
    return rows


def _mark_done(conn, queue_id: int, stats: dict, duration_ms: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE mentions_queue
               SET status         = 'done',
                   inserted_count = %s,
                   skipped_count  = %s,
                   duration_ms    = %s,
                   processed_at   = NOW(),
                   updated_at     = NOW(),
                   last_error     = NULL
             WHERE queue_id = %s
            """,
            (stats.get("inserted", 0), stats.get("skipped", 0),
             duration_ms, queue_id),
        )
    conn.commit()


def _mark_error(conn, queue_id: int, error: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE mentions_queue
               SET status     = 'error',
                   last_error = %s,
                   updated_at = NOW()
             WHERE queue_id = %s
            """,
            (str(error)[:1000], queue_id),
        )
    conn.commit()


def _prune_done(conn) -> int:
    """Delete done rows older than RETENTION_DAYS. Keeps 'error' rows."""
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM mentions_queue
             WHERE status = 'done'
               AND processed_at < NOW() - %s::interval
            """,
            (f"{RETENTION_DAYS} days",),
        )
        n = cur.rowcount
    conn.commit()
    return n


def process_one_batch() -> dict:
    """Claim a batch, process each, mark done/error. Returns stats."""
    from storage.postgres.db import get_connection
    from core.extraction.mentions import upsert_mentions_from_envelope

    out = {"claimed": 0, "done": 0, "error": 0,
           "total_inserted": 0, "total_skipped": 0}

    with get_connection() as conn:
        rows = _claim_batch(conn)
        out["claimed"] = len(rows)

        for queue_id, signal_rid, envelope, tenant_id, attempts in rows:
            t0 = time.time()
            try:
                stats = upsert_mentions_from_envelope(
                    envelope, conn,
                    tenant_id=tenant_id,
                    signal_rid=signal_rid,
                )
                duration_ms = int((time.time() - t0) * 1000)
                _mark_done(conn, queue_id, stats, duration_ms)
                out["done"] += 1
                out["total_inserted"] += stats.get("inserted", 0)
                out["total_skipped"]  += stats.get("skipped", 0)
            except Exception as e:
                log.warning(
                    "mentions_writer: queue_id=%s signal_rid=%s "
                    "attempt=%s failed: %s",
                    queue_id, signal_rid, attempts, e,
                )
                try:
                    _mark_error(conn, queue_id, str(e))
                except Exception:
                    log.exception("could not mark queue row as error")
                out["error"] += 1

    return out


def queue_depth() -> dict:
    """Snapshot of queue counts by status. For health checks."""
    from storage.postgres.db import get_connection
    out: dict[str, int] = {"queued": 0, "processing": 0,
                           "error": 0, "done": 0}
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, COUNT(*) FROM mentions_queue GROUP BY status"
                )
                for status, count in cur.fetchall():
                    out[status] = count
    except Exception as e:
        log.debug("queue_depth() failed: %s", e)
    return out


# ── Daemon loop ────────────────────────────────────────────────────────────

def _loop(interval_seconds: int):
    poll_count = 0
    log.info("mentions_writer started (interval=%ds, batch=%d)",
             interval_seconds, BATCH_SIZE)
    while not _STOP.is_set():
        try:
            stats = process_one_batch()
            if stats["claimed"]:
                log.info(
                    "mentions_writer: claimed=%d done=%d error=%d "
                    "inserted=%d skipped=%d",
                    stats["claimed"], stats["done"], stats["error"],
                    stats["total_inserted"], stats["total_skipped"],
                )
            poll_count += 1
            if poll_count % PRUNE_EVERY_N == 0:
                pruned = _prune_done_safe()
                if pruned:
                    log.info("mentions_writer: pruned %d done rows", pruned)
        except Exception as e:
            log.exception("mentions_writer loop error: %s", e)
        _STOP.wait(interval_seconds)
    log.info("mentions_writer stopped")


def _prune_done_safe() -> int:
    from storage.postgres.db import get_connection
    try:
        with get_connection() as conn:
            return _prune_done(conn)
    except Exception as e:
        log.debug("prune skipped: %s", e)
        return 0


def start_mentions_writer(interval_seconds: int = 2) -> threading.Thread:
    """Start the daemon thread. Idempotent — returns existing thread
    if already running."""
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        return _THREAD
    _STOP.clear()
    _THREAD = threading.Thread(
        target=_loop, args=(interval_seconds,),
        name="mentions_writer", daemon=True,
    )
    _THREAD.start()
    return _THREAD


def stop_mentions_writer():
    _STOP.set()


if __name__ == "__main__":
    # One-shot CLI for manual testing
    import sys
    sys.path.insert(0, ".")
    print("queue_depth:", queue_depth())
    print("process_one_batch:", process_one_batch())
    print("queue_depth_after:", queue_depth())
