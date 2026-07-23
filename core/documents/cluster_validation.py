"""
Cluster-aware source validation — the multi-signal resolver end-to-end.

Routes each document_mention_cluster (NOT each surface_form_norm globally)
through internal-node and Wikipedia lookups, using the cluster's
context_keywords as disambiguation evidence. This is what makes
"Doe in Acme" resolve to Q1111111 (Jane Doe) and
"Doe in Roswell" resolve to whatever a separate Doe would be —
without falsely collapsing them.

How context scoring works:
  1. For each cluster, fetch up to N Wikipedia search hits with summaries.
  2. For each hit, count how many of the cluster's context_keywords appear
     in the hit's title + summary (case-insensitive token match).
  3. Hit with the highest context_overlap wins. Ties broken by Wikipedia
     search order (more popular article first).
  4. If best context_overlap == 0, fall back to the top hit but mark it
     as low-confidence (no disambiguation evidence).
  5. For review_required corpora, if best context_overlap == 0 AND
     suggested_type is unknown, REJECT the match (refusing to guess on
     adversarial corpora is safer than guessing wrong).

Public surface:

    validate_clusters(corpus_id) -> summary
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.documents.cluster_validation")


def _corpus_trust_class(corpus_id: str) -> Optional[str]:
    """Same helper as in source_validation — DRY in a future refactor."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT trust_class FROM dataset_policies WHERE dataset_id = %s",
                    (corpus_id,),
                )
                row = cur.fetchone()
                if not row:
                    cur.execute(
                        "SELECT trust_class FROM documents WHERE dataset_id = %s LIMIT 1",
                        (corpus_id,),
                    )
                    row = cur.fetchone()
                return row[0] if row else None
    except Exception:
        return None


def _fetch_clusters(corpus_id: str, min_mentions: int = 1) -> list[dict]:
    """Pull every cluster for a corpus. Order by mention_count desc so the
    most-corroborated clusters get routed first (rate-limit friendly)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cluster_id, surface_form_norm, representative_form,
                       mention_count, document_count, context_keywords,
                       suggested_type
                  FROM document_mention_clusters
                 WHERE corpus_id = %s AND mention_count >= %s
                 ORDER BY mention_count DESC, surface_form_norm
                """,
                (corpus_id, min_mentions),
            )
            return [
                {
                    "cluster_id": r[0],
                    "surface_form_norm": r[1],
                    "representative_form": r[2],
                    "mention_count": r[3],
                    "document_count": r[4],
                    "context_keywords": r[5] or [],
                    "suggested_type": r[6],
                }
                for r in cur.fetchall()
            ]


def _save_cluster_lookup(
    corpus_id: str, cluster_id: int, surface_norm: str, source: str,
    external_id: Optional[str], label: Optional[str],
    summary: Optional[str], url: Optional[str],
    matched: bool, confidence: Optional[float],
    raw: Optional[dict],
) -> None:
    """UPSERT one row keyed on (corpus_id, cluster_id, source). The unique
    index is PARTIAL on cluster_id IS NOT NULL, so the ON CONFLICT clause
    must include the same WHERE predicate."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO entity_external_lookups
                    (corpus_id, cluster_id, surface_form_norm, source,
                     external_id, label, summary, url, matched, confidence, raw)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (corpus_id, cluster_id, source)
                WHERE cluster_id IS NOT NULL
                DO UPDATE SET
                    surface_form_norm = EXCLUDED.surface_form_norm,
                    external_id = EXCLUDED.external_id,
                    label = EXCLUDED.label,
                    summary = EXCLUDED.summary,
                    url = EXCLUDED.url,
                    matched = EXCLUDED.matched,
                    confidence = EXCLUDED.confidence,
                    raw = EXCLUDED.raw,
                    looked_up_at = NOW()
                """,
                (
                    corpus_id, cluster_id, surface_norm, source,
                    external_id, label, summary, url, matched, confidence,
                    json.dumps(raw) if raw else None,
                ),
            )
        conn.commit()


def _log_cluster_decision(
    corpus_id: str, cluster_id: int, surface_norm: str,
    action: str, rule_fired: str, score: float, confidence_tier: str,
    entity_id: Optional[int] = None,
    evidence_for: Optional[list] = None, evidence_against: Optional[list] = None,
    notes: Optional[str] = None,
) -> None:
    """Write a row to collapse_decisions with the cluster context."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO collapse_decisions
                        (corpus_id, surface_form_norm, action, rule_fired,
                         score, confidence_tier, entity_id,
                         evidence_for, evidence_against, decided_by, notes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                            %s, %s)
                    """,
                    (
                        corpus_id, surface_norm, action, rule_fired,
                        score, confidence_tier, entity_id,
                        json.dumps(evidence_for or []),
                        json.dumps(evidence_against or []),
                        f"auto:cluster_validation_v1[cluster={cluster_id}]",
                        notes,
                    ),
                )
            conn.commit()
    except Exception as e:
        log.debug("collapse_decisions insert failed: %s", e)


# ─── Context scoring for Wikipedia hits ────────────────────────────────

def _score_hit_against_context(
    hit_title: str, hit_summary: str, context_keywords: list[str],
) -> int:
    """How many of the cluster's context keywords appear in the
    Wikipedia hit's title + summary? Case-insensitive substring match.
    Returns the count — higher = stronger context match."""
    if not context_keywords:
        return 0
    blob = (hit_title + " " + (hit_summary or "")).upper()
    return sum(1 for kw in context_keywords if kw and kw.upper() in blob)


def _route_wikipedia_with_context(
    corpus_id: str, cluster: dict, strict_unknown: bool,
) -> dict:
    """Look up cluster's surface form in Wikipedia, picking the hit whose
    article shares the most context keywords with the cluster."""
    try:
        from core.research.sources import wikipedia_search, wikipedia_summary
        from core.documents.wikidata_types import is_compatible
    except Exception:
        return {"matched": False, "reason": "import_fail"}

    surface = cluster["representative_form"] or cluster["surface_form_norm"]
    surface_norm = cluster["surface_form_norm"]
    cluster_id = cluster["cluster_id"]
    suggested_type = cluster["suggested_type"]
    context_kws = cluster["context_keywords"] or []

    # Step 1: search for up to 5 candidate articles
    try:
        hits = wikipedia_search(surface, limit=5)
    except Exception:
        hits = []

    if not hits:
        _save_cluster_lookup(
            corpus_id=corpus_id, cluster_id=cluster_id,
            surface_norm=surface_norm, source="wikipedia",
            external_id=None, label=None, summary=None, url=None,
            matched=False, confidence=0.0,
            raw={"reason": "no_search_hits"},
        )
        _log_cluster_decision(
            corpus_id, cluster_id, surface_norm,
            "kept_separate", "no_wikipedia_hits", 0.0, "no",
        )
        return {"matched": False, "reason": "no_hits"}

    # Step 2: score each hit by context overlap. Fetch summaries for the
    # top 3 only (rate-limit budget); deeper hits use snippet only.
    scored: list[dict] = []
    for i, h in enumerate(hits[:5]):
        title = h["title"]
        snippet = h.get("snippet", "")
        summary_data = None
        if i < 3:
            try:
                summary_data = wikipedia_summary(title)
            except Exception:
                summary_data = None
        text_blob = (summary_data or {}).get("summary") or snippet
        context_score = _score_hit_against_context(title, text_blob, context_kws)
        scored.append({
            "title": title,
            "summary_data": summary_data,
            "snippet": snippet,
            "context_score": context_score,
            "search_rank": i,
        })

    # Step 3: pick the best — highest context_score, ties broken by
    # original search rank.
    scored.sort(key=lambda x: (-x["context_score"], x["search_rank"]))
    best = scored[0]
    best_data = best["summary_data"]
    qid = (best_data or {}).get("wikidata_id")
    best_context_score = best["context_score"]

    matched = bool(best_data and (best_data.get("summary") or qid))

    # Step 4: type compatibility check
    type_reason = "no_gate"
    if matched and qid:
        compatible, type_reason = is_compatible(
            qid, suggested_type, strict_unknown=strict_unknown,
        )
        if not compatible:
            _save_cluster_lookup(
                corpus_id=corpus_id, cluster_id=cluster_id,
                surface_norm=surface_norm, source="wikipedia",
                external_id=qid, label=best["title"], summary=None, url=None,
                matched=False, confidence=0.0,
                raw={
                    "candidates": [{"title": s["title"],
                                    "context_score": s["context_score"]}
                                   for s in scored],
                    "best_picked_then_rejected": True,
                    "rejected_reason": type_reason,
                    "context_keywords": context_kws,
                    "suggested_type": suggested_type,
                },
            )
            _log_cluster_decision(
                corpus_id, cluster_id, surface_norm,
                "rejected", "wikipedia_type_mismatch", 0.0, "no",
                evidence_against=[{"axis": "wikidata_p31",
                                   "value": type_reason, "weight": -1.0}],
                notes=f"best context-match Wikipedia hit '{best['title']}' "
                      f"({qid}) failed type check: {type_reason}",
            )
            return {"matched": False, "reason": type_reason, "qid": qid}

    # Step 5: strict-unknown for review_required when no context match
    if (matched and strict_unknown and best_context_score == 0
            and (not suggested_type or suggested_type == "unknown")):
        _save_cluster_lookup(
            corpus_id=corpus_id, cluster_id=cluster_id,
            surface_norm=surface_norm, source="wikipedia",
            external_id=qid, label=best["title"], summary=None, url=None,
            matched=False, confidence=0.0,
            raw={"rejected_reason": "no_context_overlap_in_strict_mode",
                 "context_keywords": context_kws},
        )
        _log_cluster_decision(
            corpus_id, cluster_id, surface_norm,
            "rejected", "no_context_overlap_strict_unknown", 0.0, "no",
            notes=f"Wikipedia hit '{best['title']}' had 0 context overlap "
                  f"with cluster keywords {context_kws[:5]}",
        )
        return {"matched": False, "reason": "no_context_strict", "qid": qid}

    # Score the confidence: 0.95 if strong context overlap (>=2),
    # 0.85 if some (1), 0.65 if none (top-hit fallback).
    if best_context_score >= 2:
        confidence = 0.95
        score_tier = "hard"
    elif best_context_score == 1:
        confidence = 0.85
        score_tier = "soft"
    else:
        confidence = 0.65
        score_tier = "suggested"

    _save_cluster_lookup(
        corpus_id=corpus_id, cluster_id=cluster_id,
        surface_norm=surface_norm, source="wikipedia",
        external_id=qid,
        label=best["title"],
        summary=((best_data or {}).get("summary") or "")[:2000],
        url=(best_data or {}).get("url"),
        matched=matched,
        confidence=confidence,
        raw={
            "candidates": [
                {"title": s["title"],
                 "context_score": s["context_score"],
                 "search_rank": s["search_rank"]}
                for s in scored
            ],
            "picked_title": best["title"],
            "picked_context_score": best_context_score,
            "type_check": type_reason,
            "context_keywords": context_kws[:10],
            "suggested_type": suggested_type,
        },
    )
    _log_cluster_decision(
        corpus_id, cluster_id, surface_norm,
        "merged_into", f"cluster_wikipedia_match+ctx{best_context_score}",
        confidence, score_tier,
        evidence_for=[
            {"axis": "external_id", "value": qid or "no_qid", "weight": 0.5},
            {"axis": "context_overlap",
             "value": f"{best_context_score} keywords matched",
             "weight": 0.3 + 0.1 * best_context_score},
            {"axis": "name_match",
             "value": f"surface '{surface}' → wp '{best['title']}'",
             "weight": 0.2},
        ],
        notes=f"cluster #{cluster_id} ({surface_norm}, "
              f"{cluster['mention_count']} mentions): "
              f"picked '{best['title']}' QID={qid} "
              f"context_overlap={best_context_score}/{len(context_kws)} "
              f"type={type_reason}",
    )
    return {
        "matched": matched, "qid": qid, "label": best["title"],
        "context_score": best_context_score,
        "candidates_count": len(scored),
    }


# ─── Orchestrator ──────────────────────────────────────────────────────

def validate_clusters(
    corpus_id: str,
    *,
    min_mentions: int = 1,
    limit: int = 200,
    delay_between_calls: float = 0.2,
) -> dict:
    """Route every cluster (with >= min_mentions members) through
    cluster-aware Wikipedia lookup.

    This is the cluster-level equivalent of validate_corpus. Each cluster
    gets its OWN entity_external_lookups row (keyed on cluster_id), so
    two clusters of the same surface form can resolve to different
    Wikipedia entities."""
    clusters = _fetch_clusters(corpus_id, min_mentions=min_mentions)
    if not clusters:
        return {"corpus_id": corpus_id, "ok": True, "reason": "no clusters"}

    if limit and len(clusters) > limit:
        clusters = clusters[:limit]

    trust_class = _corpus_trust_class(corpus_id)
    strict_unknown = (trust_class == "review_required")

    matched = 0
    type_rejected = 0
    no_context_rejected = 0
    no_hits = 0
    multi_member_clusters = 0
    same_form_different_cluster = 0

    # Track form -> distinct QIDs found across its clusters so we can
    # surface "this surface form resolved to N different entities"
    form_to_qids: dict[str, set[str]] = {}

    sample_with_context: list[dict] = []
    sample_no_context: list[dict] = []
    sample_rejected: list[dict] = []

    for c in clusters:
        if c["mention_count"] > 1:
            multi_member_clusters += 1
        r = _route_wikipedia_with_context(corpus_id, c, strict_unknown)
        sfn = c["surface_form_norm"]
        if r.get("matched"):
            matched += 1
            qid = r.get("qid")
            if qid:
                form_to_qids.setdefault(sfn, set()).add(qid)
            if r.get("context_score", 0) > 0 and len(sample_with_context) < 8:
                sample_with_context.append({
                    "form": c["representative_form"],
                    "qid": qid,
                    "label": r.get("label"),
                    "context_score": r.get("context_score"),
                    "context_keywords": c["context_keywords"][:5],
                })
            elif r.get("context_score", 0) == 0 and len(sample_no_context) < 5:
                sample_no_context.append({
                    "form": c["representative_form"],
                    "qid": qid,
                    "label": r.get("label"),
                })
        elif r.get("reason", "").startswith(("type_mismatch", "strict_unknown",
                                              "creative_work_blocks")):
            type_rejected += 1
            if len(sample_rejected) < 5:
                sample_rejected.append({
                    "form": c["representative_form"],
                    "reason": r.get("reason"),
                    "qid": r.get("qid"),
                })
        elif r.get("reason") == "no_context_strict":
            no_context_rejected += 1
        elif r.get("reason") == "no_hits":
            no_hits += 1
        if delay_between_calls:
            time.sleep(delay_between_calls)

    # How many surface forms resolved to MULTIPLE distinct Wikidata
    # entities across their clusters? That's the disambiguation working.
    forms_with_multiple_qids = sum(
        1 for sfn, qids in form_to_qids.items() if len(qids) > 1
    )

    return {
        "corpus_id": corpus_id,
        "ok": True,
        "clusters_routed": len(clusters),
        "multi_member_clusters": multi_member_clusters,
        "matched": matched,
        "type_rejected": type_rejected,
        "no_context_rejected": no_context_rejected,
        "no_hits": no_hits,
        "forms_resolved_to_multiple_entities": forms_with_multiple_qids,
        "trust_class": trust_class,
        "strict_unknown": strict_unknown,
        "sample_with_context_match": sample_with_context,
        "sample_no_context_fallback": sample_no_context,
        "sample_rejected": sample_rejected,
    }
