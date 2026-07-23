"""
Document-to-library relevance gate (open contributions).

Polls `contributions` rows in status='validating' and decides whether the
contributed document actually belongs in the library it was submitted to.
This is the gate that turns the contamination problem into a feature: a doc
titled "Alex Acme Controversy" whose content is a 1988 Broadcasting
magazine scores ~0 and is FLAGGED (kept visible, marked, weight≈0), and the
contributor's reputation drops.

Mirrors core/workers/finding_relevance_scorer.py (daemon + status dict +
start/stop). Two scoring paths:

  - LLM path: compare doc title + first-page text to the library's focal term.
  - Deterministic fallback (no LLM / fast): the proven detector — if the doc's
    extracted mentions never name the library's focal entity, flag it.

On `relevant`: mark accepted, bump contributor trust, and run the existing
extraction pipeline (process_document) so the doc's entities/facts land.
On `not_relevant`: flag the doc + membership, drop contributor trust. The mark
and source link always remain visible.
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

log = logging.getLogger("enki.workers.document_relevance_scorer")

CHECK_INTERVAL = 20
BATCH_SIZE = 5
IDLE_BACKOFF = 180
THRESHOLD = 0.5            # >= keeps the doc; < flags it
FIRST_PAGE_CHARS = 1200

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS = {
    "running": False, "phase": "idle", "last_run": None, "tick_count": 0,
    "relevant": 0, "flagged": 0, "errors": 0, "last_error": None,
}


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default") -> dict:
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(target=_loop, args=(tenant_id,),
                                      name="document-relevance-scorer", daemon=True)
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    log.info("Document relevance scorer started")
    return {"status": "started"}


def stop_worker() -> dict:
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=5)
    _STATUS["running"] = False
    return {"status": "stopped"}


def _get_provider():
    try:
        from core.ai.resolver import get_effective_provider
        return get_effective_provider()
    except Exception as e:
        log.warning("LLM provider unavailable: %s", e)
        return None


def _first_page_text(document_id: int) -> str:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT raw_text FROM document_pages
                    WHERE document_id=%s AND raw_text IS NOT NULL
                    ORDER BY page_number LIMIT 2""", (document_id,))
            rows = cur.fetchall()
    return re.sub(r"\s+", " ", " ".join(r[0] for r in rows if r[0]))[:FIRST_PAGE_CHARS]


def _focal_mentions_present(document_id: int, focal_term: str) -> Optional[bool]:
    """Deterministic fallback signal: does the doc's text name the focal term?
    Returns None if focal_term is empty."""
    if not focal_term:
        return None
    # match any focal token (e.g. 'Acme') in the surface forms / page text
    token = focal_term.split()[-1].lower()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT EXISTS (
                       SELECT 1 FROM document_mentions
                        WHERE document_id=%s AND lower(surface_form) LIKE %s)
                    OR EXISTS (
                       SELECT 1 FROM document_pages
                        WHERE document_id=%s AND lower(raw_text) LIKE %s)""",
                (document_id, f"%{token}%", document_id, f"%{token}%"))
            return bool(cur.fetchone()[0])


def _build_prompt(focal_term: str, library_name: str, title: str, first_page: str) -> str:
    return f"""You are a library curator. Decide whether a contributed document belongs
in a curated library, based ONLY on content match to the library's focal topic.

Library:
  focal topic: {focal_term or library_name}
  name: {library_name}

Document:
  title: {(title or '(untitled)')[:200]}
  first page text: {first_page[:FIRST_PAGE_CHARS]}

Score 0.0-1.0:
  1.0 = clearly on-topic, a primary source about the focal topic
  0.7 = clearly relevant (discusses the topic / its people / context)
  0.3 = only mentions it in passing
  0.0 = off-topic (no real connection to the focal topic)

Respond with ONLY JSON: {{"score": 0.X, "reason": "<one short sentence>"}}"""


def _parse_score(text: str) -> Optional[tuple[float, str]]:
    if not text:
        return None
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    m = re.search(r"\{[^{}]*\}", s)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        score = max(0.0, min(1.0, float(obj.get("score"))))
        return (score, str(obj.get("reason") or "")[:500])
    except Exception:
        return None


def _apply_verdict(contribution_id: int, document_id: Optional[int], library_id: int,
                    contributor_id: str, score: float, reason: str, model: str) -> bool:
    """Write the verdict everywhere; return True if relevant (kept)."""
    relevant = score >= THRESHOLD
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE contributions
                      SET status=%s, relevance_score=%s, relevance_reason=%s,
                          relevance_model=%s, relevance_scored_at=NOW(), updated_at=NOW()
                    WHERE contribution_id=%s""",
                ("relevant" if relevant else "not_relevant", score, reason, model, contribution_id))
            if document_id:
                cur.execute("UPDATE documents SET relevance_status=%s WHERE document_id=%s",
                            ("relevant" if relevant else "flagged", document_id))
                cur.execute(
                    """UPDATE library_members SET submission_status=%s, relevance_score=%s
                        WHERE library_id=%s AND member_kind='document' AND member_id=%s""",
                    ("accepted" if relevant else "flagged", score, library_id, document_id))
            # reputation: relevant nudges trust up, flagged drops it (yield-accountant style)
            if relevant:
                cur.execute(
                    """UPDATE contributors
                          SET accepted_count = accepted_count + 1,
                              trust_score = LEAST(1.0, trust_score + 0.05),
                              last_seen_at = NOW()
                        WHERE contributor_id=%s""", (contributor_id,))
            else:
                cur.execute(
                    """UPDATE contributors
                          SET flagged_count = flagged_count + 1,
                              trust_score = GREATEST(0.0, trust_score - 0.10),
                              last_seen_at = NOW()
                        WHERE contributor_id=%s""", (contributor_id,))
        conn.commit()
    return relevant


def _extract_and_accept(document_id: int, library_id: int, contribution_id: int) -> None:
    """Run the full extraction pipeline on a relevant contributed doc, then mark accepted."""
    try:
        from core.workers.collapse_worker import process_document
        process_document(document_id, library_id, batch_size=8)
    except Exception as e:
        log.warning("extraction failed for contributed doc %s: %s", document_id, e)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE contributions SET status='accepted', updated_at=NOW() WHERE contribution_id=%s",
                        (contribution_id,))
        conn.commit()


def _loop(tenant_id: str) -> None:
    backoff = CHECK_INTERVAL
    while not _STOP_EVENT.is_set():
        try:
            n = _score_batch(tenant_id, BATCH_SIZE)
            _STATUS["tick_count"] += 1
            _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
            _STATUS["last_error"] = None
            backoff = CHECK_INTERVAL if n else min(backoff * 2, IDLE_BACKOFF)
            _STATUS["phase"] = f"scored {n}" if n else f"idle (backoff {backoff}s)"
        except Exception as e:
            log.warning("relevance tick failed: %s", e)
            _STATUS["errors"] += 1
            _STATUS["last_error"] = str(e)[:300]
            backoff = IDLE_BACKOFF
        for _ in range(int(backoff)):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)
    _STATUS["running"] = False


def _score_batch(tenant_id: str, batch: int) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.contribution_id, c.document_id, c.library_id, c.contributor_id,
                          d.title, l.canonical_name, l.focal_term
                     FROM contributions c
                     JOIN libraries l ON l.library_id = c.library_id
                     LEFT JOIN documents d ON d.document_id = c.document_id
                    WHERE c.tenant_id=%s AND c.status='validating'
                    ORDER BY c.created_at LIMIT %s""", (tenant_id, batch))
            rows = cur.fetchall()
    if not rows:
        return 0

    provider = _get_provider()
    scored = 0
    for cid, doc_id, lib_id, contributor_id, title, lib_name, focal in rows:
        try:
            score = reason = model = None
            if provider and doc_id:
                from core.ai.provider import LLMMessage
                prompt = _build_prompt(focal or "", lib_name or "", title or "", _first_page_text(doc_id))
                resp = provider.complete([LLMMessage(role="user", content=prompt)],
                                         max_tokens=120, temperature=0.0)
                if resp and resp.content and not resp.error:
                    parsed = _parse_score(resp.content)
                    if parsed:
                        score, reason = parsed
                        model = "gemma4:e2b"
            if score is None:
                # deterministic fallback: focal-term presence
                present = _focal_mentions_present(doc_id, focal or "") if doc_id else None
                score = 0.8 if present else (0.0 if present is False else 0.5)
                reason = ("focal term present in document" if present
                          else "focal term absent from document (auto-flag)" if present is False
                          else "no focal term to check")
                model = "deterministic"

            relevant = _apply_verdict(cid, doc_id, lib_id, contributor_id, score, reason, model)
            if relevant and doc_id:
                _extract_and_accept(doc_id, lib_id, cid)
                _STATUS["relevant"] += 1
            else:
                _STATUS["flagged"] += 1
            scored += 1
        except Exception as e:
            log.debug("failed to score contribution %s: %s", cid, e)
            _STATUS["errors"] += 1
    return scored
