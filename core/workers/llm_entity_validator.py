"""
LLM Entity Validator — uses Gemma to verify whether an entity is real.

Different from the Entity Confirmer (which merges duplicates).
This one asks: "Is 'X' actually a person/organization/location, or is it
a topic phrase, idiom, sentence fragment, or generic term?"

Runs continuously on:
1. Newly created entities (signal_count just crossed 5+)
2. Entities that haven't been validated yet (no entry in entity_validations)

Verdict 'reject' → entity is deleted, name added to a learned blocklist
Verdict 'rename' → suggests canonical name (e.g., 'Bulletin of Atomic Scientists' for 'Atomic Scientists')
Verdict 'valid' → marked validated, won't be re-checked unless type changes
Verdict 'uncertain' → logged to governance_conflicts for human review
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("enki.workers.llm_entity_validator")

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "validated_count": 0,
    "rejected_count": 0,
    "renamed_count": 0,
    "uncertain_count": 0,
    "errors": 0,
    "batch_num": 0,
}

CHECK_INTERVAL = 300  # 5 minutes
BATCH_SIZE = 8


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_loop, args=(tenant_id,),
        name="llm-entity-validator", daemon=True,
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
            log.warning("Validator loop error: %s", e)
            _STATUS["errors"] += 1
        for _ in range(CHECK_INTERVAL):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)
    _STATUS["running"] = False


def find_validation_candidates(tenant_id: str = "default", limit: int = 20) -> list[dict]:
    """Find entities that need LLM validation.

    Priority:
    1. Newly emerged entities (signal_count >= 5, never validated)
    2. Entities flagged by quality filter as uncertain
    """
    from storage.postgres.db import get_connection

    candidates = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Use last_researched_at as a proxy for "recently validated"
                # Entities with signal_count > 5 and no last_researched_at = need validation
                cur.execute("""
                    SELECT entity_id, canonical_name, canonical_type, signal_count, aliases
                    FROM entities
                    WHERE tenant_id = %s
                      AND verified_at IS NULL
                      AND signal_count >= 5
                      AND (last_researched_at IS NULL OR last_researched_at < NOW() - INTERVAL '30 days')
                    ORDER BY signal_count DESC
                    LIMIT %s
                """, (tenant_id, limit))
                for r in cur.fetchall():
                    # Get sample headlines for context
                    headlines = []
                    try:
                        cur.execute("""
                            SELECT s.payload->>'title' FROM signals s
                            JOIN signal_extractions se ON se.signal_id = s.signal_id
                            WHERE se.entity_id = %s
                            ORDER BY s.timestamp DESC LIMIT 3
                        """, (r[0],))
                        for h in cur.fetchall():
                            if h[0]:
                                headlines.append(h[0][:150])
                    except Exception:
                        pass

                    candidates.append({
                        "entity_id": r[0], "name": r[1], "type": r[2],
                        "signal_count": r[3], "aliases": r[4] or [],
                        "headlines": headlines,
                    })
    except Exception as e:
        log.debug("Find candidates failed: %s", e)
    return candidates


def validate_with_llm(candidates: list[dict]) -> list[dict]:
    """Ask Gemma to validate each candidate entity.

    Returns: [{entity_id, decision, suggested_type, suggested_name, reason}]
    decision: 'valid' | 'reject' | 'rename' | 'uncertain'
    """
    if not candidates:
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
    cand_lines = []
    for i, c in enumerate(candidates):
        ctx = " | ".join(c["headlines"][:2]) if c["headlines"] else "(no context)"
        cand_lines.append(
            f'{i+1}. "{c["name"]}" (currently labeled: {c["type"]}, {c["signal_count"]} mentions)\n   Context: {ctx}'
        )

    prompt = f"""Validate each candidate entity. Decide if it's a real-world entity worth keeping in a knowledge graph.

Real entities are: specific people (named individuals), organizations (companies, institutions, governments), locations (countries, cities, regions, named facilities), events (named conflicts, summits, programs).

REJECT these as NOT real entities:
- Topic phrases ("Nuclear Weapons", "Missile Capabilities", "Trade Wars")
- Idioms or sentence fragments ("One Step Closer", "More Than", "Is Not")
- Generic noun phrases ("Senior Fellow", "Vice President" without name)
- Article headline fragments ("Iran Update", "Russia Watch")
- Common nouns that got capitalized

For each, return:
- decision: "valid" (keep), "reject" (delete from graph), "rename" (suggest better name), "uncertain"
- type: correct type if different from current
- suggested_name: only if decision=rename
- reason: brief

Candidates:
{chr(10).join(cand_lines)}

Return ONLY a JSON array:
[{{"num": 1, "decision": "valid"|"reject"|"rename"|"uncertain", "type": "person|organization|location|event", "suggested_name": "", "reason": "brief"}}]"""

    try:
        try:
            from core.workers.reconciliation import acquire_llm_slot, release_llm_slot
            if not acquire_llm_slot(timeout=120):
                return []
        except ImportError:
            pass

        response = provider.complete(
            [LLMMessage(role="user", content=prompt)],
            max_tokens=1200, temperature=0.0,
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
            num = d.get("num", 0) - 1
            if num < 0 or num >= len(candidates):
                continue
            results.append({
                "entity_id": candidates[num]["entity_id"],
                "name": candidates[num]["name"],
                "decision": d.get("decision", "uncertain"),
                "suggested_type": d.get("type", candidates[num]["type"]),
                "suggested_name": d.get("suggested_name", ""),
                "reason": d.get("reason", ""),
            })
        return results
    except Exception as e:
        log.warning("Validator LLM call failed: %s", e)
        return []


def apply_validations(decisions: list[dict], tenant_id: str = "default") -> dict:
    """Apply LLM validation decisions."""
    from storage.postgres.db import get_connection
    from core.gdu.entity_governance import emit_entity_event, log_conflict

    valid = 0
    rejected = 0
    renamed = 0
    uncertain = 0

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                for d in decisions:
                    eid = d["entity_id"]
                    decision = d["decision"]

                    if decision == "valid":
                        # Mark as validated (last_researched_at acts as validation timestamp)
                        cur.execute("""
                            UPDATE entities SET last_researched_at = NOW()
                            WHERE entity_id = %s
                        """, (eid,))
                        # Update type if Gemma corrected it — but only if the
                        # proposed type is in entityvocab (avoid overwriting
                        # with non-admittable LLM output and crashing 116).
                        if d.get("suggested_type") and d["suggested_type"] != "unknown":
                            try:
                                from core.ldu.subtype_validator import safe_canonical_type
                                _safe = safe_canonical_type(d["suggested_type"])
                            except Exception:
                                _safe = None
                            if _safe:
                                cur.execute("""
                                    UPDATE entities SET canonical_type = %s
                                    WHERE entity_id = %s AND canonical_type != %s
                                """, (_safe, eid, _safe))
                        valid += 1

                    elif decision == "reject":
                        # Delete entity and cascade
                        try:
                            cur.execute("DELETE FROM watch_topic_entities WHERE canonical_entity_id = %s", (eid,))
                            try:
                                cur.execute("DELETE FROM signal_extractions WHERE entity_id = %s", (eid,))
                            except Exception:
                                pass
                            cur.execute("DELETE FROM entity_relationships WHERE source_entity_id = %s OR target_entity_id = %s", (eid, eid))
                            cur.execute("DELETE FROM facet_counts WHERE facet_value = %s", (d["name"],))
                            cur.execute("DELETE FROM feed_snapshot_edges WHERE source_name = %s OR target_name = %s", (d["name"], d["name"]))
                            cur.execute("DELETE FROM entities WHERE entity_id = %s", (eid,))

                            emit_entity_event(
                                tenant_id=tenant_id, mutation_type="rejected",
                                entity_ids=[eid],
                                payload={"name": d["name"], "llm_reason": d["reason"]},
                                source_pipeline="llm_validator",
                            )
                            rejected += 1
                        except Exception as e:
                            log.debug("Reject failed for %s: %s", d["name"], e)

                    elif decision == "rename" and d.get("suggested_name"):
                        new_name = d["suggested_name"].strip()
                        old_name = d["name"]
                        try:
                            cur.execute("""
                                UPDATE entities SET
                                    canonical_name = %s,
                                    normalized_name = LOWER(%s),
                                    aliases = array_append(COALESCE(aliases, '{}'::text[]), %s),
                                    last_researched_at = NOW()
                                WHERE entity_id = %s
                            """, (new_name, new_name, old_name.lower(), eid))
                            emit_entity_event(
                                tenant_id=tenant_id, mutation_type="renamed",
                                entity_ids=[eid],
                                payload={"old": old_name, "new": new_name, "llm_reason": d["reason"]},
                                source_pipeline="llm_validator",
                            )
                            renamed += 1
                        except Exception as e:
                            log.debug("Rename failed: %s", e)

                    else:
                        # Uncertain → log for human review
                        log_conflict(
                            tenant_id=tenant_id,
                            conflict_type="entity_validity",
                            entity_id=eid,
                            severity="routine",
                            competing_values=[
                                {"value": d["name"], "source": "extraction", "current_type": d.get("suggested_type", "")},
                            ],
                            detected_by="llm_validator",
                            metadata={"llm_decision": "uncertain", "llm_reason": d["reason"]},
                        )
                        uncertain += 1

            conn.commit()
    except Exception as e:
        log.warning("Apply validations failed: %s", e)

    return {"valid": valid, "rejected": rejected, "renamed": renamed, "uncertain": uncertain}


def run_once(tenant_id: str = "default") -> dict:
    """Run one validation cycle."""
    _STATUS["batch_num"] += 1
    _STATUS["phase"] = "finding candidates"

    candidates = find_validation_candidates(tenant_id, limit=BATCH_SIZE * 2)
    if not candidates:
        _STATUS["phase"] = "idle — nothing to validate"
        return {"candidates": 0}

    _STATUS["phase"] = f"validating {len(candidates)} candidates"
    decisions = validate_with_llm(candidates[:BATCH_SIZE])
    if not decisions:
        _STATUS["phase"] = "LLM unavailable"
        return {"candidates": len(candidates), "decisions": 0}

    _STATUS["phase"] = f"applying {len(decisions)} decisions"
    result = apply_validations(decisions, tenant_id)
    _STATUS["validated_count"] += result["valid"]
    _STATUS["rejected_count"] += result["rejected"]
    _STATUS["renamed_count"] += result["renamed"]
    _STATUS["uncertain_count"] += result["uncertain"]
    _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
    _STATUS["phase"] = f"complete: valid={result['valid']}, rejected={result['rejected']}, renamed={result['renamed']}, uncertain={result['uncertain']}"
    return {"candidates": len(candidates), "decisions": len(decisions), **result}
