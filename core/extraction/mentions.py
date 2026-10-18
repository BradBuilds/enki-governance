"""
Per-mention storage — one row per (article, entity) occurrence.

The `entities` table is canonical (one row per person/org). But every
article that mentions an entity carries UNIQUE information: what role
the entity played, the sentence it appeared in, the offset in the
body. Without that, entity resolution gives you a name and a count
but not WHY the entity matters in each context.

This module is the bridge. Called by the runner after resolve_all
has stamped entity:<id> node_ids onto envelope.entities[].

Also extracts context_snippet automatically when a body text is
available — takes the sentence surrounding the entity's first_offset,
so the UI can show "…at the Shanghai summit, **Petrov** told **Wei**
that sanctions had failed…" as the justification for a mention edge.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from core.ldu.lineage import lineage_columns, get_lineage_stamp

log = logging.getLogger("enki.extraction.mentions")

# Sentence boundary heuristic — good enough for English + most romance
# languages. For Chinese/Japanese we'd want a proper segmenter; these
# pass through as a single long sentence which is still useful.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[\.\!\?。！？])\s+(?=[A-Z\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff])")


def _pick_context_snippet(body: str, offset: Optional[int],
                          name: str, max_len: int = 300) -> str:
    """Return the sentence containing `offset` (or the first sentence
    that contains `name` if offset is missing). Trimmed to max_len with
    centered ellipsis so the name stays visible."""
    if not body:
        return ""
    if offset is None or offset < 0 or offset > len(body):
        # Fall back to first substring match
        lo = body.lower().find(name.lower())
        if lo < 0:
            return ""
        offset = lo

    # Walk back to the start of the sentence containing the offset
    start = offset
    while start > 0 and body[start - 1] not in ".!?。！？\n":
        start -= 1
    # And forward to the end
    end = offset
    while end < len(body) and body[end] not in ".!?。！？\n":
        end += 1
    if end < len(body):
        end += 1  # include the terminator

    snippet = body[start:end].strip()
    if len(snippet) <= max_len:
        return snippet

    # Too long — center on the name
    name_lo = snippet.lower().find(name.lower())
    if name_lo < 0:
        return snippet[:max_len] + "…"
    half = max_len // 2
    s = max(0, name_lo - half)
    e = min(len(snippet), name_lo + half)
    prefix = "…" if s > 0 else ""
    suffix = "…" if e < len(snippet) else ""
    return f"{prefix}{snippet[s:e].strip()}{suffix}"


def upsert_mentions_from_envelope(
    envelope: dict, conn, *,
    tenant_id: str = "default",
    signal_rid: Optional[int] = None,
) -> dict:
    """Walk envelope.entities[] and insert one entity_mentions row per
    resolved entity. Idempotent via the (entity_id, signal_rid, offset)
    UNIQUE index — re-running produces UPDATEs, not duplicates."""
    stats = {"inserted": 0, "updated": 0, "skipped": 0}
    if conn is None:
        return stats

    body = (envelope.get("summary") or {}).get("body_original") or \
           (envelope.get("summary") or {}).get("narrative") or ""
    node_id = envelope.get("node_id") or None
    source_url = (envelope.get("source") or {}).get("url") or None
    event_time = (envelope.get("temporal") or {}).get("event_time")
    # Profile_id stamped on the envelope by the runner. Federation
    # publisher reads extraction_profile_id off entity_mentions to
    # discover which envelopes to ship to peers — NULL here means the
    # row is invisible to federation.
    extraction_profile_id = envelope.get("extraction_profile_id") or "legacy_v1"

    lineage = lineage_columns(ldu_t_id=signal_rid)

    for ent in envelope.get("entities") or []:
        if not isinstance(ent, dict):
            stats["skipped"] += 1
            continue
        nid = ent.get("node_id") or ""
        if not nid.startswith("entity:"):
            stats["skipped"] += 1
            continue
        try:
            entity_id = int(nid.split(":", 1)[1])
        except (ValueError, IndexError):
            stats["skipped"] += 1
            continue

        name_raw = ent.get("name_raw") or ent.get("label") or ""
        role = (ent.get("role") or "mention").strip().lower()
        offset = ent.get("first_offset")
        confidence = float(ent.get("confidence", 0.7) or 0.7)

        # Auto-derive context snippet when extractor didn't pre-fill one
        snippet = ent.get("context_snippet") or _pick_context_snippet(
            body, offset, name_raw,
        )
        if snippet:
            ent["context_snippet"] = snippet  # feed it back to envelope for auditing

        # Phase 4.2: pull lat/lng for the mention. Two sources:
        #   1. The envelope entity may already carry _centroid_lat/lng from
        #      the gazetteer (llm_mapper stamps them).
        #   2. Otherwise fall back to the entity's own coords (when the
        #      entity IS a place — country, city, etc.).
        lat = ent.get("_centroid_lat")
        lng = ent.get("_centroid_lng")
        if lat is None or lng is None:
            # First check the gazetteerpack — it has centroids for every
            # known country, maritime area, and admin region. The resolver
            # doesn't stamp these onto the envelope (entities just get
            # node_id), so the mentions writer needs to look them up itself.
            try:
                from core.gdu.gazetteer import lookup as _gaz_lookup
                _hit = _gaz_lookup(
                    ent.get("name_canonical") or ent.get("name_raw") or ""
                )
                if _hit and _hit.centroid_lat is not None and _hit.centroid_lng is not None:
                    lat = lat if lat is not None else _hit.centroid_lat
                    lng = lng if lng is not None else _hit.centroid_lng
            except Exception:
                pass

        if lat is None or lng is None:
            # Final fallback: place_attributes sidecar (post-unification
            # migration 118). Places are entities; spatial data lives in
            # the sidecar keyed on entity_id. Wrapped in a savepoint so
            # a schema mismatch doesn't poison the outer transaction.
            try:
                with conn.cursor() as _gcur:
                    _gcur.execute("SAVEPOINT lookup_geo")
                    try:
                        _gcur.execute(
                            """
                            SELECT ST_Y(coordinates::geometry),
                                   ST_X(coordinates::geometry)
                              FROM place_attributes
                             WHERE entity_id = %s LIMIT 1
                            """,
                            (entity_id,),
                        )
                        row = _gcur.fetchone()
                        if row:
                            lat = lat if lat is not None else row[0]
                            lng = lng if lng is not None else row[1]
                        _gcur.execute("RELEASE SAVEPOINT lookup_geo")
                    except Exception:
                        _gcur.execute("ROLLBACK TO SAVEPOINT lookup_geo")
            except Exception:
                pass

        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO entity_mentions (
                        tenant_id, entity_id, signal_rid, node_id, source_url,
                        name_raw, role, context_snippet, first_offset,
                        confidence, event_time,
                        latitude, longitude,
                        admitted_under_active_set, admitted_under_pack_versions,
                        gdu_canonical, ldu_t_id,
                        extraction_profile_id
                    ) VALUES (
                        %(tenant)s, %(entity)s, %(rid)s, %(node)s, %(url)s,
                        %(name)s, %(role)s, %(snippet)s, %(offset)s,
                        %(conf)s, %(event_time)s,
                        %(lat)s, %(lng)s,
                        %(active_set)s, %(pack_versions)s::jsonb,
                        %(canonical)s, %(ldu_t)s,
                        %(profile)s
                    )
                    ON CONFLICT (entity_id, signal_rid, COALESCE(first_offset, 0))
                    WHERE signal_rid IS NOT NULL
                    DO UPDATE SET
                        role = EXCLUDED.role,
                        context_snippet = EXCLUDED.context_snippet,
                        confidence = GREATEST(entity_mentions.confidence, EXCLUDED.confidence),
                        latitude = COALESCE(entity_mentions.latitude, EXCLUDED.latitude),
                        longitude = COALESCE(entity_mentions.longitude, EXCLUDED.longitude),
                        extraction_profile_id = COALESCE(entity_mentions.extraction_profile_id, EXCLUDED.extraction_profile_id),
                        updated_at = NOW()
                    """,
                    {
                        "tenant": tenant_id, "entity": entity_id,
                        "rid": signal_rid, "node": node_id, "url": source_url,
                        "name": name_raw[:500], "role": role[:32],
                        "snippet": (snippet or "")[:2000],
                        "offset": offset, "conf": confidence,
                        "event_time": event_time,
                        "lat": lat, "lng": lng,
                        "profile": extraction_profile_id,
                        **lineage,
                    },
                )
            stats["inserted"] += 1
        except Exception as e:
            log.debug("mention insert failed for entity %s: %s", entity_id, e)
            try: conn.rollback()
            except Exception: pass

    try:
        conn.commit()
    except Exception:
        pass
    return stats


# ── Async queue boundary ──────────────────────────────────────────────────
#
# Enqueue an envelope for the mentions_writer worker. This is what the
# runner calls now (instead of upsert_mentions_from_envelope directly).
#
# Migration 041 created the mentions_queue table; the worker is in
# core/workers/mentions_writer.py.

def enqueue_mentions(
    envelope: dict, conn, *,
    tenant_id: str = "default",
    signal_rid: Optional[int] = None,
) -> Optional[int]:
    """Insert one row into mentions_queue. Cheap (~3ms). The worker picks
    it up within 2 seconds and runs upsert_mentions_from_envelope.

    Returns the queue_id on success, None on failure (e.g. an active
    queue entry already exists for this signal — idempotent enqueue).
    """
    if conn is None or signal_rid is None:
        return None

    stamp = get_lineage_stamp(ldu_t_id=signal_rid)

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO mentions_queue
                    (tenant_id, signal_rid, envelope, status,
                     queued_under_active_set, queued_under_pack_versions)
                VALUES (%s, %s, %s::jsonb, 'queued', %s, %s::jsonb)
                ON CONFLICT (tenant_id, signal_rid)
                  WHERE status IN ('queued', 'processing')
                  DO UPDATE SET
                    envelope   = EXCLUDED.envelope,
                    updated_at = NOW()
                RETURNING queue_id
                """,
                (
                    tenant_id, signal_rid,
                    json.dumps(envelope, default=str),
                    stamp.get("active_set_version"),
                    json.dumps(stamp.get("pack_versions") or {}),
                ),
            )
            row = cur.fetchone()
        try:
            conn.commit()
        except Exception:
            pass
        return row[0] if row else None
    except Exception as e:
        log.warning("enqueue_mentions failed for signal %s: %s", signal_rid, e)
        try:
            conn.rollback()
        except Exception:
            pass
        return None
