"""signal_statement_extractor.py — extract (subject, predicate, object,
time, place) statements from RSS signal text the same way the existing
document statement_extractor does for PDF paragraphs.

Wiring:
  - Input: signals where signal_type='signal.content.rss_entry' and no
    statements yet (LEFT JOIN statements ON statements.signal_rid = s.rid
    WHERE statements.statement_id IS NULL).
  - For each: title + first_paragraph (capped 2000 chars) → LLM extraction.
  - Persist statements with signal_rid set, document_id NULL.
  - assessment_promoter picks them up automatically — same collapse_key
    math, same fact/observation/claim tiering as PDF-derived statements.

Cost knob: ``--batch N`` limits how many signals to process per run. A full
day's RSS (~2,000 signals) would be ~2,000 LLM calls; default --batch 30
keeps cost modest while we tune the prompt + extraction quality.

Run modes:
  python -m core.workers.signal_statement_extractor                  # one batch
  python -m core.workers.signal_statement_extractor --batch 100      # bigger
  python -m core.workers.signal_statement_extractor --since 6h       # window
  python -m core.workers.signal_statement_extractor --continuous     # loop
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from storage.postgres.db import get_connection
from core.documents.statement_extractor import (
    _llm_extract_statements,
    _lemmatize_verb,
)

log = logging.getLogger("enki.workers.signal_statement_extractor")

# Continuous-loop scaffolding (matches auto_validator pattern)
_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS: dict = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "statements_total": 0,
    "errors": 0,
}
_BATCH_SIZE = 10
_CHECK_INTERVAL = 90  # seconds


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_continuous_loop, args=(tenant_id,),
        name="signal-statement-extractor", daemon=True,
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
    """Pull a small batch every _CHECK_INTERVAL seconds. Batches stay
    small to bound LLM cost; full backlog drained over time."""
    try:
        llm = _build_llm()
    except Exception as e:
        log.warning("LLM build failed at startup: %s", e)
        llm = None

    while not _STOP_EVENT.is_set():
        try:
            if llm is None:
                # Re-attempt to build provider periodically
                llm = _build_llm()
            if llm is not None:
                stats = run_once(batch=_BATCH_SIZE, since=None,
                                 tenant_id=tenant_id, llm=llm)
                _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
                _STATUS["statements_total"] += stats.get("statements_persisted", 0)
                _STATUS["phase"] = f"idle — {stats.get('statements_persisted', 0)} stmts last pass"
            else:
                _STATUS["phase"] = "waiting for LLM provider"
        except Exception as e:
            log.warning("cycle failed: %s", e)
            _STATUS["errors"] += 1
            _STATUS["phase"] = f"error: {str(e)[:60]}"
        for _ in range(_CHECK_INTERVAL):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)
    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"


# ─── Persistence ──────────────────────────────────────────────────────────

def _persist_signal_statement(
    *,
    tenant_id: str,
    signal_rid: int,
    speaker_mention_id: Optional[int],
    speaker_entity_id: Optional[int],
    predicate: str,
    object_mention_id: Optional[int],
    object_entity_id: Optional[int],
    raw_text: Optional[str],
    event_time: Optional[datetime],
    location_text: Optional[str],
    modality: str,
    polarity: str,
    confidence: float,
    predicate_verb_surface: str,
    predicate_lemma: str,
) -> int:
    """Write a statement row sourced from an RSS signal (document_id NULL,
    signal_rid set)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO statements
                    (tenant_id, document_id, page_id, paragraph_id,
                     speaker_entity_id, speaker_mention_id,
                     predicate, object_entity_id, object_mention_id,
                     raw_text, summarized_text,
                     modality, confidence, polarity,
                     extraction_method, event_time,
                     predicate_verb_surface, predicate_lemma,
                     surrounding_text, validation_status,
                     signal_rid)
                VALUES (%s, NULL, NULL, NULL, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, 'llm_v1_signal', %s, %s, %s, %s, 'pending',
                        %s)
                RETURNING statement_id
                """,
                (tenant_id,
                 speaker_entity_id, speaker_mention_id,
                 predicate, object_entity_id, object_mention_id,
                 raw_text, raw_text,
                 modality, confidence, polarity,
                 event_time,
                 predicate_verb_surface, predicate_lemma,
                 location_text, signal_rid),
            )
            stmt_id = cur.fetchone()[0]
        conn.commit()
    return stmt_id


# ─── Mention helpers ──────────────────────────────────────────────────────

def _mentions_for_signal(signal_rid: int) -> list[dict]:
    """Pull entity_mentions for this signal in the shape _llm_extract_statements
    expects."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT em.name_raw, e.canonical_type, em.mention_id, em.entity_id
                  FROM entity_mentions em
                  JOIN entities e ON e.entity_id = em.entity_id
                 WHERE em.signal_rid = %s
                   AND em.name_raw IS NOT NULL
                """,
                (signal_rid,),
            )
            return [
                {"surface_form": r[0], "suggested_type": r[1] or "unknown",
                 "mention_id": r[2], "entity_id": r[3]}
                for r in cur.fetchall()
            ]


def _find_in_mentions(mentions: list[dict], surface: str
                      ) -> Optional[tuple[int, int]]:
    """Loose match of an LLM-output surface against the mention list.
    Returns (mention_id, entity_id) on match."""
    if not surface:
        return None
    s = surface.strip().lower()
    if len(s) < 2:
        return None
    # Exact first
    for m in mentions:
        if (m["surface_form"] or "").strip().lower() == s:
            return (m["mention_id"], m["entity_id"])
    # Substring fallback (case-insensitive)
    for m in mentions:
        sf = (m["surface_form"] or "").strip().lower()
        if not sf:
            continue
        if s in sf or sf in s:
            return (m["mention_id"], m["entity_id"])
    return None


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


# ─── Extraction core ──────────────────────────────────────────────────────

def extract_from_signal(*, signal_rid: int, tenant_id: str, llm: Any) -> dict:
    """Extract statements for one RSS signal. Returns counters."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT payload->>'title' AS title,
                       payload->>'first_paragraph' AS first_paragraph,
                       payload->>'summary' AS summary
                  FROM signals
                 WHERE rid = %s
                """,
                (signal_rid,),
            )
            row = cur.fetchone()
    if not row:
        return {"persisted": 0, "candidates": 0, "skipped_no_subject": 0}

    title = row[0] or ""
    first_paragraph = row[1] or row[2] or ""
    text = (title + "\n\n" + first_paragraph).strip()[:2000]
    if not text:
        return {"persisted": 0, "candidates": 0, "skipped_no_subject": 0}

    mentions = _mentions_for_signal(signal_rid)
    candidates = _llm_extract_statements(text, mentions, llm)

    persisted = 0
    skipped_no_subject = 0
    skipped_self_ref = 0
    skipped_noise = 0
    from core.documents.statement_extractor import _is_fragment_or_table
    for c in candidates:
        try:
            # Drop noise raw_text BEFORE entity resolution.
            if _is_fragment_or_table(c.get("raw_text") or ""):
                skipped_noise += 1
                continue

            subj_ref = _find_in_mentions(mentions, c["subject"])
            if not subj_ref:
                # Cannot resolve subject → skip (would write a dangling row)
                skipped_no_subject += 1
                continue
            obj_ref = _find_in_mentions(mentions, c["object"]) if c["object"] else None
            obj_mention_id, obj_entity_id = (obj_ref or (None, None))

            # Drop self-references (X did X to X).
            if (subj_ref[1] is not None and obj_entity_id is not None
                    and subj_ref[1] == obj_entity_id):
                skipped_self_ref += 1
                continue

            # NB: statements.speaker_mention_id / object_mention_id are FKs
            # to document_mentions, not entity_mentions. RSS-derived statements
            # NULL them out and rely on entity_id linkage only — entity_mentions
            # is queryable via signal_rid for provenance.
            _persist_signal_statement(
                tenant_id=tenant_id,
                signal_rid=signal_rid,
                speaker_mention_id=None,
                speaker_entity_id=subj_ref[1],
                predicate=c["verb"],
                object_mention_id=None,
                object_entity_id=obj_entity_id,
                raw_text=c["raw_text"] or text[:500],
                event_time=_parse_iso(c.get("date_iso")),
                location_text=c.get("location_text"),
                modality=c["modality"],
                polarity=c["polarity"],
                confidence=c["confidence"],
                predicate_verb_surface=c["verb"],
                predicate_lemma=_lemmatize_verb(c["verb"]),
            )
            persisted += 1
        except Exception:
            log.exception("persist failed for signal %s", signal_rid)

    return {"persisted": persisted,
            "candidates": len(candidates),
            "skipped_no_subject": skipped_no_subject,
            "skipped_self_ref": skipped_self_ref,
            "skipped_noise": skipped_noise}


# ─── Batch driver ─────────────────────────────────────────────────────────

def run_once(*, batch: int, since: Optional[datetime], tenant_id: str,
             llm: Any) -> dict:
    totals = {"signals_processed": 0, "statements_persisted": 0,
              "candidates_total": 0, "skipped_no_subject": 0}
    wm_clause = "AND s.timestamp >= %s" if since else ""
    wm_params: tuple = (since,) if since else ()

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Only process signals that have entity_mentions extracted —
            # without them, the LLM has no legal subject set and every
            # statement gets skipped. Mentions extraction runs upstream
            # in the article worker; we just wait for it.
            cur.execute(
                f"""
                SELECT s.rid
                  FROM signals s
                  LEFT JOIN statements st ON st.signal_rid = s.rid
                 WHERE s.tenant_id = %s
                   AND s.signal_type = 'signal.content.rss_entry'
                   AND st.statement_id IS NULL
                   AND EXISTS (
                     SELECT 1 FROM entity_mentions em
                      WHERE em.signal_rid = s.rid
                   )
                   {wm_clause}
                 ORDER BY s.rid DESC
                 LIMIT %s
                """,
                (tenant_id, *wm_params, batch),
            )
            signal_rids = [r[0] for r in cur.fetchall()]

    log.info("processing %d signals", len(signal_rids))
    for sig_rid in signal_rids:
        result = extract_from_signal(
            signal_rid=sig_rid, tenant_id=tenant_id, llm=llm,
        )
        totals["signals_processed"] += 1
        totals["statements_persisted"] += result["persisted"]
        totals["candidates_total"] += result["candidates"]
        totals["skipped_no_subject"] += result["skipped_no_subject"]
        if result["persisted"]:
            log.info("signal %s: %d statements", sig_rid, result["persisted"])
    log.info("done: %s", totals)
    return totals


def _build_llm():
    """Build an LLM provider. Uses the same path as other workers
    (core.ai.resolver.get_effective_provider) — Gemma 4 on node-A, cloud on
    cloud profile."""
    try:
        from core.ai.resolver import get_effective_provider
        return get_effective_provider()
    except Exception as e:
        log.warning("LLM provider unavailable: %s", e)
        return None


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )
    p = argparse.ArgumentParser()
    p.add_argument("--batch", type=int, default=30,
                   help="How many signals per pass (default 30)")
    p.add_argument("--since", default=None,
                   help="ISO datetime OR shorthand: 1h, 24h, 7d, 30m")
    p.add_argument("--tenant", default="default")
    p.add_argument("--continuous", action="store_true",
                   help="Loop every 90s")
    args = p.parse_args()

    since: Optional[datetime] = None
    if args.since:
        if args.since.endswith("h"):
            since = datetime.now(timezone.utc) - timedelta(hours=int(args.since[:-1]))
        elif args.since.endswith("d"):
            since = datetime.now(timezone.utc) - timedelta(days=int(args.since[:-1]))
        elif args.since.endswith("m"):
            since = datetime.now(timezone.utc) - timedelta(minutes=int(args.since[:-1]))
        else:
            since = datetime.fromisoformat(args.since)

    llm = _build_llm()

    if args.continuous:
        log.info("continuous mode — batch=%d every 90s", args.batch)
        while True:
            try:
                run_once(batch=args.batch, since=since,
                         tenant_id=args.tenant, llm=llm)
            except Exception as e:
                log.error("cycle failed: %s", e)
            time.sleep(90)
    else:
        run_once(batch=args.batch, since=since,
                 tenant_id=args.tenant, llm=llm)
    return 0


if __name__ == "__main__":
    sys.exit(main())
