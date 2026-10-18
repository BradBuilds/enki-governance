"""predicate_extender.py — validated automation of predicate_dictionary growth.

Three tiers of promotion (most→least automated):

  Tier 1 · morph_v1 — Morphological variant of an existing dict entry
      "killed" / "kills" / "killing" map onto an existing "kill" entry.
      Auto-promoted at 0.95, never queued. The variant is added as a new
      row pointing to the same edge_type.

  Tier 2 · llm_v1  — LLM-validated proposal
      For new lemmas with no morphological match, pull ≤5 sample sentences
      and ask Gemma which edge_type fits (constrained to the existing
      edge_type vocabulary, with "none_apply" allowed). Auto-promote if
      proposed_confidence ≥ AUTO_PROMOTE_THRESHOLD; otherwise queue for
      human review.

  Tier 3 · human   — Governance review queue
      Surfaces in /governance. User approves / rejects / remaps.
      On approve: write to predicate_dictionary + reset pending statements.

Every promotion — auto OR human — writes a predicate_review_queue row, so
we have a complete audit of why each edge_type mapping exists.

Run modes:
  python -m core.workers.predicate_extender                # one pass
  python -m core.workers.predicate_extender --batch 20     # bigger
  python -m core.workers.predicate_extender --continuous   # 5-min loop

This module also exposes start_worker() / stop_worker() for FastAPI startup.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

sys.path.insert(0, "/app")

from core.gdu.address import assign_address
from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.predicate_extender")


# Threshold above which LLM-proposed mappings auto-promote without queueing.
# Open-weight models tend to rubber-stamp 0.85-0.90. Setting threshold at
# 0.93 forces near-certainty before bypassing human review.
AUTO_PROMOTE_THRESHOLD = 0.93

# Lemmas the LLM tier won't even ask about — pure noise that should never
# reach predicate_dictionary regardless of LLM opinion. Filtered at the
# candidate stage before we spend an LLM call. Mirrors the "none_apply"
# guidance in the prompt; the explicit list saves the API roundtrip.
_COPULA_BLOCKLIST = {
    # copulas
    "is", "was", "were", "be", "been", "being", "am", "are",
    # auxiliaries
    "have", "has", "had", "having", "do", "does", "did", "doing",
    "will", "would", "can", "could", "may", "might", "must",
    "shall", "should", "ought",
    # vague light verbs
    "get", "got", "make", "made", "take", "took", "give", "gave",
    "put", "set", "become", "became", "seem", "seems", "seemed",
    # generic existence
    "exist", "exists", "existed", "occur", "occurs", "occurred",
    "happen", "happens", "happened",
    # prepositions / conjunctions the upstream extractor sometimes mis-types
    # as predicates. Real predicates are verbs; these are not.
    "and", "or", "but", "nor", "so",
    "in", "on", "at", "to", "of", "for", "with", "by", "from",
    "into", "onto", "over", "under", "between", "among", "through",
    "during", "before", "after", "since", "until", "while", "as",
    # too-short fragments (single letter, two letter, common abbrevs)
    "a", "an", "the", "i", "it", "we", "he", "she", "they",
    "nec", "etc", "et", "vs", "via", "per",
}

# Confidence stamped on dict entries by tier.
_CONFIDENCE_MORPH = 0.95
_CONFIDENCE_LLM_HIGH = 0.85  # whatever LLM said, capped at this
_CONFIDENCE_HUMAN = 1.00


# Continuous-loop scaffolding
_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS: dict = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "morph_promoted": 0,
    "llm_auto_promoted": 0,
    "queued_for_review": 0,
    "errors": 0,
}
_CHECK_INTERVAL_S = 300  # 5 min between cycles


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_continuous_loop, args=(tenant_id,),
        name="predicate-extender", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    return {"status": "started"}


def stop_worker():
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=10)
    _STATUS["running"] = False
    return {"status": "stopped"}


def _continuous_loop(tenant_id: str):
    while not _STOP_EVENT.is_set():
        try:
            stats = run(min_occurrences=3, batch=20)
            _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
            _STATUS["morph_promoted"] += stats.get("morph_promoted", 0)
            _STATUS["llm_auto_promoted"] += stats.get("llm_auto_promoted", 0)
            _STATUS["queued_for_review"] += stats.get("queued_for_review", 0)
            _STATUS["phase"] = (
                f"idle — last pass: morph={stats.get('morph_promoted',0)}, "
                f"llm={stats.get('llm_auto_promoted',0)}, "
                f"queued={stats.get('queued_for_review',0)}"
            )
        except Exception as e:
            log.warning("cycle failed: %s", e)
            _STATUS["errors"] += 1
            _STATUS["phase"] = f"error: {str(e)[:60]}"
        for _ in range(_CHECK_INTERVAL_S):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)
    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"


# ─── Morphological variants ───────────────────────────────────────────────

def _morph_variants(lemma: str) -> list[str]:
    """Variants of `lemma` that might already be in predicate_dictionary
    (lowercase, simple morph). Order: try the most "canonical" forms first."""
    s = lemma.lower().strip()
    if not s:
        return []
    out: list[str] = []
    # -ed → strip
    if s.endswith("ied") and len(s) > 4:
        out.append(s[:-3] + "y")    # tried → try
    if s.endswith("ed") and len(s) > 4:
        out.append(s[:-2])           # killed → kill
        out.append(s[:-1])           # died → die
    if s.endswith("ing") and len(s) > 5:
        out.append(s[:-3])           # killing → kill
        out.append(s[:-3] + "e")     # making → make
    if s.endswith("s") and not s.endswith("ss") and len(s) > 3:
        out.append(s[:-1])           # kills → kill
    if s.endswith("es") and len(s) > 4:
        out.append(s[:-2])           # goes → go
    # The bare lemma itself
    out.append(s)
    # Dedup preserving order
    seen: set[str] = set()
    return [v for v in out if not (v in seen or seen.add(v))]


def _existing_dict_match(conn, lemma: str) -> Optional[tuple[str, float]]:
    """Return (edge_type, confidence) if any morphological variant of
    `lemma` is already in predicate_dictionary."""
    variants = _morph_variants(lemma)
    if not variants:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT canonical_lemma, edge_type, confidence
              FROM predicate_dictionary
             WHERE canonical_lemma = ANY(%s)
             ORDER BY confidence DESC, observation_count DESC
             LIMIT 1
            """,
            (variants,),
        )
        row = cur.fetchone()
        if row:
            return (row[1], float(row[2] or 0.5))
    return None


# ─── Candidate discovery ──────────────────────────────────────────────────

def _candidates(conn, min_occurrences: int, limit: int
                ) -> list[tuple[str, int]]:
    """Find rejected verb lemmas worth processing (≥N occurrences, not
    already in dict, not already queued, not in copula blocklist)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.predicate_lemma, COUNT(*) AS n
              FROM statements s
             WHERE s.validation_status = 'rejected'
               AND s.validation_reason LIKE 'no_predicate_mapping:%%'
               AND s.predicate_lemma IS NOT NULL
               AND s.predicate_lemma <> ''
               AND NOT (LOWER(s.predicate_lemma) = ANY(%s))
               AND NOT EXISTS (
                   SELECT 1 FROM predicate_dictionary pd
                    WHERE pd.canonical_lemma = s.predicate_lemma
               )
               AND NOT EXISTS (
                   SELECT 1 FROM predicate_review_queue q
                    WHERE q.candidate_lemma = s.predicate_lemma
                      AND q.status IN ('pending', 'approved', 'auto_promoted')
               )
             GROUP BY s.predicate_lemma
            HAVING COUNT(*) >= %s
             ORDER BY n DESC
             LIMIT %s
            """,
            (list(_COPULA_BLOCKLIST), min_occurrences, limit),
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


def _sample_statements(conn, lemma: str, k: int = 5) -> list[dict]:
    """Pull up to k sample statements showing this lemma in context."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.statement_id, s.raw_text,
                   se.canonical_name AS subj_name, se.canonical_type AS subj_type,
                   oe.canonical_name AS obj_name, oe.canonical_type AS obj_type
              FROM statements s
              LEFT JOIN entities se ON se.entity_id = s.speaker_entity_id
              LEFT JOIN entities oe ON oe.entity_id = s.object_entity_id
             WHERE s.predicate_lemma = %s
               AND s.validation_status = 'rejected'
             ORDER BY s.created_at DESC
             LIMIT %s
            """,
            (lemma, k),
        )
        return [
            {
                "statement_id": r[0],
                "raw_text": (r[1] or "")[:300],
                "subject": r[2],
                "subject_type": r[3],
                "object": r[4],
                "object_type": r[5],
            }
            for r in cur.fetchall()
        ]


def _edge_taxonomy(conn) -> list[str]:
    """Return the FULL registered edge-type vocabulary (gdu_type_constraints)
    as the LLM's legal answer set — most-used-in-dictionary first so common
    relations are offered before rare ones. Widened from dictionary-only so
    the LLM can propose (and auto-promote to) any governed edge type, not just
    ones that already have seeds."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT tc.edge_type, COALESCE(pd.n, 0) AS n
              FROM gdu_type_constraints tc
              LEFT JOIN (SELECT edge_type, COUNT(*) AS n
                           FROM predicate_dictionary GROUP BY edge_type) pd
                     ON pd.edge_type = tc.edge_type
             ORDER BY n DESC, tc.edge_type
            """
        )
        return [r[0] for r in cur.fetchall()]


# ─── LLM validation ───────────────────────────────────────────────────────

_LLM_PROMPT = """You map a verb to a knowledge-graph edge_type.

A predicate must be a TRANSITIVE ACTION VERB that establishes a specific
relationship FROM subject TO object. If the token below is NOT such a verb
— if it is a copula, preposition, conjunction, fragment, or vague light
verb — return "none_apply" with confidence ≤ 0.3.

TOKEN TO MAP: "{lemma}"

SAMPLE SENTENCES showing this token in context:
{samples}

AVAILABLE EDGE TYPES — pick ONE only if the token is a real action verb
whose meaning directly matches one of these relations:
{edge_types}

RETURN "none_apply" with low confidence (0.1-0.3) IF:
  • token is a copula (is/was/be/been/are/were/am)
  • token is an auxiliary (have/has/had/do/does/did/will/can/may/should)
  • token is a preposition (of/in/on/at/to/for/with/by/from/about)
  • token is a conjunction (and/or/but/so/nor)
  • token is a determiner or fragment (a/an/the/this/that/it/i)
  • token is a vague light verb (get/make/take/give/put/become/seem)
  • the verb is too generic to name a SPECIFIC relation
  • the samples don't actually show subject→verb→object structure

ONLY assign edge_type at confidence ≥ 0.85 when the verb plainly carries
the edge_type's meaning. Default to LOWER confidence and "none_apply"
when in doubt. A bad mapping pollutes the graph permanently.

Respond ONLY with JSON, no commentary, no markdown fences:
{{"edge_type": "..." or "none_apply",
  "confidence": 0.0-1.0,
  "reasoning": "one sentence",
  "alt_edge_type": "..." or null}}

Confidence rubric — calibrate honestly. Confidence ≥ 0.93 auto-promotes
without review:
  0.95-1.0  verb plainly = this edge_type ("killed" → "killed_by")
  0.85-0.94 strong fit but verb is somewhat broad
  0.70-0.84 partial fit — prefer "none_apply" unless very confident
  ≤0.50     no fit / token is not a real predicate — use "none_apply" """


def _ask_llm(lemma: str, samples: list[dict], edge_types: list[str],
             llm: Any) -> Optional[dict]:
    """Call LLM with the verb + samples. Returns parsed result or None."""
    if not llm:
        return None

    sample_lines = []
    for s in samples:
        sample_lines.append(
            f"- subj=({s.get('subject') or '?'} : {s.get('subject_type') or '?'}) "
            f"obj=({s.get('object') or '?'} : {s.get('object_type') or '?'}) "
            f"text=\"{(s.get('raw_text') or '')[:200]}\""
        )
    samples_block = "\n".join(sample_lines) or "(no samples)"

    edge_block = ", ".join(edge_types) if edge_types else "(none)"

    prompt = _LLM_PROMPT.format(
        lemma=lemma,
        samples=samples_block,
        edge_types=edge_block,
    )
    try:
        from core.ai.provider import LLMMessage
        messages = [
            LLMMessage(role="system",
                       content="You return only valid JSON. No commentary, no markdown fences."),
            LLMMessage(role="user", content=prompt),
        ]
        response = llm.complete(messages, max_tokens=300, temperature=0.0)
    except Exception as e:
        log.debug("LLM call failed for %s: %s", lemma, e)
        return None
    if response is None or getattr(response, "error", None):
        return None
    raw = (response.content or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    if raw.startswith("json"):
        raw = raw[4:].lstrip()
    try:
        parsed = json.loads(raw)
    except Exception:
        try:
            s, e = raw.index("{"), raw.rindex("}") + 1
            parsed = json.loads(raw[s:e])
        except Exception:
            return None
    if not isinstance(parsed, dict):
        return None
    edge_type = parsed.get("edge_type")
    confidence = parsed.get("confidence")
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "edge_type": edge_type if edge_type and edge_type != "none_apply" else None,
        "confidence": max(0.0, min(1.0, confidence)),
        "reasoning": parsed.get("reasoning") or "",
        "alt_edge_type": parsed.get("alt_edge_type"),
    }


# ─── Promotion ────────────────────────────────────────────────────────────

def _add_dict_entry(conn, lemma: str, edge_type: str, confidence: float,
                    source: str, observation_count: int) -> bool:
    """Insert into predicate_dictionary. Returns True if new, False if dup."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM predicate_dictionary WHERE canonical_lemma=%s AND edge_type=%s",
            (lemma, edge_type),
        )
        if cur.fetchone():
            return False
        addr = assign_address(conn, "form", "verb")
        cur.execute(
            """
            INSERT INTO predicate_dictionary
                (canonical_lemma, edge_type, base60_address,
                 confidence, observation_count, source)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (lemma, edge_type, addr, confidence, observation_count, source),
        )
    return True


def _repend_statements(conn, lemma: str) -> int:
    """Reset rejected statements for this lemma so the promoter retries them."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE statements
               SET validation_status='pending', validation_reason=NULL
             WHERE predicate_lemma=%s
               AND validation_status='rejected'
               AND validation_reason LIKE 'no_predicate_mapping:%%'
            """,
            (lemma,),
        )
        return cur.rowcount or 0


def _queue_row(conn, lemma: str, occurrences: int, samples: list[dict],
               proposed_edge_type: Optional[str], confidence: float,
               proposed_by: str, evidence: dict, status: str,
               final_edge_type: Optional[str] = None) -> Optional[int]:
    """Insert/upsert the audit row in predicate_review_queue."""
    sample_ids = [s["statement_id"] for s in samples if s.get("statement_id")]
    applied_at = "NOW()" if status == "auto_promoted" else "NULL"
    with conn.cursor() as cur:
        try:
            cur.execute(
                f"""
                INSERT INTO predicate_review_queue
                    (candidate_lemma, occurrences, sample_statement_ids,
                     proposed_edge_type, proposed_confidence, proposed_by,
                     evidence, status, reviewed_by, reviewed_at, applied_at,
                     final_edge_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s,
                        %s, {('NOW()' if status == 'auto_promoted' else 'NULL')},
                        {applied_at}, %s)
                ON CONFLICT (candidate_lemma, COALESCE(proposed_edge_type, ''))
                DO UPDATE SET
                    occurrences = EXCLUDED.occurrences,
                    sample_statement_ids = EXCLUDED.sample_statement_ids,
                    proposed_confidence = EXCLUDED.proposed_confidence,
                    evidence = EXCLUDED.evidence,
                    updated_at = NOW()
                RETURNING queue_id
                """,
                (lemma, occurrences, sample_ids,
                 proposed_edge_type, confidence, proposed_by,
                 json.dumps(evidence), status,
                 "auto" if status == "auto_promoted" else None,
                 final_edge_type),
            )
            row = cur.fetchone()
            return row[0] if row else None
        except Exception as e:
            log.warning("queue upsert failed for %s: %s", lemma, e)
            return None


# ─── Main run ─────────────────────────────────────────────────────────────

def run(min_occurrences: int = 3, batch: int = 20,
        llm: Any = None) -> dict:
    """One promotion pass. If llm=None, LLM tier is skipped (only morph runs)."""
    if llm is None:
        try:
            from core.ai.resolver import get_effective_provider
            llm = get_effective_provider()
        except Exception:
            llm = None

    stats = {
        "candidates_found": 0,
        "morph_promoted": 0,
        "llm_auto_promoted": 0,
        "queued_for_review": 0,
        "rejected_other": 0,
    }

    with get_connection() as conn:
        candidates = _candidates(conn, min_occurrences, batch)
        stats["candidates_found"] = len(candidates)
        if not candidates:
            return stats
        edge_types = _edge_taxonomy(conn)

    log.info("processing %d candidates", len(candidates))

    for lemma, occurrences in candidates:
        try:
            with get_connection() as conn:
                # Tier 1 — morphological match against existing dict.
                morph_match = _existing_dict_match(conn, lemma)
                if morph_match:
                    edge_type, _ = morph_match
                    is_new = _add_dict_entry(
                        conn, lemma, edge_type, _CONFIDENCE_MORPH,
                        "morph_v1", occurrences,
                    )
                    if is_new:
                        samples = _sample_statements(conn, lemma, k=3)
                        _queue_row(
                            conn, lemma, occurrences, samples,
                            edge_type, _CONFIDENCE_MORPH, "morph_v1",
                            {"reason": "morphological variant of existing entry"},
                            "auto_promoted", final_edge_type=edge_type,
                        )
                        repended = _repend_statements(conn, lemma)
                        conn.commit()
                        stats["morph_promoted"] += 1
                        log.info("morph: %s → %s (re-pended %d stmts)",
                                 lemma, edge_type, repended)
                        continue
                    # Already in dict for some reason — nothing to do
                    conn.commit()
                    continue

                # Tier 2 — LLM validation.
                samples = _sample_statements(conn, lemma, k=5)
                llm_result = _ask_llm(lemma, samples, edge_types, llm) if llm else None
                if not llm_result:
                    stats["rejected_other"] += 1
                    continue

                proposed = llm_result["edge_type"]
                confidence = llm_result["confidence"]

                # Type-pair distribution for the queue evidence
                type_pair_dist = Counter(
                    (s.get("subject_type") or "?", s.get("object_type") or "?")
                    for s in samples
                ).most_common(5)
                evidence = {
                    "sample_sentences": [s["raw_text"] for s in samples],
                    "type_pair_dist": [{"pair": list(k), "count": v}
                                       for k, v in type_pair_dist],
                    "llm_reasoning": llm_result.get("reasoning", ""),
                    "llm_alt_proposals": llm_result.get("alt_edge_type"),
                }

                if proposed and confidence >= AUTO_PROMOTE_THRESHOLD \
                        and proposed in set(edge_types):
                    # High-confidence auto-promote
                    capped_conf = min(confidence, _CONFIDENCE_LLM_HIGH)
                    is_new = _add_dict_entry(
                        conn, lemma, proposed, capped_conf,
                        "llm_v1", occurrences,
                    )
                    _queue_row(
                        conn, lemma, occurrences, samples,
                        proposed, confidence, "llm_v1",
                        evidence, "auto_promoted",
                        final_edge_type=proposed if is_new else None,
                    )
                    repended = _repend_statements(conn, lemma)
                    conn.commit()
                    stats["llm_auto_promoted"] += 1
                    log.info("llm-auto: %s → %s @ %.2f (re-pended %d stmts)",
                             lemma, proposed, confidence, repended)
                else:
                    # Queue for human review
                    _queue_row(
                        conn, lemma, occurrences, samples,
                        proposed, confidence, "llm_v1",
                        evidence, "pending",
                    )
                    conn.commit()
                    stats["queued_for_review"] += 1
                    log.info("queued: %s → %s @ %.2f (pending review)",
                             lemma, proposed, confidence)
        except Exception as e:
            log.warning("candidate %s failed: %s", lemma, e)
            stats["rejected_other"] += 1

    return stats


if __name__ == "__main__":
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )
    p = argparse.ArgumentParser()
    p.add_argument("--min-occurrences", type=int, default=3)
    p.add_argument("--batch", type=int, default=20)
    p.add_argument("--continuous", action="store_true")
    args = p.parse_args()
    if args.continuous:
        log.info("continuous mode — running every %ds", _CHECK_INTERVAL_S)
        while True:
            try:
                stats = run(args.min_occurrences, args.batch)
                print(json.dumps(stats, indent=2))
            except Exception as e:
                log.error("cycle failed: %s", e)
            time.sleep(_CHECK_INTERVAL_S)
    else:
        print(json.dumps(run(args.min_occurrences, args.batch), indent=2))
