"""
Signal extraction store — writes envelopes to signal_extractions.

Replaces the Phase 5 auto_node flow for the per-signal path. Every
signal that runs through the extraction pipeline lands here, not in
knowledge_nodes. knowledge_nodes is now reserved for research
artifacts only (dossiers, questions, chronicles, etc.) created by
rules / research workers / chronicle workers / humans.

Dedup contract — three cross-checks before INSERT:

  1. node_id UNIQUE constraint (strong)
     Deterministic hash of (signal_type, url, outlet, event_time)
     prevents the exact same signal from landing twice.

  2. content_hash collision check (medium)
     sha256(title + body) — catches the same article syndicated
     through multiple feeds with different URLs. When a match is
     found, we merge: keep the older row, append the newer signal_rid
     to its cited sources, do not create a new row.

  3. source_url + event_time window (soft)
     If a row with the same URL exists within ±5min, merge.

Source + time cross-reference columns (source_url, event_time,
content_hash) are promoted OUT of the envelope JSONB into their own
indexed columns so dedup queries are fast even at scale.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from core.extraction.envelope import get_path
from core.ldu.lineage import get_lineage_stamp

log = logging.getLogger("enki.extraction.store")


def _compute_content_hash(envelope: dict) -> Optional[str]:
    """sha256 of the title + body. Deterministic dedup key for content
    that might arrive via multiple routes."""
    title = (get_path(envelope, "identity.title_display") or "").strip().lower()
    body = (
        get_path(envelope, "summary.body_original")
        or get_path(envelope, "summary.feed_summary_clean")
        or get_path(envelope, "summary.narrative")
        or ""
    )
    body = body.strip().lower()[:5000]  # cap so small body differences don't split
    if not title and not body:
        return None
    combined = f"{title}|{body}".encode("utf-8")
    return hashlib.sha256(combined).hexdigest()


def _check_existing_extraction(
    conn,
    tenant_id: str,
    node_id: str,
    content_hash: Optional[str],
    source_url: Optional[str],
    event_time: Optional[str],
) -> Optional[dict]:
    """Three-level dedup check. Returns an existing extraction row dict
    if this signal duplicates something we already have.

    Order:
      1. node_id match (strong)
      2. content_hash match (catches syndication)
      3. source_url within ±5min of event_time (catches late re-fetches)
    """
    with conn.cursor() as cur:
        # 1. node_id
        cur.execute(
            """
            SELECT extraction_id, node_id, signal_rid, event_time
              FROM signal_extractions
             WHERE tenant_id = %s AND node_id = %s
             LIMIT 1
            """,
            (tenant_id, node_id),
        )
        row = cur.fetchone()
        if row:
            return {"match": "node_id", "extraction_id": row[0], "node_id": row[1],
                    "signal_rid": row[2]}

        # 2. content_hash
        if content_hash:
            cur.execute(
                """
                SELECT extraction_id, node_id, signal_rid
                  FROM signal_extractions
                 WHERE tenant_id = %s AND content_hash = %s
                 LIMIT 1
                """,
                (tenant_id, content_hash),
            )
            row = cur.fetchone()
            if row:
                return {"match": "content_hash", "extraction_id": row[0],
                        "node_id": row[1], "signal_rid": row[2]}

        # 3. source_url within 5min window
        if source_url and event_time:
            cur.execute(
                """
                SELECT extraction_id, node_id, signal_rid
                  FROM signal_extractions
                 WHERE tenant_id = %s
                   AND source_url = %s
                   AND event_time BETWEEN (%s::timestamptz - INTERVAL '5 minutes')
                                      AND (%s::timestamptz + INTERVAL '5 minutes')
                 LIMIT 1
                """,
                (tenant_id, source_url, event_time, event_time),
            )
            row = cur.fetchone()
            if row:
                return {"match": "url_time_window", "extraction_id": row[0],
                        "node_id": row[1], "signal_rid": row[2]}

    return None


def upsert_signal_extraction(
    envelope: dict,
    conn,
    *,
    tenant_id: str = "default",
    signal_rid: Optional[int] = None,
    signal_type: Optional[str] = None,
    feed_worthy: bool = True,
) -> dict:
    """Write one extraction to signal_extractions with strict dedup.

    Returns:
        {"status": "ok"|"deduped"|"skipped"|"error",
         "action": "insert"|"update"|"dedup_merge",
         "extraction_id": <int or None>,
         "node_id": <str>,
         "dedup_match": <"node_id"|"content_hash"|"url_time_window"|None>}
    """
    if conn is None:
        return {"status": "skipped", "reason": "no db connection"}

    node_id = envelope.get("node_id")
    if not node_id:
        return {"status": "skipped", "reason": "envelope has no node_id"}
    if not signal_type:
        return {"status": "skipped", "reason": "no signal_type"}

    # Compute dedup fields up front
    source_url = get_path(envelope, "source.url") or None
    source_outlet = get_path(envelope, "source.outlet_name") or None
    event_time = get_path(envelope, "temporal.event_time") or None
    content_hash = _compute_content_hash(envelope)

    # Normalize event_time to ISO or None (postgres happy with either)
    if event_time and not isinstance(event_time, str):
        event_time = str(event_time)

    # Dedup check
    existing = _check_existing_extraction(
        conn, tenant_id, node_id, content_hash, source_url, event_time,
    )
    if existing and existing.get("match") != "node_id":
        # content_hash or url_time_window collision — merge: keep the
        # existing row, just note that another signal pointed at it.
        # We do NOT overwrite the existing envelope because the original
        # was accepted first and we trust "first wins" for audit clarity.
        log.info(
            "dedup merge: incoming node_id=%s matched existing via %s -> existing node_id=%s",
            node_id, existing["match"], existing["node_id"],
        )
        return {
            "status": "deduped",
            "action": "dedup_merge",
            "extraction_id": existing["extraction_id"],
            "node_id": existing["node_id"],
            "dedup_match": existing["match"],
        }

    stamp = get_lineage_stamp(ldu_t_id=signal_rid)
    completeness = envelope.get("completeness") or {}

    # Truncate varchar columns to fit schema
    sig_type_trunc = (signal_type or "")[:128]
    node_id_trunc = (node_id or "")[:255]
    outlet_trunc = (source_outlet or "")[:255] if source_outlet else None
    tier_trunc = (completeness.get("tier") or "")[:16] or None
    subtype_val = envelope.get("subtype")
    subtype_trunc = subtype_val[:64] if subtype_val else None
    node_type_trunc = (envelope.get("node_type") or "")[:32]

    sql = """
        INSERT INTO signal_extractions (
            tenant_id, signal_rid, signal_type,
            node_id, envelope, envelope_version, node_type, subtype,
            completeness_score, completeness_tier,
            master_entity_id, master_topic_id,
            source_url, source_outlet, content_hash, event_time,
            admitted_under_active_set, admitted_under_pack_versions,
            gdu_canonical, ldu_t_id,
            llm_processed, feed_worthy
        ) VALUES (
            %(tenant_id)s, %(signal_rid)s, %(signal_type)s,
            %(node_id)s, %(envelope)s::jsonb, %(envelope_version)s,
            %(node_type)s, %(subtype)s,
            %(completeness_score)s, %(completeness_tier)s,
            %(entity_id)s, %(topic_id)s,
            %(source_url)s, %(source_outlet)s, %(content_hash)s, %(event_time)s,
            %(active_set)s, %(pack_versions)s::jsonb,
            %(canonical_flag)s, %(ldu_t)s,
            %(llm_processed)s, %(feed_worthy)s
        )
        ON CONFLICT (tenant_id, node_id) DO UPDATE SET
            envelope            = EXCLUDED.envelope,
            envelope_version    = EXCLUDED.envelope_version,
            node_type           = EXCLUDED.node_type,
            subtype             = EXCLUDED.subtype,
            -- completeness_* intentionally NOT carried across on conflict; see
            -- core/gdu/completeness.py. EXCLUDED is NULL now, so copying it
            -- would erase any score already recorded for this extraction.
            master_entity_id    = COALESCE(EXCLUDED.master_entity_id, signal_extractions.master_entity_id),
            master_topic_id     = COALESCE(EXCLUDED.master_topic_id,  signal_extractions.master_topic_id),
            source_url          = EXCLUDED.source_url,
            source_outlet       = EXCLUDED.source_outlet,
            content_hash        = EXCLUDED.content_hash,
            event_time          = EXCLUDED.event_time,
            llm_processed       = EXCLUDED.llm_processed,
            feed_worthy         = EXCLUDED.feed_worthy,
            updated_at          = NOW()
        RETURNING extraction_id, (xmax = 0) AS inserted
    """

    # Identify the master IDs from the resolved envelope items
    entity_id = None
    for e in envelope.get("entities") or []:
        nid = e.get("node_id") or ""
        if isinstance(nid, str) and nid.startswith("entity:"):
            try:
                entity_id = int(nid.split(":", 1)[1])
                break
            except (ValueError, IndexError):
                pass
    topic_id = None
    for t in envelope.get("topics") or []:
        nid = t.get("node_id") or ""
        if isinstance(nid, str) and nid.startswith("topic:"):
            try:
                topic_id = int(nid.split(":", 1)[1])
                break
            except (ValueError, IndexError):
                pass
    # Post-unification (migration 118): resolved_places carries
    # 'entity:NNN' strings — places are entities now. If no entity_id
    # picked up from envelope.entities, fall back to first resolved
    # place (same entity_id space).
    if entity_id is None:
        for p in (envelope.get("spatial") or {}).get("resolved_places") or []:
            if isinstance(p, str) and p.startswith("entity:"):
                try:
                    entity_id = int(p.split(":", 1)[1])
                    break
                except (ValueError, IndexError):
                    pass

    params = {
        "tenant_id": tenant_id,
        "signal_rid": signal_rid,
        "signal_type": sig_type_trunc,
        "node_id": node_id_trunc,
        "envelope": json.dumps(envelope, default=str),
        "envelope_version": envelope.get("envelope_version") or "v1",
        "node_type": node_type_trunc or None,
        "subtype": subtype_trunc,
        # Not asserted here — see core/gdu/completeness.py. The envelope no
        # longer carries a completeness claim for the writer to copy, and
        # defaulting to 0.0/'stub' would fabricate one. NULL = not yet scored.
        "completeness_score": None,
        "completeness_tier": None,
        "entity_id": entity_id,
        "topic_id": topic_id,
        "source_url": source_url[:2048] if source_url else None,
        "source_outlet": outlet_trunc,
        "content_hash": content_hash,
        "event_time": event_time,
        "active_set": stamp.get("active_set_version"),
        "pack_versions": json.dumps(stamp.get("pack_versions") or {}),
        "canonical_flag": bool(stamp.get("gdu_canonical")),
        "ldu_t": signal_rid,
        "llm_processed": any(
            p.get("stage") == "deep" and p.get("success")
            for p in (envelope.get("provenance", {}).get("extraction_passes") or [])
        ),
        "feed_worthy": feed_worthy,
    }

    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            extraction_id = row[0] if row else None
            inserted = bool(row[1]) if row else False
        conn.commit()
        return {
            "status": "ok",
            "action": "insert" if inserted else "update",
            "extraction_id": extraction_id,
            "node_id": node_id,
            "dedup_match": None,
        }
    except Exception as e:
        log.warning("upsert_signal_extraction(%s) failed: %s", node_id, e)
        try:
            conn.rollback()
        except Exception:
            pass
        return {
            "status": "error",
            "node_id": node_id,
            "reason": str(e),
        }
