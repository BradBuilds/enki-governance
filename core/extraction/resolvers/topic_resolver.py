"""
Topic resolver — dedupes envelope.topics[] against the topics table.

Topics emerge from extraction (no seeding). The first time a label is
seen, a topic row is created. Subsequent mentions update mention_count
and last_seen. Dedup key is (tenant_id, normalized_name).

Stamps envelope.topics[i].node_id = "topic:<topic_id>".
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from core.extraction.resolvers.base import normalize_name
from core.ldu.lineage import get_lineage_stamp

log = logging.getLogger("enki.extraction.resolvers.topic")


def _upsert_topic(
    conn,
    *,
    tenant_id: str,
    label: str,
    ldu_t_id: Optional[int],
) -> Optional[int]:
    normalized = normalize_name(label)
    stamp = get_lineage_stamp(ldu_t_id=ldu_t_id)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT topic_id FROM topics
                 WHERE tenant_id = %s AND normalized_name = %s
                 LIMIT 1
                """,
                (tenant_id, normalized),
            )
            row = cur.fetchone()
            if row:
                topic_id = row[0]
                cur.execute(
                    """
                    UPDATE topics
                       SET last_seen     = NOW(),
                           mention_count = mention_count + 1,
                           updated_at    = NOW()
                     WHERE topic_id = %s
                    """,
                    (topic_id,),
                )
                conn.commit()
                return topic_id

            cur.execute(
                """
                INSERT INTO topics (
                    tenant_id, canonical_type, canonical_name,
                    normalized_name,
                    mention_count,
                    admitted_under_active_set,
                    admitted_under_pack_versions,
                    gdu_canonical, ldu_t_id
                ) VALUES (
                    %s, 'topic', %s,
                    %s,
                    1,
                    %s, %s::jsonb, %s, %s
                )
                RETURNING topic_id
                """,
                (
                    tenant_id, label,
                    normalized,
                    stamp.get("active_set_version"),
                    json.dumps(stamp.get("pack_versions") or {}),
                    bool(stamp.get("gdu_canonical")),
                    ldu_t_id,
                ),
            )
            row = cur.fetchone()
        conn.commit()
        return row[0] if row else None
    except Exception as e:
        log.warning("upsert_topic(%s) failed: %s", label, e)
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def resolve_topics(
    envelope: dict,
    conn,
    *,
    tenant_id: str = "default",
    ldu_t_id: Optional[int] = None,
) -> dict[str, int]:
    stats = {"resolved": 0, "created": 0, "skipped": 0, "failed": 0}
    if conn is None:
        return stats

    topics = envelope.get("topics") or []
    for topic in topics:
        try:
            label = topic.get("label") or topic.get("name_raw") or ""
            if not label or len(label) < 2:
                stats["skipped"] += 1
                continue

            topic_id = _upsert_topic(
                conn,
                tenant_id=tenant_id,
                label=label,
                ldu_t_id=ldu_t_id,
            )
            if topic_id is None:
                stats["failed"] += 1
                continue

            topic["node_id"] = f"topic:{topic_id}"
            stats["resolved"] += 1
        except Exception as e:
            log.warning("resolve topic failed: %s", e)
            stats["failed"] += 1

    return stats
