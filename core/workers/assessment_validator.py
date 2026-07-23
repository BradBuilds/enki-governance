"""
Assessment Validator Worker — cross-source corroboration of facts and claims.

Picks up knowledge_nodes with node_type='assessment' and enrichment_status='pending'.
For each new claim/fact, it:

  1. Finds semantically similar existing assessments (text similarity)
  2. Determines if they agree, contradict, or are unrelated
  3. Creates supports/contrasts edges between them
  4. Updates corroboration_count and contradiction_count
  5. Recomputes status: confirmed | supported | contested | stale | unverified
  6. Promotes claims → facts when corroborations >= 2 and contradictions == 0

This is where the GDU produces real intelligence. A claim becomes a fact when
multiple independent sources agree. A fact gets demoted when contradictions
emerge. Status changes are deterministic — driven by integer counts, not LLM
opinion.

Status state machine:
  contested   = contradictions > 0 AND corroborations == 0
  confirmed   = corroborations >= 2 AND contradictions == 0
  supported   = corroborations >= 1 AND contradictions == 0
  stale       = temporal_relevance < 0.3
  unverified  = default — no evidence yet
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.assessment_validator")

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "validated": 0,
    "supports_created": 0,
    "contrasts_created": 0,
    "promotions": 0,
    "demotions": 0,
    "errors": 0,
}

CHECK_INTERVAL = 30
BATCH_SIZE = 10
SIMILARITY_THRESHOLD = 0.65   # cosine/jaccard threshold for "same claim"
PROMOTION_THRESHOLD = 2        # corroborations needed to promote claim → fact


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_worker_loop, args=(tenant_id,),
        name="assessment-validator", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    log.info("Assessment validator worker started")
    return {"status": "started"}


def stop_worker():
    """Signal the worker thread to stop and detach it unconditionally.

    Previously this only set the stop event + tried to join, leaving the
    module-level _WORKER_THREAD reference in place. If the old thread was
    stuck in a hung DB query, join(timeout=10) returned with the thread
    still alive, and the next start_worker() call would see
    `_WORKER_THREAD.is_alive() == True` and silently return
    `already_running` — supervisor then thought the worker had died (its
    status check sees running=False) and the restart loop never made
    progress. The June 3 2026 walkthrough caught this:
    assessment_validator was stuck for hours, blocking fact promotion.

    Force-detaching here means the next start_worker() always spawns a
    fresh thread. The orphaned hung thread is a daemon so it dies on
    process exit; in the meantime it sits doing nothing harmful (its
    next DB query either errors via statement_timeout or it stays stuck
    holding a connection — see _process_batch for the timeout fix).
    """
    global _WORKER_THREAD
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=5)
    _WORKER_THREAD = None
    _STATUS["running"] = False
    return {"status": "stopped"}


def _worker_loop(tenant_id: str):
    _STATUS["running"] = True
    backoff = CHECK_INTERVAL

    while not _STOP_EVENT.is_set():
        try:
            processed = _process_batch(tenant_id)
            if processed > 0:
                backoff = CHECK_INTERVAL
                _STATUS["phase"] = "processing"
            else:
                backoff = min(backoff + 30, 180)
                _STATUS["phase"] = "idle — no pending assessments"
        except Exception as e:
            log.warning("Assessment validator error: %s", e)
            _STATUS["errors"] += 1
            _STATUS["phase"] = f"error: {str(e)[:60]}"
            backoff = min(backoff * 2, 600)

        for _ in range(int(backoff)):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)

    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"


def _process_batch(tenant_id: str) -> int:
    nodes = _find_pending_assessments(tenant_id, BATCH_SIZE)
    if not nodes:
        # On idle ticks, run a sweep to demote stale and recompute status
        _sweep_existing(tenant_id)
        return 0

    processed = 0
    for node in nodes:
        try:
            _validate_assessment(node, tenant_id)
            processed += 1
        except Exception as e:
            log.debug("Assessment validation failed for %s: %s", node["node_id"], e)
            _STATUS["errors"] += 1

    _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
    return processed


def _find_pending_assessments(tenant_id: str, limit: int) -> list:
    """Find assessment nodes that haven't been cross-referenced yet."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # statement_timeout caps any single query at 30s so a slow
                # or hung query can't pin the worker thread indefinitely —
                # supervisor was unable to restart this worker for hours
                # on June 3 because the old thread was stuck in a DB call
                # and join(timeout=10) couldn't reach it. Per-session GUC,
                # cleared when the connection returns to the pool.
                cur.execute("SET LOCAL statement_timeout = '30s'")
                cur.execute("""
                    SELECT node_id, title, subtype, description, envelope, source_count, created_at
                    FROM knowledge_nodes
                    WHERE tenant_id = %s
                      AND node_type = 'assessment'
                      AND (enrichment_status = 'pending' OR enrichment_status IS NULL)
                      AND title IS NOT NULL
                      AND LENGTH(title) >= 5
                    ORDER BY created_at ASC
                    LIMIT %s
                """, (tenant_id, limit))

                return [
                    {
                        "node_id": r[0],
                        "title": r[1],
                        "subtype": r[2],
                        "description": r[3] or "",
                        "envelope": r[4] or {},
                        "source_count": r[5] or 1,
                        "created_at": r[6],
                    }
                    for r in cur.fetchall()
                ]
    except Exception as e:
        log.debug("Failed to find pending assessments: %s", e)
        return []


def _validate_assessment(node: dict, tenant_id: str):
    """Cross-reference an assessment against existing assessments.

    Finds similar claims/facts, determines agreement/contradiction,
    creates edges, updates counts, and recomputes status.
    """
    node_id = node["node_id"]
    title = node["title"]
    description = node["description"]
    subtype = node["subtype"]  # 'fact' or 'claim'

    # The text content to compare against
    full_text = f"{title}. {description}".strip()

    # Find candidate similar assessments
    candidates = _find_similar_assessments(tenant_id, node_id, full_text)

    corroborations = 0
    contradictions = 0
    supports_edges = []
    contrasts_edges = []

    for cand in candidates:
        relation = _determine_relation(full_text, f"{cand['title']}. {cand['description']}".strip())

        if relation == "agree":
            corroborations += 1
            supports_edges.append(cand["node_id"])
        elif relation == "contradict":
            contradictions += 1
            contrasts_edges.append(cand["node_id"])
        # 'unrelated' → no edge

    # Compute temporal_relevance (decay over time)
    age_hours = 0
    if node.get("created_at"):
        age = datetime.now(timezone.utc) - node["created_at"].replace(tzinfo=timezone.utc) if node["created_at"].tzinfo is None else datetime.now(timezone.utc) - node["created_at"]
        age_hours = age.total_seconds() / 3600
    temporal_relevance = _compute_temporal_relevance(subtype, age_hours)

    # Compute status from counts
    status = _compute_status(corroborations, contradictions, temporal_relevance)

    # Compute completeness based on evidence
    completeness_score = 0.3  # base for an assessment
    if corroborations > 0:
        completeness_score = min(0.5 + corroborations * 0.1, 0.85)
    if contradictions > 0:
        completeness_score = max(completeness_score - 0.15, 0.2)
    completeness_tier = _score_to_tier(completeness_score)

    # Promotion: claim → fact when conditions met
    new_subtype = subtype
    promoted = False
    if subtype == "claim" and corroborations >= PROMOTION_THRESHOLD and contradictions == 0:
        new_subtype = "fact"
        promoted = True
        _STATUS["promotions"] += 1
        log.info("Promoting %s claim → fact (%d corroborations): %s",
                 node_id, corroborations, title[:60])

    # Write everything to the database
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Update the assessment node
                envelope = node["envelope"] or {}
                envelope["evidence"] = {
                    "source_count": node["source_count"],
                    "corroboration_count": corroborations,
                    "contradiction_count": contradictions,
                    "agreement_ratio": _compute_agreement_ratio(corroborations, contradictions),
                    "temporal_relevance": round(temporal_relevance, 2),
                    "status": status,
                    "validated_at": datetime.now(timezone.utc).isoformat(),
                }
                envelope["validation"] = {
                    "status": "validated",
                    "source": "Cross-Source",
                    "method": "semantic_similarity",
                    "confidence": completeness_score,
                    "validated_at": datetime.now(timezone.utc).isoformat(),
                }

                cur.execute("""
                    UPDATE knowledge_nodes
                    SET envelope = %s,
                        enrichment_status = 'complete',
                        completeness_score = %s,
                        completeness_tier = %s,
                        subtype = %s,
                        updated_at = NOW()
                    WHERE node_id = %s AND tenant_id = %s
                """, (
                    json.dumps(envelope),
                    completeness_score,
                    completeness_tier,
                    new_subtype,
                    node_id, tenant_id,
                ))

                # Create supports edges
                for target in supports_edges:
                    edge_id = f"sup_{node_id[-12:]}_{target[-12:]}"[:255]
                    cur.execute("""
                        INSERT INTO knowledge_edges
                            (edge_id, project_id, source_node_id, target_node_id,
                             relationship, strength, evidence, ai_generated)
                        VALUES (%s, 'default', %s, %s, 'supports', 0.85,
                                'cross-source corroboration', TRUE)
                        ON CONFLICT (source_node_id, target_node_id, relationship)
                        DO UPDATE SET strength = LEAST(knowledge_edges.strength + 0.05, 1.0)
                    """, (edge_id, node_id, target))
                    _STATUS["supports_created"] += 1

                    # Bump the target's corroboration count too
                    _bump_evidence_count(cur, target, "corroboration_count")

                # Create contrasts edges
                for target in contrasts_edges:
                    edge_id = f"con_{node_id[-12:]}_{target[-12:]}"[:255]
                    cur.execute("""
                        INSERT INTO knowledge_edges
                            (edge_id, project_id, source_node_id, target_node_id,
                             relationship, strength, evidence, ai_generated)
                        VALUES (%s, 'default', %s, %s, 'contrasts', 0.85,
                                'cross-source contradiction', TRUE)
                        ON CONFLICT (source_node_id, target_node_id, relationship)
                        DO UPDATE SET strength = LEAST(knowledge_edges.strength + 0.05, 1.0)
                    """, (edge_id, node_id, target))
                    _STATUS["contrasts_created"] += 1

                    # Bump target's contradiction count and possibly demote
                    _bump_evidence_count(cur, target, "contradiction_count")
                    _maybe_demote(cur, target, tenant_id)

                # Log promotion
                if promoted:
                    cur.execute("""
                        INSERT INTO governance_conflicts
                            (tenant_id, conflict_type, severity,
                             competing_values, auto_resolved, auto_resolution,
                             detected_by, metadata)
                        VALUES (%s, 'epistemic_promotion', 'info',
                                %s::jsonb, TRUE, %s,
                                'assessment_validator', %s::jsonb)
                        ON CONFLICT DO NOTHING
                    """, (
                        tenant_id,
                        json.dumps([{"value": "claim", "source": "initial"},
                                    {"value": "fact", "source": "cross-source"}]),
                        f"promoted to fact ({corroborations} corroborations)",
                        json.dumps({"node_id": node_id, "title": title[:200]}),
                    ))

            conn.commit()

        _STATUS["validated"] += 1
        if status == "contested":
            log.info("Contested assessment %s — %d sources support, %d contradict: %s",
                     node_id, corroborations, contradictions, title[:60])
        elif status == "confirmed":
            log.info("Confirmed assessment %s — %d corroborations: %s",
                     node_id, corroborations, title[:60])

    except Exception as e:
        log.warning("Failed to store assessment validation for %s: %s", node_id, e)
        _STATUS["errors"] += 1


def _is_credible_source(node_id: str, conn) -> bool:
    """Check if an assessment came from a credible source.

    Satire never produces credible facts. State media don't count as
    independent corroborations. Aggregators point elsewhere.
    """
    try:
        with conn.cursor() as cur:
            # Find the source document via derived_from edge
            cur.execute("""
                SELECT kn.envelope->'validation' as v
                FROM knowledge_edges ke
                JOIN knowledge_nodes kn ON kn.node_id = ke.target_node_id
                WHERE ke.source_node_id = %s AND ke.relationship = 'derived_from'
                  AND kn.node_type = 'document'
                LIMIT 1
            """, (node_id,))
            row = cur.fetchone()
            if not row or not row[0]:
                return True  # unknown source, assume credible
            v = row[0]
            if v.get("is_satire"):
                return False
            return True
    except Exception:
        return True


def _find_similar_assessments(
    tenant_id: str, exclude_id: str, full_text: str, limit: int = 10
) -> list:
    """Find existing assessments with similar text content.

    Uses PostgreSQL's pg_trgm similarity (already in pgvector schema)
    or falls back to simple keyword overlap if not available.
    """
    if not full_text or len(full_text) < 10:
        return []

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Try trigram similarity first (works without embeddings)
                cur.execute("""
                    SELECT node_id, title, description, subtype,
                           similarity(LOWER(title || ' ' || COALESCE(description, '')), LOWER(%s)) as sim
                    FROM knowledge_nodes
                    WHERE tenant_id = %s
                      AND node_type = 'assessment'
                      AND node_id != %s
                      AND title IS NOT NULL
                      AND similarity(LOWER(title || ' ' || COALESCE(description, '')), LOWER(%s)) > 0.3
                    ORDER BY sim DESC
                    LIMIT %s
                """, (full_text, tenant_id, exclude_id, full_text, limit))

                return [
                    {
                        "node_id": r[0], "title": r[1], "description": r[2] or "",
                        "subtype": r[3], "similarity": float(r[4]),
                    }
                    for r in cur.fetchall()
                ]
    except Exception as e:
        log.debug("Trigram search failed (%s), falling back to keyword overlap", e)

    # Fallback: simple keyword overlap
    try:
        keywords = _extract_keywords(full_text)
        if not keywords:
            return []

        with get_connection() as conn:
            with conn.cursor() as cur:
                # Match nodes that share keywords
                pattern = "|".join(re.escape(k) for k in keywords[:5])
                cur.execute("""
                    SELECT node_id, title, description, subtype
                    FROM knowledge_nodes
                    WHERE tenant_id = %s
                      AND node_type = 'assessment'
                      AND node_id != %s
                      AND title ~* %s
                    LIMIT %s
                """, (tenant_id, exclude_id, pattern, limit))

                results = []
                for r in cur.fetchall():
                    cand_text = f"{r[1]}. {r[2] or ''}".lower()
                    sim = _jaccard_similarity(full_text.lower(), cand_text)
                    if sim > 0.3:
                        results.append({
                            "node_id": r[0], "title": r[1],
                            "description": r[2] or "", "subtype": r[3],
                            "similarity": sim,
                        })
                return sorted(results, key=lambda x: -x["similarity"])
    except Exception as e:
        log.debug("Keyword fallback failed: %s", e)
        return []


def _determine_relation(text_a: str, text_b: str) -> str:
    """Determine if two assessment texts agree, contradict, or are unrelated.

    Uses a deterministic heuristic — no LLM needed for the common cases:
    - Same key entities + same key claim words → agree
    - Same key entities + opposite negation → contradict
    - Different key entities → unrelated

    Returns: 'agree' | 'contradict' | 'unrelated'
    """
    a = text_a.lower().strip()
    b = text_b.lower().strip()

    if not a or not b:
        return "unrelated"

    # Substance check — refuse to compare anything too short to be a real claim.
    # "27 years" vs "years" are not the same fact even though they share tokens.
    if len(a) < 20 or len(b) < 20:
        return "unrelated"

    # Substance check — must contain at least 4 distinct meaningful tokens.
    # Pure numeric/durational text has too few unique tokens to make claims.
    tokens_a = set(re.findall(r"\b[a-z]{3,}\b", a))
    tokens_b = set(re.findall(r"\b[a-z]{3,}\b", b))
    if len(tokens_a) < 4 or len(tokens_b) < 4:
        return "unrelated"

    # Exact match — definitely agree
    if a == b:
        return "agree"

    # Negation detection: same text but one has "not", "no", "never", "denies"
    negation_terms = [" not ", " no ", " never ", " denies ", " denied ",
                      " disputes ", " contradicts ", " refutes ", " rejects "]
    a_negated = any(t in f" {a} " for t in negation_terms)
    b_negated = any(t in f" {b} " for t in negation_terms)

    # Strip negations to compare core content
    a_clean = a
    b_clean = b
    for t in negation_terms:
        a_clean = a_clean.replace(t.strip(), "")
        b_clean = b_clean.replace(t.strip(), "")

    similarity = _jaccard_similarity(a_clean, b_clean)

    # Strong content overlap
    if similarity > 0.7:
        if a_negated != b_negated:
            return "contradict"  # one says X, other says not-X
        return "agree"

    # Medium overlap — likely related
    if similarity > 0.45:
        if a_negated != b_negated:
            return "contradict"
        return "agree"

    return "unrelated"


def _jaccard_similarity(a: str, b: str) -> float:
    """Token-level Jaccard similarity."""
    if not a or not b:
        return 0.0
    tokens_a = set(re.findall(r"\b\w{3,}\b", a.lower()))
    tokens_b = set(re.findall(r"\b\w{3,}\b", b.lower()))
    # Drop stopwords
    stopwords = {"the", "and", "for", "with", "that", "this", "from", "are",
                 "was", "were", "have", "has", "had", "been", "will", "would",
                 "said", "say", "says", "but", "not", "any", "all", "may",
                 "can", "could", "should"}
    tokens_a -= stopwords
    tokens_b -= stopwords
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = len(tokens_a & tokens_b)
    union = len(tokens_a | tokens_b)
    return intersection / union if union > 0 else 0.0


def _extract_keywords(text: str, top_n: int = 8) -> list[str]:
    """Pull the most distinctive keywords from a text."""
    tokens = re.findall(r"\b[A-Z][a-z]{2,}\b", text)  # proper nouns
    if not tokens:
        tokens = re.findall(r"\b\w{4,}\b", text.lower())
    seen = set()
    result = []
    for t in tokens:
        tl = t.lower()
        if tl not in seen:
            seen.add(tl)
            result.append(t)
        if len(result) >= top_n:
            break
    return result


def _compute_temporal_relevance(subtype: str, age_hours: float) -> float:
    """Decay function based on subtype.

    Facts are durable. Claims decay over a week. Hypotheses decay faster.
    """
    if subtype == "fact":
        return 1.0  # facts don't decay
    if subtype == "observation":
        # Observations stay relevant for a few days
        return max(0.0, 1.0 - (age_hours / (24 * 30)))  # 30 days to zero
    if subtype == "claim":
        # Claims decay over a week
        return max(0.0, 1.0 - (age_hours / (24 * 7)))
    if subtype == "hypothesis":
        # Hypotheses decay over 2 weeks
        return max(0.0, 1.0 - (age_hours / (24 * 14)))
    return 1.0


def _compute_status(corroborations: int, contradictions: int, temporal_relevance: float) -> str:
    """The status state machine — deterministic, integer-driven."""
    if temporal_relevance < 0.3:
        return "stale"
    if contradictions > 0 and corroborations == 0:
        return "contested"
    if corroborations >= 2 and contradictions == 0:
        return "confirmed"
    if corroborations >= 1 and contradictions == 0:
        return "supported"
    if contradictions > 0 and corroborations > 0:
        return "contested"  # mixed signals = contested
    return "unverified"


def _compute_agreement_ratio(corroborations: int, contradictions: int) -> float:
    total = corroborations + contradictions
    if total == 0:
        return 1.0
    return round(corroborations / total, 2)


def _score_to_tier(score: float) -> str:
    if score >= 0.90:
        return "verified"
    if score >= 0.70:
        return "solid"
    if score >= 0.40:
        return "partial"
    return "stub"


def _bump_evidence_count(cur, target_node_id: str, field: str):
    """Increment corroboration_count or contradiction_count on a target node."""
    cur.execute("""
        UPDATE knowledge_nodes
        SET envelope = jsonb_set(
            COALESCE(envelope, '{}'::jsonb),
            ARRAY['evidence', %s],
            to_jsonb(COALESCE((envelope->'evidence'->>%s)::int, 0) + 1)
        ),
        updated_at = NOW()
        WHERE node_id = %s
    """, (field, field, target_node_id))


def _maybe_demote(cur, node_id: str, tenant_id: str):
    """If a target was confirmed/verified and got a contradiction, demote."""
    cur.execute("""
        SELECT subtype, completeness_tier,
               (envelope->'evidence'->>'corroboration_count')::int as corr,
               (envelope->'evidence'->>'contradiction_count')::int as contr
        FROM knowledge_nodes
        WHERE node_id = %s AND tenant_id = %s
    """, (node_id, tenant_id))
    row = cur.fetchone()
    if not row:
        return
    subtype, tier, corr, contr = row[0], row[1], row[2] or 0, row[3] or 0

    # If a fact got contradicted, demote to claim and update status
    if subtype == "fact" and contr > 0:
        cur.execute("""
            UPDATE knowledge_nodes
            SET subtype = 'claim',
                completeness_tier = CASE
                    WHEN %s = 'verified' THEN 'solid'
                    WHEN %s = 'solid' THEN 'partial'
                    ELSE completeness_tier
                END,
                envelope = jsonb_set(
                    envelope,
                    ARRAY['evidence', 'status'],
                    '"contested"'::jsonb
                ),
                updated_at = NOW()
            WHERE node_id = %s
        """, (tier, tier, node_id))
        _STATUS["demotions"] += 1
        log.info("Demoted %s fact → claim (contradiction emerged)", node_id)


def _sweep_existing(tenant_id: str):
    """Periodic sweep — recompute temporal_relevance and demote stale assessments."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Find assessments that should now be stale
                cur.execute("""
                    SELECT node_id, subtype, created_at,
                           (envelope->'evidence'->>'status') as current_status
                    FROM knowledge_nodes
                    WHERE tenant_id = %s
                      AND node_type = 'assessment'
                      AND enrichment_status = 'complete'
                      AND subtype IN ('claim', 'hypothesis', 'observation')
                      AND created_at < NOW() - INTERVAL '7 days'
                      AND (envelope->'evidence'->>'status') != 'stale'
                    LIMIT 50
                """, (tenant_id,))

                for row in cur.fetchall():
                    nid, subtype, created, current_status = row
                    age_hours = (datetime.now(timezone.utc) - created.replace(tzinfo=timezone.utc) if created.tzinfo is None else datetime.now(timezone.utc) - created).total_seconds() / 3600
                    rel = _compute_temporal_relevance(subtype, age_hours)
                    if rel < 0.3:
                        cur.execute("""
                            UPDATE knowledge_nodes
                            SET envelope = jsonb_set(
                                envelope,
                                ARRAY['evidence', 'status'],
                                '"stale"'::jsonb
                            ),
                            envelope = jsonb_set(
                                envelope,
                                ARRAY['evidence', 'temporal_relevance'],
                                to_jsonb(%s::numeric)
                            ),
                            updated_at = NOW()
                            WHERE node_id = %s
                        """, (round(rel, 2), nid))
            conn.commit()
    except Exception as e:
        log.debug("Stale sweep failed: %s", e)
