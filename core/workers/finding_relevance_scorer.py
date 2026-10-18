"""
Per-finding LLM relevance scorer.

Phase 7d of the internal research validation plan.

Polls unscored investigation_findings every CHECK_INTERVAL seconds.
For each, builds a prompt comparing the finding to its parent entity
(name, type, signal_count) and asks Gemma whether they're the same
entity. Score is 0.0-1.0; reasoning is short free-text.

Worked example — David Harlow (Acme Widgets CEO, organization=Acme
Widgets Corporation):
  Finding "Brian David Harlow, Sr. — bankruptcy"
    → score 0.0, reason="Different first name (Brian David vs David),
       bankruptcy filing not consistent with Acme Widgets CEO context"
  Finding "VARGAS v. ACME WIDGETS CORPORATION"
    → score 1.0, reason="Acme Widgets securities case, fits Acme Widgets CEO"

The dossier endpoint then filters by relevance >= 0.5 by default;
the UI has a toggle to show all findings including low-relevance.

Same worker pattern as the others: status dict + start/stop +
supervisor-friendly daemon thread, slots into manager Tier 2 (LLM-
medium — pauses when system load high or Ollama unhealthy).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.finding_relevance_scorer")

CHECK_INTERVAL = 30          # poll every 30s
BATCH_SIZE = 10              # 10 findings per tick (was 5; was capacity-bound)
IDLE_BACKOFF = 300           # back off when no unscored findings
MAX_PROMPT_DATA_CHARS = 600  # truncate finding.data to keep prompt small

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()

_STATUS = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "tick_count": 0,
    "scored_total": 0,
    "scored_strong": 0,    # >= 0.85
    "scored_moderate": 0,  # 0.5 - 0.85
    "scored_weak": 0,      # 0.2 - 0.5
    "scored_reject": 0,    # < 0.2
    "errors": 0,
    "last_error": None,
}


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default") -> dict:
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_worker_loop,
        args=(tenant_id,),
        name="finding-relevance-scorer",
        daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    log.info("Finding relevance scorer started")
    return {"status": "started"}


def stop_worker() -> dict:
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=5)
    _STATUS["running"] = False
    return {"status": "stopped"}


def _get_provider():
    """Lazy provider — same path as feed_enrichment.py + others.
    Was previously importing from core.ai.provider:get_provider which
    doesn't exist; the actual public API is core.ai.resolver:
    get_effective_provider. Silent ImportError → returned None →
    worker silently scored 0 each tick. Caught by deploy verification
    showing 19,097 unscored findings + 0 scored."""
    try:
        from core.ai.resolver import get_effective_provider
        return get_effective_provider()
    except Exception as e:
        log.warning("LLM provider unavailable: %s", e)
        return None


def _build_prompt(parent_name: str, parent_type: str, parent_signal_count: int,
                   finding_source: str, finding_title: str, finding_data: dict) -> str:
    """Build the relevance-scoring prompt. Kept tight — Gemma 4 e2b is fast
    on short prompts and the score is a single decision, not a long answer.
    """
    # Summarise data to top 8 fields, truncated, to keep prompt under
    # ~1000 tokens including instruction overhead.
    data_lines = []
    if isinstance(finding_data, dict):
        for k, v in list(finding_data.items())[:8]:
            if v in (None, "", [], {}):
                continue
            sv = str(v)[:120]
            data_lines.append(f"  {k}: {sv}")
    data_block = "\n".join(data_lines) or "  (no structured data)"

    return f"""You are an entity-disambiguation assistant. Decide whether a research finding
is actually about the entity it's been attached to.

Entity:
  name: {parent_name}
  type: {parent_type}
  news mentions: {parent_signal_count}

Finding from {finding_source}:
  title: {finding_title[:200]}
  data:
{data_block[:MAX_PROMPT_DATA_CHARS]}

Score 0.0-1.0:
  1.0  = clearly the same entity (name + identifiers + context all match)
  0.7  = plausible — name matches, context fits the entity type
  0.3  = weak — substring match only, context unclear
  0.0  = clearly wrong (different person/org with similar name, or
         finding is about an unrelated subject)

Respond with ONLY a JSON object (no preamble):
{{"score": 0.X, "reason": "<one short sentence>"}}"""


def _parse_score(text: str) -> Optional[tuple[float, str]]:
    """Extract {score, reason} from the LLM response. Tolerant of
    markdown fences, leading/trailing whitespace, etc."""
    if not text:
        return None
    # Strip markdown fences
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    # Find the first {...} JSON object
    m = re.search(r"\{[^{}]*\}", s)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return None
    score = obj.get("score")
    reason = obj.get("reason") or ""
    try:
        score = float(score)
    except Exception:
        return None
    score = max(0.0, min(1.0, score))
    return (score, str(reason)[:500])


def _worker_loop(tenant_id: str) -> None:
    backoff = CHECK_INTERVAL
    while not _STOP_EVENT.is_set():
        try:
            scored = _score_batch(tenant_id, BATCH_SIZE)
            _STATUS["tick_count"] += 1
            _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
            _STATUS["last_error"] = None
            if scored == 0:
                backoff = min(backoff * 2, IDLE_BACKOFF)
                _STATUS["phase"] = f"idle (backoff {backoff}s)"
            else:
                backoff = CHECK_INTERVAL
                _STATUS["phase"] = f"scored {scored}"
        except Exception as e:
            log.warning("Relevance scorer tick failed: %s", e)
            _STATUS["errors"] += 1
            _STATUS["last_error"] = str(e)[:300]
            _STATUS["phase"] = f"error: {str(e)[:60]}"
            backoff = IDLE_BACKOFF

        for _ in range(int(backoff)):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)

    _STATUS["running"] = False


def _score_batch(tenant_id: str, batch: int) -> int:
    """Score the next N unscored findings. Returns count scored."""
    provider = _get_provider()
    if not provider:
        return 0

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Prioritize findings on entities with the most news mentions —
            # those are the ones a user is most likely to view in the dossier
            # next, so they give the biggest UX win soonest. Newest-first
            # within a signal bucket so fresh investigations don't sit at
            # the back of a long queue.
            cur.execute(
                """
                SELECT fi.finding_id, fi.source, fi.title, fi.data,
                       ei.entity_id,
                       e.canonical_name, e.canonical_type, e.signal_count
                  FROM investigation_findings fi
                  JOIN entity_investigations  ei USING (investigation_id)
                  JOIN entities e ON e.entity_id = ei.entity_id
                 WHERE fi.tenant_id = %s
                   AND fi.relevance_score IS NULL
                   AND ei.entity_id IS NOT NULL
                 ORDER BY COALESCE(e.signal_count, 0) DESC,
                          fi.created_at DESC
                 LIMIT %s
                """,
                (tenant_id, batch),
            )
            rows = cur.fetchall()

        if not rows:
            return 0

        scored = 0
        for finding_id, source, title, data, entity_id, name, etype, sig in rows:
            try:
                from core.ai.provider import LLMMessage
                prompt = _build_prompt(name, etype or "unknown", sig or 0,
                                        source, title or "", data or {})
                resp = provider.complete(
                    [LLMMessage(role="user", content=prompt)],
                    max_tokens=120, temperature=0.0,
                )
                if not resp or not resp.content or resp.error:
                    continue
                parsed = _parse_score(resp.content)
                if not parsed:
                    continue
                score, reason = parsed

                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE investigation_findings
                           SET relevance_score = %s,
                               relevance_reason = %s,
                               relevance_scored_at = now(),
                               relevance_model = %s
                         WHERE finding_id = %s
                        """,
                        (score, reason, "gemma4:e2b", finding_id),
                    )
                scored += 1
                _STATUS["scored_total"] += 1
                if score >= 0.85:
                    _STATUS["scored_strong"] += 1
                elif score >= 0.5:
                    _STATUS["scored_moderate"] += 1
                elif score >= 0.2:
                    _STATUS["scored_weak"] += 1
                else:
                    _STATUS["scored_reject"] += 1
            except Exception as e:
                log.debug("Failed to score finding %s: %s", finding_id, e)
                _STATUS["errors"] += 1

        conn.commit()
        return scored
