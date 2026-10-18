"""
Media backfill — walks signal_extractions that carry media.thumbnail URLs
but no media_node_id, pulls them through the Phase 4B pipeline, and marks
the envelope as processed.

Runs under the governor at BACKFILL priority so it gets paused entirely
under foreground pressure. Processes a small batch per tick; designed to
chew through historical data incrementally without ever hogging the queue.
"""
from __future__ import annotations

import logging

log = logging.getLogger("enki.learning.media_backfill")

BATCH_LIMIT = 10                   # images per tick — small to keep GPU/network quiet
FETCH_WINDOW_DAYS = 90             # don't try to reach URLs older than this


def run(conn) -> dict:
    stats = {"examined": 0, "processed": 0, "skipped": 0, "errors": 0}

    rows = _fetch_pending(conn, limit=BATCH_LIMIT)
    stats["examined"] = len(rows)

    from core.media.pipeline import process_image_url

    for row in rows:
        thumb_url = row["thumb_url"]
        article_nid = row["node_id"]
        outlet = row["outlet"]
        signal_rid = row["signal_rid"]
        try:
            result = process_image_url(
                conn, thumb_url,
                tenant_id=row["tenant_id"],
                signal_rid=signal_rid,
                article_node_id=article_nid,
                outlet=outlet,
                role="thumbnail",
                emit_edge=True,
            )
            if result:
                _mark_processed(conn, row["extraction_id"], result)
                stats["processed"] += 1
            else:
                _mark_processed(conn, row["extraction_id"], None)
                stats["skipped"] += 1
        except Exception as e:
            log.exception("media_backfill %s failed: %s", article_nid, e)
            try: conn.rollback()
            except Exception: pass
            stats["errors"] += 1

    return stats


def register(governor) -> None:
    from core.learning.governor import Priority
    governor.register(
        name="media_backfill",
        priority=Priority.BACKFILL,
        interval_seconds=120.0,
        fn=run,
    )


def _fetch_pending(conn, *, limit: int) -> list[dict]:
    """Signal extractions with a media.thumbnail URL and no media node yet.

    We stash progress on the envelope itself at `media.backfill_checked_at`
    so a URL that fails to fetch doesn't get retried on every tick. Rows
    where that key already exists are skipped."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT se.extraction_id, se.tenant_id, se.signal_rid, se.node_id,
                   se.envelope->'media'->>'thumbnail'        AS thumb_url,
                   se.envelope->'source'->>'outlet_name'     AS outlet
              FROM signal_extractions se
             WHERE se.envelope->'media'->>'thumbnail' IS NOT NULL
               AND se.envelope->'media'->>'thumbnail' <> ''
               AND se.envelope->'media'->'backfill_checked_at' IS NULL
               AND se.envelope->'media'->'images' IS NULL
               AND se.created_at > NOW() - INTERVAL '{FETCH_WINDOW_DAYS} days'
             ORDER BY se.created_at DESC
             LIMIT %s
            """,
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _mark_processed(conn, extraction_id: int, result: dict | None) -> None:
    """Stamp media.backfill_checked_at so we don't re-try. If the pipeline
    produced a media node, also write media.images[] into the envelope so
    envelope_summary picks it up on the next page render."""
    with conn.cursor() as cur:
        if result:
            cur.execute(
                """
                UPDATE signal_extractions SET
                    envelope = jsonb_set(
                        jsonb_set(envelope,
                            '{media,backfill_checked_at}',
                            to_jsonb(NOW()::text)),
                        '{media,images}',
                        COALESCE(envelope->'media'->'images', '[]'::jsonb)
                        || jsonb_build_array(jsonb_build_object(
                            'node_id', %(nid)s,
                            'url',     %(url)s,
                            'width',   %(w)s,
                            'height',  %(h)s,
                            'role',    'thumbnail'
                        ))
                    ),
                    updated_at = NOW()
                WHERE extraction_id = %(eid)s
                """,
                {
                    "eid": extraction_id,
                    "nid": result["node_id"],
                    "url": None,  # the URL is already in envelope.media.thumbnail
                    "w":   result.get("width"),
                    "h":   result.get("height"),
                },
            )
        else:
            cur.execute(
                """
                UPDATE signal_extractions SET
                    envelope = jsonb_set(envelope,
                        '{media,backfill_checked_at}',
                        to_jsonb(NOW()::text)),
                    updated_at = NOW()
                WHERE extraction_id = %s
                """,
                (extraction_id,),
            )
    conn.commit()
