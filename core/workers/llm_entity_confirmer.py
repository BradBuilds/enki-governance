"""
LLM Entity Confirmer — uses Gemma to confirm/reject ambiguous entity merges.

Scans for:
1. Entities with similar names that aren't merged (potential duplicates):
   title-prefixed forms vs full name, short alias vs canonical, etc.
2. Conflicts in governance_conflicts where auto-resolution would help.
3. Type-ambiguous entities (a token that could be a country or a person —
   the LLM gets the context and decides).

Sends to Gemma for adjudication, applies the result via the resolver
(merge two entities) or logs to governance_conflicts (if uncertain).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("enki.workers.llm_entity_confirmer")

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "merges_confirmed": 0,
    "merges_rejected": 0,
    "uncertain": 0,
    "errors": 0,
    "batch_num": 0,
}

CHECK_INTERVAL = 600  # 10 minutes
BATCH_SIZE = 5  # entity pairs per LLM call


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_loop, args=(tenant_id,),
        name="llm-entity-confirmer", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    return {"status": "started"}


def stop_worker():
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=5)
    _STATUS["running"] = False
    return {"status": "stopped"}


def _loop(tenant_id: str):
    _STATUS["running"] = True
    while not _STOP_EVENT.is_set():
        try:
            run_once(tenant_id)
        except Exception as e:
            log.warning("Confirmer loop error: %s", e)
            _STATUS["errors"] += 1
        for _ in range(CHECK_INTERVAL):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)
    _STATUS["running"] = False


def find_merge_candidates(tenant_id: str = "default", limit: int = 20) -> list[dict]:
    """Find pairs of entities that look like potential duplicates.

    Heuristics:
    - Same canonical_type
    - Token overlap >= 50%
    - Both have signal_count > 0 (real usage)
    - Neither is verified
    """
    from storage.postgres.db import get_connection

    pairs = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Find entities whose names share at least one token
                cur.execute("""
                    WITH active AS (
                        SELECT entity_id, canonical_name, canonical_type, aliases, signal_count
                        FROM entities
                        WHERE tenant_id = %s AND signal_count > 0 AND verified_at IS NULL
                        ORDER BY signal_count DESC LIMIT 200
                    )
                    SELECT a1.entity_id, a1.canonical_name, a1.canonical_type, a1.signal_count,
                           a2.entity_id, a2.canonical_name, a2.canonical_type, a2.signal_count
                    FROM active a1
                    CROSS JOIN active a2
                    WHERE a1.entity_id < a2.entity_id
                      AND a1.canonical_type = a2.canonical_type
                      AND (
                          -- Substring match
                          LOWER(a1.canonical_name) LIKE '%' || LOWER(a2.canonical_name) || '%'
                          OR LOWER(a2.canonical_name) LIKE '%' || LOWER(a1.canonical_name) || '%'
                          -- Or last word matches (catches title-prefixed surname vs full name)
                          OR (
                              array_length(string_to_array(a1.canonical_name, ' '), 1) > 1
                              AND array_length(string_to_array(a2.canonical_name, ' '), 1) > 1
                              AND (string_to_array(LOWER(a1.canonical_name), ' '))
                                  [array_length(string_to_array(a1.canonical_name, ' '), 1)]
                                  =
                                  (string_to_array(LOWER(a2.canonical_name), ' '))
                                  [array_length(string_to_array(a2.canonical_name, ' '), 1)]
                          )
                      )
                    LIMIT %s
                """, (tenant_id, limit))

                for r in cur.fetchall():
                    pairs.append({
                        "a_id": r[0], "a_name": r[1], "a_type": r[2], "a_count": r[3],
                        "b_id": r[4], "b_name": r[5], "b_type": r[6], "b_count": r[7],
                    })
    except Exception as e:
        log.debug("Find merge candidates failed: %s", e)
    return pairs


def confirm_with_llm(pairs: list[dict]) -> list[dict]:
    """Ask Gemma whether each pair should be merged.

    Returns list of {pair, decision, confidence, reasoning}.
    decision: 'merge', 'keep_separate', 'uncertain'
    """
    if not pairs:
        return []

    try:
        from core.ai.resolver import get_effective_provider
        from core.ai.provider import LLMMessage
    except Exception:
        return []

    provider = get_effective_provider()
    if not provider:
        return []

    # Build prompt
    pair_lines = []
    for i, p in enumerate(pairs):
        pair_lines.append(
            f'{i+1}. "{p["a_name"]}" ({p["a_type"]}, {p["a_count"]} mentions) '
            f'vs "{p["b_name"]}" ({p["b_type"]}, {p["b_count"]} mentions)'
        )

    prompt = f"""For each pair below, decide if they refer to the SAME real-world entity.
Guidance:
- A title in front of a person's last name (e.g., a head-of-state title in
  front of a surname) usually means SAME — same person, different role label.
- A short geographic name and a longer name that uses the geographic name as
  a token (country vs unrelated person sharing a token) usually means DIFFERENT.
- A company short-name and the same company with a legal suffix (Inc/LLC/Ltd)
  is SAME.
- A country and a country-named program (e.g., "<country> nuclear program") is
  DIFFERENT — the country is an entity, the program is a topic.
- When uncertain, choose 'uncertain' rather than guessing — false merges are
  worse than duplicates.

Pairs:
{chr(10).join(pair_lines)}

Return ONLY a JSON array. For each pair return:
{{"pair": N, "decision": "merge" | "keep_separate" | "uncertain", "confidence": 0.0-1.0, "reason": "brief"}}"""

    try:
        # Throttle
        try:
            from core.workers.reconciliation import acquire_llm_slot, release_llm_slot
            if not acquire_llm_slot(timeout=120):
                return []
        except ImportError:
            pass

        response = provider.complete(
            [LLMMessage(role="user", content=prompt)],
            max_tokens=800, temperature=0.0,
        )

        try:
            release_llm_slot()
        except Exception:
            pass

        if not response.content or response.error:
            return []

        match = re.search(r'\[.*\]', response.content, re.DOTALL)
        if not match:
            return []

        decisions = json.loads(match.group())
        results = []
        for d in decisions:
            if not isinstance(d, dict):
                continue
            pair_idx = d.get("pair", 0) - 1
            if pair_idx < 0 or pair_idx >= len(pairs):
                continue
            results.append({
                "pair": pairs[pair_idx],
                "decision": d.get("decision", "uncertain"),
                "confidence": float(d.get("confidence", 0.5)),
                "reason": d.get("reason", ""),
            })
        return results
    except Exception as e:
        log.warning("LLM confirmation failed: %s", e)
        return []


def apply_merges(decisions: list[dict], tenant_id: str = "default") -> dict:
    """Apply merge decisions: combine entities or log conflicts."""
    from storage.postgres.db import get_connection
    from core.gdu.entity_governance import emit_entity_event, log_conflict

    merged = 0
    rejected = 0
    uncertain = 0

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                for d in decisions:
                    pair = d["pair"]
                    decision = d["decision"]

                    if decision == "merge" and d["confidence"] >= 0.85:
                        # Merge: keep entity with higher signal_count
                        keep_id, drop_id = (pair["a_id"], pair["b_id"]) \
                            if pair["a_count"] >= pair["b_count"] \
                            else (pair["b_id"], pair["a_id"])
                        keep_name = pair["a_name"] if keep_id == pair["a_id"] else pair["b_name"]
                        drop_name = pair["b_name"] if keep_id == pair["a_id"] else pair["a_name"]

                        try:
                            # Add dropped name as alias to kept entity
                            cur.execute("""
                                UPDATE entities SET
                                    aliases = array_append(aliases, %s),
                                    signal_count = signal_count + (SELECT signal_count FROM entities WHERE entity_id = %s)
                                WHERE entity_id = %s
                            """, (drop_name.lower(), drop_id, keep_id))

                            # Redirect: update watch_topic_entities and signal_extractions
                            cur.execute("""
                                UPDATE watch_topic_entities SET canonical_entity_id = %s WHERE canonical_entity_id = %s
                            """, (keep_id, drop_id))
                            try:
                                cur.execute("""
                                    UPDATE signal_extractions SET entity_id = %s WHERE entity_id = %s
                                """, (keep_id, drop_id))
                            except Exception:
                                pass

                            # Insert into entity_redirects
                            try:
                                cur.execute("""
                                    INSERT INTO entity_redirects (old_entity_id, new_entity_id, tenant_id, reason, created_at)
                                    VALUES (%s, %s, %s, %s, NOW())
                                """, (drop_id, keep_id, tenant_id, f"LLM confirmed merge: {d['reason']}"))
                            except Exception:
                                pass

                            # Delete merged entity
                            cur.execute("DELETE FROM entities WHERE entity_id = %s", (drop_id,))

                            # Audit event
                            emit_entity_event(
                                tenant_id=tenant_id, mutation_type="merge",
                                entity_ids=[keep_id, drop_id],
                                payload={
                                    "kept": keep_name, "merged_in": drop_name,
                                    "llm_reason": d["reason"],
                                    "llm_confidence": d["confidence"],
                                },
                                source_pipeline="llm_entity_confirmer",
                            )
                            merged += 1
                        except Exception as e:
                            log.debug("Merge failed: %s", e)

                    elif decision == "keep_separate" and d["confidence"] >= 0.85:
                        # Confidence is high they're different — no action needed
                        rejected += 1

                    else:
                        # Uncertain — log as conflict for human review
                        log_conflict(
                            tenant_id=tenant_id,
                            conflict_type="merge_proposal",
                            entity_id=pair["a_id"],
                            related_entity_ids=[pair["b_id"]],
                            severity="routine",
                            competing_values=[
                                {"value": pair["a_name"], "source": f"entity {pair['a_id']}", "confidence": pair["a_count"] / 100},
                                {"value": pair["b_name"], "source": f"entity {pair['b_id']}", "confidence": pair["b_count"] / 100},
                            ],
                            detected_by="llm_entity_confirmer",
                            metadata={"llm_decision": decision, "llm_reason": d["reason"], "llm_confidence": d["confidence"]},
                        )
                        uncertain += 1

            conn.commit()
    except Exception as e:
        log.warning("Apply merges failed: %s", e)

    return {"merged": merged, "rejected": rejected, "uncertain": uncertain}


def run_once(tenant_id: str = "default") -> dict:
    """Run one cycle: find candidates, ask LLM, apply decisions."""
    _STATUS["batch_num"] += 1
    _STATUS["phase"] = "finding candidates"

    pairs = find_merge_candidates(tenant_id, limit=BATCH_SIZE * 4)
    if not pairs:
        _STATUS["phase"] = "idle — no candidates"
        return {"candidates": 0}

    _STATUS["phase"] = f"asking LLM about {len(pairs)} pairs"
    decisions = confirm_with_llm(pairs[:BATCH_SIZE])
    if not decisions:
        _STATUS["phase"] = "LLM unavailable"
        return {"candidates": len(pairs), "decisions": 0}

    _STATUS["phase"] = f"applying {len(decisions)} decisions"
    result = apply_merges(decisions, tenant_id)
    _STATUS["merges_confirmed"] += result["merged"]
    _STATUS["merges_rejected"] += result["rejected"]
    _STATUS["uncertain"] += result["uncertain"]
    _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
    _STATUS["phase"] = f"complete: merged={result['merged']}, rejected={result['rejected']}, uncertain={result['uncertain']}"
    return {"candidates": len(pairs), "decisions": len(decisions), **result}
