"""Topic materializer + creation helper.

Two responsibilities:
  1. create_topic(): admit a new topic through the GDU gateway, persist
     the membership_rule, and (optionally) materialize once on creation.
  2. materialize_topic(): re-evaluate a topic's rule and refresh the
     topic_membership table. Idempotent — same rule, same substrate, same
     output. Tracks `materialized_at` and `member_count`.

Soft-delete pattern on re-materialization: rows that no longer match get
their `evicted_at` set instead of being deleted, so we have a trail of
how membership has shifted across runs.

No topic-specific code. The materializer doesn't know what any topic is
"about" — it just executes the membership rule.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from storage.postgres.db import get_connection
from core.gdu.gateway import get_gateway
from core.topics.evaluator import evaluate, Member
from core.topics.rules import validate as validate_rule, MembershipRuleError

log = logging.getLogger("enki.topics.materialize")


VALID_CREATED_BY = {
    "manual", "user", "auto_cluster", "watch_topic", "cascade_seed", "api",
}

# Topic subtypes from nodevocab.node_types.json (v0.8.1). The picker uses
# this set to validate; the actual classification is data, not code.
# Intelligence-domain subtypes + general-aboutness subtypes (v0.8.1) so
# Wikipedia-style topics ("Death of X") have honest kinds.
VALID_TOPIC_SUBTYPES = {
    "conflict", "operation", "treaty", "protocol", "technology",
    "ideology", "market", "supply_chain", "program", "doctrine",
    "investigation",
    "event", "relationship", "aspect", "legal", "reference", "theme",
}


def create_topic(
    *, tenant_id: str = "default",
    label: str,
    subtype: str = "investigation",
    description: str = "",
    membership_rule: dict,
    created_by: str = "manual",
    materialize: bool = True,
) -> dict:
    """Admit a new topic through the gateway and persist the rule.

    Returns: {topic_id, base60_address, node_id, member_count}

    Validates the rule shape before admission so we never persist a
    malformed rule. If `materialize=True`, also runs the rule immediately
    and writes topic_membership rows.
    """
    if subtype not in VALID_TOPIC_SUBTYPES:
        raise ValueError(
            f"subtype must be one of {sorted(VALID_TOPIC_SUBTYPES)} "
            f"(from nodevocab.topic), got {subtype!r}"
        )
    if created_by not in VALID_CREATED_BY:
        raise ValueError(
            f"created_by must be one of {sorted(VALID_CREATED_BY)}, "
            f"got {created_by!r}"
        )
    validate_rule(membership_rule)

    # Collapse-key based on rule shape so identical rule → same topic.
    # This dedupes auto-clustered topics that re-fire on the same scope.
    import hashlib
    rule_canonical = json.dumps(membership_rule, sort_keys=True, separators=(",", ":"))
    collapse_key = f"topic:{subtype}:{hashlib.sha1(rule_canonical.encode()).hexdigest()[:16]}"

    gateway = get_gateway()
    result = gateway.admit_node(
        tenant_id=tenant_id,
        category="topic",
        subtype=subtype,
        title=label[:300],
        description=description[:2000],
        signal_rid=0,            # not signal-derived
        collapse_key=collapse_key,
        metadata={"created_by": created_by},
    )
    if not result or not result.node_id:
        raise RuntimeError(
            f"gateway refused topic admission (action={getattr(result, 'action', '?')})"
        )

    # Persist a topics row + attach the membership_rule. The gateway
    # already wrote to knowledge_nodes; this layer holds the rule, the
    # provenance, and the materialization counters. No ON CONFLICT — the
    # topics table doesn't have a unique constraint we can rely on, so
    # we do a SELECT-then-INSERT/UPDATE keyed on (tenant_id, normalized_name).
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT topic_id FROM topics "
                " WHERE tenant_id=%s "
                "   AND normalized_name = lower(regexp_replace(%s, '\\s+', ' ', 'g')) "
                " LIMIT 1",
                (tenant_id, label),
            )
            row = cur.fetchone()
            if row:
                topic_id = row[0]
                cur.execute(
                    """
                    UPDATE topics
                       SET canonical_type = %s,
                           membership_rule = %s::jsonb,
                           created_by = %s,
                           updated_at = NOW()
                     WHERE topic_id = %s
                    """,
                    (subtype, json.dumps(membership_rule), created_by, topic_id),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO topics (
                        tenant_id, canonical_type, canonical_name,
                        normalized_name, mention_count, gdu_canonical,
                        admitted_under_active_set, admitted_under_pack_versions,
                        membership_rule, created_by
                    ) VALUES (
                        %s, %s, %s, lower(regexp_replace(%s, '\\s+', ' ', 'g')),
                        0, TRUE,
                        'v6.2.0', '{}'::jsonb,
                        %s::jsonb, %s
                    )
                    RETURNING topic_id
                    """,
                    (tenant_id, subtype, label, label,
                     json.dumps(membership_rule), created_by),
                )
                topic_id = cur.fetchone()[0]
        conn.commit()

    out = {
        "topic_id": topic_id,
        "base60_address": result.base60_address,
        "node_id": result.node_id,
        "subtype": subtype,
        "label": label,
        "created_by": created_by,
        "member_count": 0,
    }
    if materialize:
        mres = materialize_topic(topic_id)
        out["member_count"] = mres["member_count"]
        out["by_kind"] = mres["by_kind"]

    # Federation Phase 1 — queue this topic for publication to the hub.
    # No-op when ENKI_HUB_URL is unset (single-node mode).
    try:
        from core.federation import publisher as fed_publisher
        fed_publisher.queue_for_publish(topic_id, tenant_id=tenant_id)
    except Exception:
        log.debug("federation queue failed for topic %d", topic_id, exc_info=True)

    # Federation Phase 3 — publish topic members per redaction policy.
    # Default policy is 'none'; only opt-in topics actually transmit.
    try:
        from core.federation import membership_publisher as fed_membership
        fed_membership.publish_membership(topic_id, tenant_id=tenant_id)
    except Exception:
        log.debug("federation membership publish failed for topic %d",
                  topic_id, exc_info=True)

    return out


def materialize_topic(topic_id: int) -> dict:
    """Run the topic's membership_rule and refresh topic_membership.

    Returns: {topic_id, member_count, added, evicted, by_kind}

    Behavior:
      * Members currently in topic_membership but not in the new evaluator
        output get `evicted_at = now()` (soft delete).
      * Members in the new output that aren't in the table get inserted.
      * Members in both get score/via_rule updated.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT membership_rule FROM topics WHERE topic_id = %s",
                (topic_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"topic {topic_id} not found")
            rule_json = row[0]

    if not rule_json:
        return {"topic_id": topic_id, "member_count": 0, "added": 0,
                "evicted": 0, "by_kind": {}}

    rule = rule_json if isinstance(rule_json, dict) else json.loads(rule_json)
    new_members: list[Member] = evaluate(rule)
    new_keys: dict[tuple[str, int], Member] = {(m.kind, m.id): m for m in new_members}

    added = 0
    evicted = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Existing live members
            cur.execute(
                "SELECT member_kind, member_id FROM topic_membership "
                " WHERE topic_id=%s AND evicted_at IS NULL",
                (topic_id,),
            )
            live = {(r[0], r[1]) for r in cur.fetchall()}

            # Evict members no longer in the new set
            to_evict = live - set(new_keys.keys())
            if to_evict:
                cur.executemany(
                    """
                    UPDATE topic_membership SET evicted_at = NOW()
                     WHERE topic_id = %s AND member_kind = %s AND member_id = %s
                    """,
                    [(topic_id, k, i) for k, i in to_evict],
                )
                evicted = cur.rowcount or len(to_evict)

            # Upsert the new set
            if new_keys:
                rows = [
                    (topic_id, m.kind, m.id, m.score, m.via_rule)
                    for m in new_keys.values()
                ]
                cur.executemany(
                    """
                    INSERT INTO topic_membership
                        (topic_id, member_kind, member_id, score, via_rule, added_at, evicted_at)
                    VALUES (%s, %s, %s, %s, %s, NOW(), NULL)
                    ON CONFLICT (topic_id, member_kind, member_id)
                    DO UPDATE SET score = EXCLUDED.score,
                                  via_rule = EXCLUDED.via_rule,
                                  evicted_at = NULL
                    """,
                    rows,
                )
                added = len(rows)

            # Refresh denormalized counters
            cur.execute(
                """
                UPDATE topics
                   SET materialized_at = NOW(),
                       member_count = (
                         SELECT COUNT(*) FROM topic_membership
                          WHERE topic_id=%s AND evicted_at IS NULL
                       )
                 WHERE topic_id = %s
                """,
                (topic_id, topic_id),
            )

        conn.commit()

    # Final per-kind tally
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT member_kind, COUNT(*) FROM topic_membership "
                " WHERE topic_id=%s AND evicted_at IS NULL GROUP BY member_kind",
                (topic_id,),
            )
            by_kind = {r[0]: r[1] for r in cur.fetchall()}

    return {
        "topic_id": topic_id,
        "member_count": sum(by_kind.values()),
        "added": added,
        "evicted": evicted,
        "by_kind": by_kind,
    }


def list_topics(
    *, tenant_id: str = "default", limit: int = 50,
    created_by: Optional[str] = None,
) -> list[dict]:
    """List topics with member-count for the picker UI."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            sql = """
                SELECT topic_id, canonical_name, canonical_type, created_by,
                       member_count, materialized_at
                  FROM topics
                 WHERE tenant_id = %s
            """
            params: list = [tenant_id]
            if created_by:
                sql += " AND created_by = %s"
                params.append(created_by)
            sql += " ORDER BY member_count DESC, topic_id LIMIT %s"
            params.append(limit)
            cur.execute(sql, params)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
