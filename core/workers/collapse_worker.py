"""
Continuous-collapse worker.

For each ingested-but-not-yet-extracted document in a library, run the
full chain:
  1. batched mention extraction (LLM, ~1.3s/paragraph)
  2. resolve mentions → canonical entities
  3. batched statement extraction
  4. assessment promotion (which auto-spawns events via Phase 2)
  5. library_member_sync

Marks each document with a metadata flag so we don't reprocess.

Run modes:
  python -m core.workers.collapse_worker --max-docs 3   # one tick
  python -m core.workers.collapse_worker --continuous   # daemon mode

Or via FastAPI startup: start_worker() registers a continuous-loop
thread that ticks every _TICK_INTERVAL_S seconds. Focus-aware via
library_manager.get_current_focus() — focused library's docs jump
the queue.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, "/app")

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.collapse")

# Continuous-loop scaffolding
_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS: dict = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "ticks": 0,
    "docs_processed_total": 0,
    "errors": 0,
    # sense-collapse health (so the manager / a monitor can confirm collapse is
    # actually happening — collapsed should climb, parked ratio should stay sane)
    "senses_collapsed_total": 0,
    "senses_parked_total": 0,
    "last_tick_senses": None,
}
_TICK_INTERVAL_S = 30  # seconds between ticks
_TICK_MAX_DOCS = 3     # docs per tick (bounded so one slow PDF doesn't hog the loop)
_TICK_BATCH_SIZE = 8   # paragraphs per LLM batch


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_loop, args=(tenant_id,), name="collapse-worker", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    return {"status": "started"}


def stop_worker():
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=30)
    _STATUS["running"] = False
    return {"status": "stopped"}


def _loop(tenant_id: str):
    """Continuous tick loop. Each tick processes up to _TICK_MAX_DOCS
    eligible documents through mention → statement → promotion."""
    while not _STOP_EVENT.is_set():
        try:
            stats = run_tick(max_docs=_TICK_MAX_DOCS,
                             batch_size=_TICK_BATCH_SIZE)
            _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
            _STATUS["ticks"] += 1
            _STATUS["docs_processed_total"] += stats.get("docs_processed", 0)
            _STATUS["senses_collapsed_total"] += stats.get("senses_collapsed", 0)
            _STATUS["senses_parked_total"] += stats.get("senses_parked", 0)
            _STATUS["last_tick_senses"] = {
                "collapsed": stats.get("senses_collapsed", 0),
                "parked": stats.get("senses_parked", 0),
            }
            _STATUS["phase"] = (
                f"idle — processed {stats.get('docs_processed', 0)} docs last tick"
            )
        except Exception as e:
            log.warning("collapse tick failed: %s", e, exc_info=False)
            _STATUS["errors"] += 1
            _STATUS["phase"] = f"error: {str(e)[:60]}"
        for _ in range(_TICK_INTERVAL_S):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)
    _STATUS["running"] = False


def _eligible_documents(limit: int) -> list[tuple[int, int]]:
    """Return list of (document_id, library_id) for docs that have been
    ingested into a library and don't yet have mentions/statements
    extracted.

    Focus-aware: if library_manager.get_current_focus() returns a focused
    library, that library's docs are processed first. Other libraries'
    docs fill remaining slots in the batch — focus is a sort priority,
    not a hard filter, so ambient work still progresses if focus is empty.
    """
    focus_library_id = None
    try:
        from core.workers.library_manager import get_current_focus
        focus = get_current_focus()
        if focus and focus.get("phase") in ("collapse", "ingest"):
            focus_library_id = focus.get("library_id")
    except Exception:
        pass

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Focus-aware ordering: put focused library's docs first.
            # SELECT DISTINCT + ORDER BY requires ordering expression to
            # appear in the select list — wrap in a subquery to make
            # PostgreSQL happy.
            cur.execute(
                """
                SELECT document_id, library_id FROM (
                  SELECT DISTINCT
                    lm.member_id AS document_id,
                    lm.library_id,
                    (CASE WHEN %s IS NOT NULL AND lm.library_id = %s
                          THEN 0 ELSE 1 END) AS focus_order
                  FROM library_members lm
                  JOIN documents d ON d.document_id = lm.member_id
                  WHERE lm.member_kind = 'document'
                    AND d.created_at < NOW() - INTERVAL '30 seconds'
                    AND (
                      NOT EXISTS (SELECT 1 FROM document_mentions
                                   WHERE document_id = lm.member_id)
                      OR
                      (EXISTS (SELECT 1 FROM document_mentions WHERE document_id = lm.member_id)
                       AND NOT EXISTS (SELECT 1 FROM statements WHERE document_id = lm.member_id))
                    )
                ) sub
                ORDER BY focus_order, document_id
                LIMIT %s
                """,
                (focus_library_id, focus_library_id, limit),
            )
            return [(r[0], r[1]) for r in cur.fetchall()]


def _resolve_mentions(document_id: int) -> dict:
    """Walk unresolved mentions in this document and call
    resolve_entity for each. Returns counts."""
    from core.gdu.entity_resolution import resolve_entity
    stats = {"scanned": 0, "resolved": 0, "skipped": 0, "errors": 0}
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT mention_id, surface_form, suggested_type
                  FROM document_mentions
                 WHERE document_id = %s AND entity_id IS NULL
                """,
                (document_id,),
            )
            mentions = cur.fetchall()
    for mid, surface, stype in mentions:
        stats["scanned"] += 1
        if not surface or not stype or stype == "unknown":
            stats["skipped"] += 1
            continue
        try:
            r = resolve_entity(
                tenant_id="default", name=surface, entity_type=stype,
                source_pipeline="collapse_worker",
            )
            if r and r.entity_id:
                with get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE document_mentions SET entity_id = %s WHERE mention_id = %s",
                            (r.entity_id, mid),
                        )
                    conn.commit()
                stats["resolved"] += 1
            else:
                # Stop mention_backfill from re-firing the LDU gate every
                # minute (which would otherwise duplicate-stage in entity_ldu_p
                # ~1,440x/day). Routed/dropped/unknown rejections flip the
                # mention's resolution_status so the backfill query
                # (WHERE resolution_status='unresolved') skips it. When the
                # vocab expands and the entity is later promoted from LDU-P,
                # an operator action or vocab-bump worker can flip it back.
                reason = (r.match_reason if r else "") or ""
                if reason.startswith(("route:", "ldu_drop:", "ldu_unknown:")):
                    try:
                        with get_connection() as conn:
                            with conn.cursor() as cur:
                                cur.execute(
                                    "UPDATE document_mentions "
                                    "SET resolution_status = 'routed_to_ldup' "
                                    "WHERE mention_id = %s",
                                    (mid,),
                                )
                            conn.commit()
                    except Exception:
                        log.debug("status flip failed for mention %d", mid,
                                  exc_info=True)
                stats["skipped"] += 1
        except Exception:
            log.exception("resolve failed for mention %d", mid)
            stats["errors"] += 1
    return stats


# ── Sense layer: lexicon packs (loaded once, cached) ────────────────────────
_SENSE_STOP: Optional[set] = None
_SENSE_PINS: Optional[dict] = None


def _sense_stop() -> set:
    """Stopwords = the function_words pack (closed-class English)."""
    global _SENSE_STOP
    if _SENSE_STOP is None:
        try:
            import json as _j
            p = _j.load(open("/app/libraries/function_words/definitions/function_words.senses.json"))
            _SENSE_STOP = {s["lemma"].lower() for s in p["content"]["senses"]}
        except Exception:
            _SENSE_STOP = set()
    return _SENSE_STOP


def _sense_pins() -> dict:
    """General (domain-agnostic) sense pins — always safe to apply (say→express,
    deny→declare-untrue, …). Domain-specific packs (legal/biomed) need per-document
    domain and are layered in later; general covers the common discourse verbs."""
    global _SENSE_PINS
    if _SENSE_PINS is None:
        try:
            import json as _j
            _SENSE_PINS = _j.load(
                open("/app/libraries/domains/definitions/general.senses.json"))["preferences"]
        except Exception:
            _SENSE_PINS = {}
    return _SENSE_PINS


def _resolve_senses(document_id: int, tenant: str = "default") -> dict:
    """Disambiguate the document's PROSE content words to senses through the one
    governed equation. ENTITY-AWARE: proper-noun / document_mention spans are
    excluded so names route to the entity layer, not WordNet (no "Acme"→painter,
    no "palm"→hand). Lemmatized, with general-pack sense pins; NO document-soup
    collocation (that signal is inert-or-harmful — validated). Writes reversible
    collapse_decisions. Skips silently if no lexical grid is loaded."""
    import re
    from core.language.sense_resolver import resolve_sense
    from core.language.lemmatizer import lemmas
    from core.language.lexical_tokens import lexical_tokens
    from core.documents.text_quality import chunk_quality
    stop, pins = _sense_stop(), _sense_pins()

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM concept_nodes WHERE subtype_path LIKE 'lexical.%%' LIMIT 1")
        if cur.fetchone() is None:
            return {"skipped": "no_lexical_grid"}
        cur.execute("""SELECT paragraph_id, raw_text FROM document_paragraphs
                        WHERE document_id = %s AND raw_text IS NOT NULL
                          AND NOT is_boilerplate AND NOT is_redacted
                          AND paragraph_type IN ('prose', 'transcript')""", (document_id,))
        paras = cur.fetchall()
        cur.execute("""SELECT paragraph_id, char_start, char_end FROM document_mentions
                        WHERE document_id = %s AND char_start IS NOT NULL""", (document_id,))
        spans: dict = {}
        for pid, a, b in cur.fetchall():
            spans.setdefault(pid, []).append((a, b))
        # every word that appears in ANY of this doc's entity-mention surfaces is an
        # entity token — exclude it from sense resolution everywhere in the doc, even
        # where it sits at a sentence start outside a tagged span (so "Acme" never
        # resolves to the WordNet painter). Belt-and-suspenders with the span mask +
        # capitalization guard inside lexical_tokens.
        cur.execute("""SELECT DISTINCT surface_form FROM document_mentions
                        WHERE document_id = %s AND surface_form IS NOT NULL""", (document_id,))
        entity_tokens = {w.lower() for (sf,) in cur.fetchall() if sf
                         for w in re.findall(r"[A-Za-z][A-Za-z'\-]+", sf)}
        # GATE: only paragraphs that validate as real sentence structure feed the
        # meaning graph. OCR garbage / corrupted text-layers / tabular records stay
        # raw, searchable evidence — they never become senses. (Phase 4 of the OCR
        # text-validation plan; source-agnostic, lexicon-backed, deterministic.)
        valid_paras, garbage = [], 0
        for pid, text in paras:
            if chunk_quality(cur, text).get("is_real_sentence"):
                valid_paras.append((pid, text))
            else:
                garbage += 1

    doc_stop = stop | entity_tokens
    toks: set = set()
    for pid, text in valid_paras:
        toks.update(lexical_tokens(text, spans.get(pid, ()), doc_stop))

    def _pin(t):
        pref = pins.get(t)
        if pref and pref.get("address"):
            return {pref["address"]: 1.0}
        for lm in lemmas(t):
            p = pins.get(lm)
            if p and p.get("address"):
                return {p["address"]: 1.0}
        return None

    stats = {"paragraphs": len(paras), "validated": len(valid_paras),
             "garbage": garbage, "terms": len(toks), "collapsed": 0, "parked": 0}
    for t in toks:
        r = resolve_sense(t, get_connection, lemmatize=True, pins=_pin(t), tenant=tenant)
        stats["collapsed" if r.action == "collapse" else "parked"] += 1
    return stats


def _extract_topics(document_id: int, llm) -> dict:
    """Emergent topic feed — the LLM surfaces what a document is ABOUT; each
    label becomes/updates a topic in the library with this document as a member.

    Peer to the curated Wikipedia-seeded topics, but this is the high-volume
    organic feed, so it stays light: a topic is GOVERNED via create_topic on
    first sight (base-60 address + membership), and on later sightings the
    document is added to topic_membership directly (no re-admission, and no
    membership_rule replacement)."""
    from core.extraction.extractors.topic_extractor import extract_topics_deep
    from core.topics.materialize import create_topic
    from core.topics.rules import user_curated, rule_set

    # Once per document — the LLM call is the cost; skip if this doc is already
    # a member of any emergent (auto_cluster) topic, regardless of via_rule.
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM topic_membership tm JOIN topics t ON t.topic_id=tm.topic_id "
                "WHERE tm.member_id=%s AND tm.member_kind='document' "
                "AND t.created_by='auto_cluster' LIMIT 1", (document_id,))
            if cur.fetchone():
                return {"skipped": "already_extracted"}

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT string_agg(raw_text, ' ' ORDER BY paragraph_id)
                     FROM document_paragraphs
                    WHERE document_id = %s
                      AND COALESCE(metadata->>'quality_band','pass') IN ('pass','record')""",
                (document_id,),
            )
            row = cur.fetchone()
    text = ((row[0] if row else "") or "").strip()
    if len(text) < 200:
        return {"skipped": "too_short"}

    labels = extract_topics_deep(text[:12000], llm=llm)
    created = linked = 0
    for t in labels:
        label = (t.get("label") or "").strip()
        if len(label) < 3:
            continue
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT topic_id FROM topics WHERE tenant_id='default' "
                    "AND normalized_name = lower(regexp_replace(%s, '\\s+', ' ', 'g'))",
                    (label,),
                )
                row = cur.fetchone()
                if row:
                    tid = row[0]
                    cur.execute(
                        "SELECT 1 FROM topic_membership WHERE topic_id=%s "
                        "AND member_kind='document' AND member_id=%s", (tid, document_id))
                    if not cur.fetchone():
                        cur.execute(
                            "INSERT INTO topic_membership "
                            "(topic_id, member_kind, member_id, score, via_rule, tenant_id) "
                            "VALUES (%s,'document',%s,1.0,'emergent_llm','default')",
                            (tid, document_id))
                    cur.execute(
                        "UPDATE topics SET mention_count=mention_count+1, last_seen=NOW() "
                        "WHERE topic_id=%s", (tid,))
                    conn.commit()
                    linked += 1
                    continue
        try:
            create_topic(label=label, subtype="theme",
                         membership_rule=rule_set([user_curated(documents=[document_id])]),
                         created_by="auto_cluster", materialize=True)
            created += 1
        except Exception as e:
            log.debug("emergent topic create failed for %r: %s", label, e)
    return {"extracted": len(labels), "created": created, "linked": linked}


def process_document(document_id: int, library_id: int, batch_size: int = 8) -> dict:
    """Run the full extraction chain on one document."""
    from core.ai.config import get_tenant_provider
    from core.documents.batched_extractor import (
        extract_mentions_batched, extract_statements_batched,
    )
    from core.documents.paragraph_classifier import classify_document
    from core.documents.assessment_promoter import promote_pending
    from core.discovery.library_member_sync import sync_library

    llm = get_tenant_provider("default")
    if not llm:
        return {"ok": False, "error": "no_llm_provider"}

    stats = {"document_id": document_id, "library_id": library_id,
             "started_at": time.time(),
             "classified": None,
             "mentions": None, "resolved": None, "statements": None,
             "promoted": None, "synced": None}

    # 0) Paragraph classification (must precede extraction so the prose
    # filter in extract_mentions_batched / extract_statements_batched works).
    # Idempotent — overwrites paragraph_type with the current classifier.
    log.info("collapse[%d]: classifying paragraphs…", document_id)
    stats["classified"] = classify_document(document_id)

    # 0b) Record extraction — parse 'record'-typed paragraphs (structured
    # date-id-subject rows like CIA FOIA case logs) into events. No LLM
    # needed; regex-driven. Runs every pass; events get inserted fresh
    # but the title+date duplicate handling is upstream of this worker.
    try:
        from core.documents.record_extractor import extract_records
        stats["records"] = extract_records(document_id)
    except Exception as e:
        log.debug("record_extractor failed for doc %d: %s", document_id, e)
        stats["records"] = {"error": str(e)}

    # 1) Mentions (if none yet)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM document_mentions WHERE document_id = %s "
                "AND extraction_method <> 'identity_match'",
                (document_id,),
            )
            mention_count = int(cur.fetchone()[0] or 0)

    # 1a) Tier-1 identity matching — known entities via the identity library
    # (deterministic, OCR-robust, no LLM). Works on any document.
    try:
        from core.extraction.identity_match import match_document as _id_match
        stats["identity"] = _id_match(document_id)
        log.info("collapse[%d]: identity match -> %s known-entity mentions",
                 document_id, stats["identity"].get("identity_mentions"))
    except Exception as e:
        log.debug("identity match failed for doc %d: %s", document_id, e)

    # 1b) Tier-2 discovery — the LLM finds novel names the library doesn't know.
    if mention_count == 0:
        log.info("collapse[%d]: mentions pass…", document_id)
        stats["mentions"] = extract_mentions_batched(
            document_id=document_id, llm=llm, batch_size=batch_size,
            skip_boilerplate=False,
        )

    # 2) Entity resolution
    log.info("collapse[%d]: entity resolution…", document_id)
    stats["resolved"] = _resolve_mentions(document_id)

    # 2b) Sense resolution — disambiguate the document's known terms against the
    # loaded lexical libraries (WordNet + catalog) through the SAME equation, with
    # co-occurring terms as collocation context. Writes reversible collapse_decisions
    # (the sense layer, live). Skips silently if no lexical grid is loaded.
    try:
        stats["senses"] = _resolve_senses(document_id)
    except Exception as e:
        log.debug("sense resolution failed for doc %d: %s", document_id, e)
        stats["senses"] = {"error": str(e)[:200]}

    # 2c) Topic extraction — the emergent aboutness feed. The LLM surfaces what
    # the document is ABOUT into the topic library (peer to the curated
    # Wikipedia-seeded topics). Once per doc, non-fatal.
    try:
        stats["topics"] = _extract_topics(document_id, llm)
        log.info("collapse[%d]: topics -> %s", document_id, stats["topics"])
    except Exception as e:
        log.debug("topic extraction failed for doc %d: %s", document_id, e)
        stats["topics"] = {"error": str(e)[:200]}

    # 3) Statements (if none yet)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM statements WHERE document_id = %s",
                (document_id,),
            )
            statement_count = int(cur.fetchone()[0] or 0)
    if statement_count == 0:
        log.info("collapse[%d]: statements pass…", document_id)
        stats["statements"] = extract_statements_batched(
            document_id=document_id, llm=llm, batch_size=batch_size,
            skip_boilerplate=False,
        )

    # 4) Property extraction — Gemma tags property+value+precision_tier
    # per mention; collapse operator handles refinement/conflict admission.
    # Skipped silently if the substrate doesn't have property concepts
    # seeded yet (i.e. propertyvocab pack not loaded).
    try:
        from core.documents.batched_extractor import extract_properties_batched
        # Check if any typed properties already exist for this doc's
        # entities — if every paragraph already has property assertions,
        # skip to save Gemma cycles. Cheap O(1) check.
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT COUNT(*) FROM assessments a
                        WHERE a.property_id IS NOT NULL
                          AND a.subj_entity_id IN (
                              SELECT DISTINCT entity_id FROM document_mentions
                               WHERE document_id = %s AND entity_id IS NOT NULL
                          )""",
                    (document_id,),
                )
                already_typed = int(cur.fetchone()[0] or 0)
        if already_typed < 10:
            log.info("collapse[%d]: property extraction pass…", document_id)
            stats["properties"] = extract_properties_batched(
                document_id=document_id, llm=llm, batch_size=batch_size,
                skip_boilerplate=False,
            )
        else:
            stats["properties"] = {"skipped": "already_typed",
                                    "existing_assertions": already_typed}
    except ImportError:
        # Property substrate not available — skip silently
        stats["properties"] = {"skipped": "substrate_not_loaded"}
    except Exception as e:
        log.debug("property extraction failed for doc %d: %s", document_id, e)
        stats["properties"] = {"error": str(e)[:200]}

    # 5) Promotion (collapses statements → assessments + auto-spawns events)
    log.info("collapse[%d]: promoting statements to assessments…", document_id)
    stats["promoted"] = promote_pending(limit=500)

    # 6) Library member sync
    log.info("collapse[%d]: library_member_sync…", document_id)
    stats["synced"] = sync_library(library_id)

    stats["elapsed_s"] = round(time.time() - stats.pop("started_at"), 1)
    return stats


def run_tick(max_docs: int = 3, batch_size: int = 8) -> dict:
    """One tick: find up to max_docs eligible documents, process each."""
    docs = _eligible_documents(max_docs)
    log.info("collapse tick: %d eligible documents", len(docs))
    results = []
    sc = sp = 0
    for doc_id, lib_id in docs:
        try:
            r = process_document(doc_id, lib_id, batch_size=batch_size)
            s = r.get("senses")
            if isinstance(s, dict):
                sc += s.get("collapsed", 0)
                sp += s.get("parked", 0)
            results.append(r)
        except Exception:
            log.exception("collapse failed for doc %d", doc_id)
            results.append({"document_id": doc_id, "error": "see logs"})
    if sc or sp:
        log.info("collapse tick: senses collapsed=%d parked=%d", sc, sp)
    return {"docs_processed": len(results), "senses_collapsed": sc,
            "senses_parked": sp, "results": results}


if __name__ == "__main__":
    import argparse, json
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--max-docs", type=int, default=3)
    p.add_argument("--batch-size", type=int, default=8)
    args = p.parse_args()
    print(json.dumps(run_tick(args.max_docs, args.batch_size), indent=2, default=str))
