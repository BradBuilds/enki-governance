"""
Contextual Scoring — computes confidence and status per-thread/case.

The same node can have different confidence in different contexts because
different edges and sources matter in different investigations.

This module answers: "How confident should Thread X be about Node Y?"

Scoring algorithm:
  1. Find all edges connecting this node to OTHER nodes in the same context
  2. Count supports (corroborations) and contrasts (contradictions) WITHIN context
  3. Check which of this node's source signals are relevant to the context topic
  4. Combine with node's baseline evidence quality
  5. Classify status: confirmed | supported | contested | unverified | stale

Usage:
    from core.knowledge.context_scoring import score_thread_nodes, score_node_in_thread

    # Score all nodes in a thread
    score_thread_nodes("thread-abc", "default")

    # Score one node in a thread
    result = score_node_in_thread("node-123", "thread-abc", "default")
    # result = {"confidence": 0.82, "status": "supported", ...}
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.context_scoring")


def score_node_in_thread(
    node_id: str,
    thread_id: str,
    tenant_id: str,
    dsn: Optional[str] = None,
) -> dict:
    """Score a single node within a thread's context.

    Returns the full contextual assessment and writes it to thread_nodes.
    """
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # 1. Get the node's baseline data
            cur.execute("""
                SELECT n.node_id, n.confidence, n.source_authority, n.source_count,
                       n.entity_name, n.entity_type, n.event_time, n.latitude,
                       n.enrichment_status, n.gaps, n.gap_count,
                       n.metadata
                FROM knowledge_nodes n
                WHERE n.node_id = %s AND n.tenant_id = %s
            """, (node_id, tenant_id))
            node_row = cur.fetchone()
            if not node_row:
                return {"error": "node not found"}

            baseline_quality = node_row[1] or 0.5
            authority = node_row[2] or "unverified"
            source_count = node_row[3] or 0
            entity_name = node_row[4]
            event_time = node_row[6]
            gaps = node_row[9] or []
            metadata = _parse_jsonb(node_row[11])

            # 2. Get all OTHER node_ids in this thread (the context graph)
            cur.execute("""
                SELECT node_id FROM thread_nodes
                WHERE thread_id = %s AND relevance IN ('pinned', 'active')
                  AND node_id != %s
            """, (thread_id, node_id))
            context_node_ids = [r[0] for r in cur.fetchall()]

            # 3. Count supports/contrasts edges WITHIN this thread's graph
            context_corroborations = 0
            context_contradictions = 0

            if context_node_ids:
                # Edges where this node supports/is supported by context nodes
                cur.execute("""
                    SELECT relationship, COUNT(*) FROM knowledge_edges
                    WHERE (
                        (source_node_id = %s AND target_node_id = ANY(%s))
                        OR (target_node_id = %s AND source_node_id = ANY(%s))
                    )
                    AND relationship IN ('supports', 'contrasts')
                    GROUP BY relationship
                """, (node_id, context_node_ids, node_id, context_node_ids))
                for rel, count in cur.fetchall():
                    if rel == "supports":
                        context_corroborations = count
                    elif rel == "contrasts":
                        context_contradictions = count

            # 4. Compute temporal relevance (how fresh is this node?)
            temporal_relevance = 1.0
            staleness_class = metadata.get("staleness_class", "temporal")
            if event_time and staleness_class != "durable":
                try:
                    et = event_time if isinstance(event_time, datetime) else datetime.fromisoformat(str(event_time).replace("Z", "+00:00"))
                    age_hours = (datetime.now(timezone.utc) - et.replace(tzinfo=timezone.utc if et.tzinfo is None else et.tzinfo)).total_seconds() / 3600
                    if staleness_class == "volatile":
                        # Volatile: half-life 48 hours
                        temporal_relevance = max(0.0, 1.0 - (age_hours / 336))
                    else:
                        # Temporal: half-life varies by entity type
                        ttl_hours = {"vessel": 6, "aircraft": 2, "device": 4, "person": 24, "vehicle": 12}.get(
                            node_row[5] or "", 24
                        )
                        temporal_relevance = max(0.0, 1.0 - (age_hours / (ttl_hours * 4)))
                except (ValueError, TypeError):
                    pass

            # 5. Compute contextual confidence
            score = baseline_quality * 0.4  # baseline contributes 40%

            # Source strength
            if source_count >= 3:
                score += 0.15
            elif source_count >= 2:
                score += 0.1
            elif source_count >= 1:
                score += 0.05

            # Authority
            auth_score = {"verified": 0.2, "trusted": 0.15, "unverified": 0.05, "disputed": 0.0}.get(authority, 0.05)
            score += auth_score

            # Context corroborations (these nodes in THIS thread agree)
            score += min(context_corroborations * 0.1, 0.2)

            # Context contradictions (these nodes in THIS thread conflict)
            score -= min(context_contradictions * 0.15, 0.3)

            # Temporal relevance
            score *= max(0.3, temporal_relevance)  # floor at 0.3 so stale nodes don't vanish

            confidence = max(0.0, min(1.0, round(score, 2)))

            # 6. Compute status
            if temporal_relevance < 0.2:
                status = "stale"
            elif context_contradictions > 0 and context_corroborations == 0:
                status = "contested"
            elif context_corroborations >= 2 and context_contradictions == 0:
                status = "confirmed"
            elif context_corroborations >= 1:
                status = "supported"
            else:
                status = "unverified"

            # 7. Context-specific gaps
            context_gaps = list(gaps)  # start with intrinsic gaps
            if context_contradictions > 0:
                context_gaps.append(f"{context_contradictions} contradiction(s) within this thread")
            if temporal_relevance < 0.5 and staleness_class != "durable":
                context_gaps.append("Data may be stale — last update was not recent")

            # 8. Build evidence profile
            evidence = {
                "baseline_quality": baseline_quality,
                "source_count": source_count,
                "authority": authority,
                "temporal_relevance": round(temporal_relevance, 2),
                "context_corroborations": context_corroborations,
                "context_contradictions": context_contradictions,
                "context_node_count": len(context_node_ids),
                "staleness_class": staleness_class,
                "gap_count": len(context_gaps),
            }

            # 9. Write to thread_nodes
            cur.execute("""
                UPDATE thread_nodes SET
                    context_confidence = %s,
                    context_status = %s,
                    context_evidence = %s,
                    context_contradictions = %s,
                    context_corroborations = %s,
                    context_gaps = %s,
                    context_scored_at = NOW(),
                    updated_at = NOW()
                WHERE thread_id = %s AND node_id = %s
            """, (
                confidence, status, json.dumps(evidence),
                context_contradictions, context_corroborations,
                context_gaps, thread_id, node_id,
            ))

        conn.commit()

    return {
        "node_id": node_id,
        "confidence": confidence,
        "status": status,
        "evidence": evidence,
        "gaps": context_gaps,
    }


def score_thread_nodes(
    thread_id: str,
    tenant_id: str,
    dsn: Optional[str] = None,
) -> list[dict]:
    """Score ALL active/pinned nodes in a thread. Returns list of results."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT node_id FROM thread_nodes
                WHERE thread_id = %s AND relevance IN ('pinned', 'active')
            """, (thread_id,))
            node_ids = [r[0] for r in cur.fetchall()]

    results = []
    for nid in node_ids:
        result = score_node_in_thread(nid, thread_id, tenant_id, dsn)
        results.append(result)

    return results


def score_node_in_case(
    node_id: str,
    case_id: str,
    tenant_id: str,
    dsn: Optional[str] = None,
) -> dict:
    """Score a single node within a case's context."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Get node baseline
            cur.execute("""
                SELECT n.confidence, n.source_authority, n.source_count,
                       n.entity_type, n.event_time, n.gaps, n.metadata
                FROM knowledge_nodes n
                WHERE n.node_id = %s AND n.tenant_id = %s
            """, (node_id, tenant_id))
            node_row = cur.fetchone()
            if not node_row:
                return {"error": "node not found"}

            baseline_quality = node_row[0] or 0.5
            authority = node_row[1] or "unverified"
            source_count = node_row[2] or 0
            gaps = node_row[5] or []
            metadata = _parse_jsonb(node_row[6])

            # Get all other nodes in this case
            cur.execute("""
                SELECT node_id FROM case_nodes
                WHERE case_id = %s AND node_id != %s
            """, (case_id, node_id))
            case_node_ids = [r[0] for r in cur.fetchall()]

            # Count edges within case graph
            context_corroborations = 0
            context_contradictions = 0
            if case_node_ids:
                cur.execute("""
                    SELECT relationship, COUNT(*) FROM knowledge_edges
                    WHERE (
                        (source_node_id = %s AND target_node_id = ANY(%s))
                        OR (target_node_id = %s AND source_node_id = ANY(%s))
                    )
                    AND relationship IN ('supports', 'contrasts')
                    GROUP BY relationship
                """, (node_id, case_node_ids, node_id, case_node_ids))
                for rel, count in cur.fetchall():
                    if rel == "supports":
                        context_corroborations = count
                    elif rel == "contrasts":
                        context_contradictions = count

            # Compute score (same algorithm as thread)
            score = baseline_quality * 0.4
            if source_count >= 3: score += 0.15
            elif source_count >= 2: score += 0.1
            elif source_count >= 1: score += 0.05
            auth_score = {"verified": 0.2, "trusted": 0.15, "unverified": 0.05, "disputed": 0.0}.get(authority, 0.05)
            score += auth_score
            score += min(context_corroborations * 0.1, 0.2)
            score -= min(context_contradictions * 0.15, 0.3)
            confidence = max(0.0, min(1.0, round(score, 2)))

            # Status
            if context_contradictions > 0 and context_corroborations == 0:
                status = "contested"
            elif context_corroborations >= 2 and context_contradictions == 0:
                status = "confirmed"
            elif context_corroborations >= 1:
                status = "supported"
            else:
                status = "unverified"

            # Write to case_nodes
            cur.execute("""
                UPDATE case_nodes SET
                    context_confidence = %s,
                    context_status = %s,
                    context_evidence = %s,
                    context_contradictions = %s,
                    context_corroborations = %s,
                    context_scored_at = NOW()
                WHERE case_id = %s AND node_id = %s
            """, (
                confidence, status,
                json.dumps({
                    "baseline_quality": baseline_quality,
                    "source_count": source_count,
                    "authority": authority,
                    "context_corroborations": context_corroborations,
                    "context_contradictions": context_contradictions,
                    "context_node_count": len(case_node_ids),
                }),
                context_contradictions, context_corroborations,
                case_id, node_id,
            ))
        conn.commit()

    return {
        "node_id": node_id, "confidence": confidence, "status": status,
        "context_corroborations": context_corroborations,
        "context_contradictions": context_contradictions,
    }


def score_case_nodes(case_id: str, tenant_id: str, dsn: Optional[str] = None) -> list[dict]:
    """Score ALL nodes in a case."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT node_id FROM case_nodes WHERE case_id = %s", (case_id,))
            node_ids = [r[0] for r in cur.fetchall()]
    return [score_node_in_case(nid, case_id, tenant_id, dsn) for nid in node_ids]


def _parse_jsonb(val) -> dict:
    if val is None: return {}
    if isinstance(val, dict): return val
    if isinstance(val, memoryview): return json.loads(bytes(val))
    if isinstance(val, str):
        try: return json.loads(val)
        except: return {}
    return {}
