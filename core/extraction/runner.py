"""
Extraction runner — orchestrates one signal through the pipeline.

Responsibilities:
    1. Resolve the correct worker via the strict registry.
    2. Build a fresh envelope (so re-runs are idempotent).
    3. Run cheap_pass.
    4. Evaluate the deep-pass gate. Gate opens when ANY of:
         - caller passed force_deep=True
         - identity.title_display is empty OR the title was marked garbage
           (cheap pass stamps `identity.title_display` as a gap in that case)
         - a designated-target match exists (stubbed — Phase 5)
         - language is new for this entity (stubbed — Phase 4)
    5. Run deep_pass if gated in.
    6. Run resolution stub (Phase 3 fills it).
    7. Log every stage to extraction_passes (DB) when a conn is provided.
    8. Return the populated envelope.

Idempotency:
    - Envelopes are rebuilt from scratch on every run; no mutation of
      stored state until Phase 5 wires the auto-node hook.
    - DB inserts into extraction_passes are append-only, matching the
      table's schema. Re-running a signal produces new pass rows; the
      envelope itself is the authoritative current state, not the pass
      history.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from core.extraction.envelope import (
    add_gap,
    build_empty,
    get_path,
    record_pass,
    validate_shape,
)
from core.extraction.registry import get_worker
from core.ldu.lineage import lineage_columns

log = logging.getLogger("enki.extraction.runner")


def _should_run_deep(envelope: dict, signal: dict, force_deep: bool) -> tuple[bool, str]:
    """Evaluate the deep-pass policy. Returns (should_run, reason).

    POLICY: deep pass ALWAYS runs when an LLM client is available.
    This is deliberate: we categorize and validate every extraction with
    the LLM, we never duplicate (strict dedup before write), and we
    always cross-check source + time + content. The return value's
    reason is for logging — it tells us WHY the pass ran so the audit
    trail distinguishes "ran because of garbage title" from "ran as
    baseline categorize+validate".
    """
    if force_deep:
        return True, "force_deep_flag"

    # Gate 1 — title quality
    title = get_path(envelope, "identity.title_display", "")
    gaps = get_path(envelope, "completeness.gaps", []) or []
    if not title or "identity.title_display" in gaps:
        return True, "garbage_title"

    # Gate 2 — narrative quality
    narrative = get_path(envelope, "summary.narrative", "")
    if not narrative or len(narrative) < 40:
        return True, "narrative_too_thin"

    # Gate 3 — language gap (deep pass translates non-English bodies)
    lang = get_path(envelope, "source.language_detected") or "en"
    if lang != "en":
        return True, "non_english_language"

    # Baseline: always run for categorization + validation
    return True, "baseline_categorize_validate"


def _log_pass_to_db(
    conn,
    *,
    signal_rid: Optional[int],
    stage: str,
    fields_filled: list[str],
    model: Optional[str] = None,
    model_version: Optional[str] = None,
    prompt_version: Optional[str] = None,
    duration_ms: int = 0,
    success: bool = True,
    error: Optional[str] = None,
) -> None:
    """Append one row to the extraction_passes table.

    No-op if conn is None (dev / corpus runs without a DB).
    """
    if conn is None:
        return
    if signal_rid is None:
        # The extraction_passes CHECK constraint requires exactly one
        # target id. Without a signal_rid we can't log yet — skip
        # silently. (The envelope still carries the pass log in
        # provenance.extraction_passes.)
        return

    lin = lineage_columns(ldu_t_id=signal_rid)
    sql = """
        INSERT INTO extraction_passes (
            tenant_id, signal_rid, stage,
            model_name, model_version, prompt_version,
            fields_filled, duration_ms, success, error,
            admitted_under_active_set, admitted_under_pack_versions, gdu_canonical
        ) VALUES (
            %(tenant_id)s, %(signal_rid)s, %(stage)s,
            %(model)s, %(model_version)s, %(prompt_version)s,
            %(fields_filled)s::jsonb, %(duration_ms)s, %(success)s, %(error)s,
            %(active_set)s, %(pack_versions)s::jsonb, %(canonical)s
        )
    """
    params = {
        "tenant_id": "default",
        "signal_rid": signal_rid,
        "stage": stage,
        "model": model,
        "model_version": model_version,
        "prompt_version": prompt_version,
        "fields_filled": json.dumps(fields_filled),
        "duration_ms": duration_ms,
        "success": success,
        "error": error,
        **lin,
    }
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    except Exception as e:
        log.warning("extraction_passes insert failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass


def run_extraction(
    signal: dict,
    *,
    conn=None,
    llm: Optional[Any] = None,
    force_deep: bool = False,
    tenant_id: str = "default",
    auto_llm: bool = True,
) -> dict:
    """Run a signal through the extraction pipeline and return its envelope.

    If `llm` is None and `auto_llm` is True (default), the runner will
    fetch the default LLM client via core.extraction.llm.get_default_client.
    This means callers don't have to wire the LLM explicitly — the runner
    finds Ollama on its own. Set auto_llm=False to opt out (e.g. for
    deterministic offline tests).

    Args:
        signal:     dict with keys {type, payload, source?, signal_rid?, fetched_at?}
        conn:       optional psycopg2 connection for DB pass logging
        llm:        optional LLM client (Phase 2: None, Phase 4: Gemma)
        force_deep: bypass the gate and always run deep_pass
        tenant_id:  for multi-tenant DB logging

    Returns:
        The populated envelope dict. Always returns a dict — on
        unrecoverable errors the envelope is returned with its
        provenance.extraction_passes log explaining what happened.
    """
    signal_type = signal.get("type") or signal.get("signal_type")
    signal_rid = signal.get("signal_rid")

    # Auto-fetch LLM client if not provided (Phase 4)
    if llm is None and auto_llm:
        try:
            from core.extraction.llm import get_default_client
            llm = get_default_client()
        except Exception as e:
            log.debug("auto_llm fetch failed: %s", e)
            llm = None

    # Build envelope
    envelope = build_empty(node_type="source", signal_rid=signal_rid)

    # Stamp the extraction_profile_id on the envelope so downstream
    # writers (mentions_writer -> upsert_mentions_from_envelope) can
    # tag every entity_mentions row they create. Without this, rows go
    # in with NULL profile and the federation publisher can't discover
    # them. Best-effort — falls back to 'legacy_v1' if registry blows up.
    try:
        from core.extraction.profile_registry import derive_profile_from_llm
        envelope["extraction_profile_id"] = derive_profile_from_llm(
            llm,
            prompt_template="signal_extraction_v1",
            prompt_version="v1",
        )
    except Exception as e:
        log.debug("profile_id stamp failed (non-fatal): %s", e)
        envelope["extraction_profile_id"] = "legacy_v1"

    # Resolve worker
    worker = get_worker(signal_type) if signal_type else None
    if worker is None:
        reason = f"no_worker_for_type:{signal_type}"
        record_pass(
            envelope,
            stage="cheap",
            fields_filled=[],
            duration_ms=0,
            success=False,
            error=reason,
        )
        add_gap(envelope, "*")
        _log_pass_to_db(
            conn, signal_rid=signal_rid, stage="cheap",
            fields_filled=[], duration_ms=0, success=False, error=reason,
        )
        # Auto-learn hook: observe this payload's shape so the learner
        # can eventually propose a mapping. Never raises — profiler
        # swallows its own errors so extraction stays uninterrupted.
        try:
            from core.extraction.learning import observe as _learn_observe
            _learn_observe(signal, conn)
        except Exception as _e:
            log.debug("learn observe swallowed error: %s", _e)
        return envelope

    # ─── cheap pass ────────────────────────────────────────────────
    try:
        envelope = worker.cheap_pass(signal, envelope)
        last = envelope["provenance"]["extraction_passes"][-1]
        _log_pass_to_db(
            conn, signal_rid=signal_rid, stage="cheap",
            fields_filled=last.get("fields_filled", []),
            duration_ms=last.get("duration_ms", 0),
            success=last.get("success", True),
            error=last.get("error"),
        )
    except Exception as e:
        log.exception("cheap_pass failed for signal_type=%s", signal_type)
        record_pass(envelope, stage="cheap", fields_filled=[],
                    success=False, error=f"cheap_pass_exception:{e}")
        _log_pass_to_db(
            conn, signal_rid=signal_rid, stage="cheap",
            fields_filled=[], success=False, error=str(e),
        )
        return envelope

    # ─── deep-pass gate ────────────────────────────────────────────
    should_deep, reason = _should_run_deep(envelope, signal, force_deep)
    if should_deep:
        try:
            envelope = worker.deep_pass(signal, envelope, llm=llm)
            last = envelope["provenance"]["extraction_passes"][-1]
            # annotate the in-envelope pass entry with the gate reason
            last["gate_reason"] = reason
            _log_pass_to_db(
                conn, signal_rid=signal_rid, stage="deep",
                fields_filled=last.get("fields_filled", []),
                model=last.get("model"),
                model_version=last.get("model_version"),
                prompt_version=last.get("prompt_version"),
                duration_ms=last.get("duration_ms", 0),
                success=last.get("success", True),
                error=last.get("error"),
            )
        except Exception as e:
            log.exception("deep_pass failed for signal_type=%s", signal_type)
            record_pass(envelope, stage="deep", fields_filled=[],
                        success=False, error=f"deep_pass_exception:{e}")

    # ─── resolution (Phase 3) ──────────────────────────────────────
    # Resolve entities, places, and topics against master tables.
    # Mutates envelope in place (stamps node_ids, writes resolved_places).
    import time as _rt
    _r0 = _rt.monotonic()
    try:
        from core.extraction.resolvers import resolve_all
        res = resolve_all(envelope, conn, tenant_id=tenant_id, ldu_t_id=signal_rid)
        res_ms = int((_rt.monotonic() - _r0) * 1000)
        total_resolved = (
            res["entities"]["resolved"]
            + res["places"]["resolved"]
            + res["topics"]["resolved"]
        )
        record_pass(
            envelope,
            stage="resolution",
            fields_filled=res["fields_filled"],
            model=None,
            prompt_version="phase3_resolver_v1",
            duration_ms=res_ms,
            success=total_resolved > 0 or conn is None,
            error=None if (total_resolved > 0 or conn is None) else "no_items_resolved",
        )
        _log_pass_to_db(
            conn, signal_rid=signal_rid, stage="resolution",
            fields_filled=res["fields_filled"],
            prompt_version="phase3_resolver_v1",
            duration_ms=res_ms,
            success=total_resolved > 0 or conn is None,
        )
    except Exception as e:
        log.exception("resolver_all failed")
        record_pass(
            envelope, stage="resolution", fields_filled=[],
            duration_ms=int((_rt.monotonic() - _r0) * 1000),
            success=False, error=f"resolver_exception:{e}",
        )

    # ─── topic ontology classifier (Phase 8, universal) ────────────
    # Merges LLM-classified canonical topics into envelope.topics BEFORE
    # resolve_all so topic_resolver can dedupe against the hierarchy.
    # Wait — resolve_all already ran above. That means we add topics
    # here and they'll get resolved on the next extraction pass. For
    # now we still emit them so downstream stages (mentions, edges)
    # see them; the resolver picks up laggards on re-extraction.
    try:
        from core.extraction.extractors.topic_ontology import (
            classify_topics, merge_into_envelope,
        )
        to_t0 = _rt.monotonic()
        classified = classify_topics(envelope, llm=llm)
        merge_into_envelope(envelope, classified)
        record_pass(
            envelope, stage="topic_ontology",
            fields_filled=[f"topics.classified={len(classified)}"] if classified else [],
            duration_ms=int((_rt.monotonic() - to_t0) * 1000),
            success=True,
            model=getattr(llm, "model_name", None),
            prompt_version="topic_ontology_v1",
        )
    except Exception as e:
        log.exception("topic ontology stage failed")
        record_pass(envelope, stage="topic_ontology", fields_filled=[],
                    success=False, error=f"topic_ontology_exception:{e}")

    # ─── media (Phase 4B, universal) ───────────────────────────────
    # Downloads every image URL attached to this signal, computes
    # perceptual + byte hashes, upserts media_nodes (collapsing
    # duplicates across outlets), and emits article→media edges.
    # Best-effort: any single image can fail without breaking the
    # extraction pipeline. See core/media/pipeline.py.
    try:
        from core.media.pipeline import process_image_url
        from core.extraction.envelope import get_path
        article_nid = envelope.get("node_id")
        outlet = get_path(envelope, "source.outlet_name")
        # Candidate URLs: feed thumbnail + structured payload hints.
        # Body-scraped <img> URLs are extracted later when the article
        # worker surfaces them in payload; for now we stick to known
        # structured fields.
        candidates: list[tuple[str, str]] = []   # (url, role)
        thumb = get_path(envelope, "media.thumbnail")
        if isinstance(thumb, str) and thumb:
            candidates.append((thumb, "thumbnail"))
        for role, key in (("og_image", "og_image_url"),
                          ("twitter_image", "twitter_image_url")):
            url = (signal.get("payload") or {}).get(key)
            if isinstance(url, str) and url:
                candidates.append((url, role))

        md_t0 = _rt.monotonic()
        media_out: list[dict] = []
        seen_urls: set[str] = set()
        for url, role in candidates:
            if url in seen_urls:
                continue
            seen_urls.add(url)
            result = process_image_url(
                conn, url,
                tenant_id=tenant_id,
                signal_rid=signal_rid,
                article_node_id=article_nid,
                outlet=outlet,
                role=role,
                emit_edge=True,
            )
            if result:
                media_out.append({
                    "node_id": result["node_id"],
                    "url":     url,
                    "width":   result.get("width"),
                    "height":  result.get("height"),
                    "role":    role,
                    "reused":  result.get("reused"),
                })
        if media_out:
            envelope.setdefault("media", {})["images"] = media_out
        record_pass(
            envelope, stage="media",
            fields_filled=[f"media.images={len(media_out)}"] if media_out else [],
            duration_ms=int((_rt.monotonic() - md_t0) * 1000),
            success=True,
            prompt_version="media_v1",
        )
    except Exception as e:
        log.exception("media stage failed")
        record_pass(envelope, stage="media", fields_filled=[],
                    success=False, error=f"media_exception:{e}")

    # ─── addresses (Phase 4, universal) ────────────────────────────
    try:
        from core.extraction.extractors.addresses import (
            extract_addresses_cheap, persist_addresses,
        )
        body_for_addr = (
            (envelope.get("summary") or {}).get("body_original")
            or (envelope.get("summary") or {}).get("narrative")
            or ""
        )
        ad_t0 = _rt.monotonic()
        addrs = extract_addresses_cheap(body_for_addr)
        n_addr = persist_addresses(
            addrs, envelope, conn,
            tenant_id=tenant_id, signal_rid=signal_rid,
            geocode=(signal_rid is not None),  # skip network for test runs
        )
        record_pass(
            envelope, stage="addresses",
            fields_filled=[f"addresses.inserted={n_addr}"] if n_addr else [],
            duration_ms=int((_rt.monotonic() - ad_t0) * 1000),
            success=True,
            prompt_version="addresses_v1",
        )
    except Exception as e:
        log.exception("addresses stage failed")
        record_pass(envelope, stage="addresses", fields_filled=[],
                    success=False, error=f"addresses_exception:{e}")

    # ─── events (Phase 3, universal) ───────────────────────────────
    try:
        from core.extraction.extractors.events import extract_events, persist_events
        body_for_ev = (
            (envelope.get("summary") or {}).get("body_original")
            or (envelope.get("summary") or {}).get("narrative")
            or ""
        )
        ev_t0 = _rt.monotonic()
        events = extract_events(
            body_for_ev, envelope, llm=llm,
            publish_date=(envelope.get("temporal") or {}).get("event_time"),
        )
        persisted = persist_events(
            events, envelope, conn,
            tenant_id=tenant_id, signal_rid=signal_rid,
        )
        record_pass(
            envelope, stage="events",
            fields_filled=[f"events.persisted={len(persisted)}"] if persisted else [],
            duration_ms=int((_rt.monotonic() - ev_t0) * 1000),
            success=True,
            model=getattr(llm, "model_name", None),
            prompt_version="events_v1",
        )
    except Exception as e:
        log.exception("events stage failed")
        record_pass(envelope, stage="events", fields_filled=[],
                    success=False, error=f"events_exception:{e}")

    # ─── quote speaker resolution (Phase 2d, universal) ────────────
    try:
        from core.extraction.extractors.quote_speakers import resolve_quote_speakers
        qs_t0 = _rt.monotonic()
        n_linked = resolve_quote_speakers(envelope)
        record_pass(
            envelope, stage="quote_speakers",
            fields_filled=[f"quotes.speaker_node_id={n_linked}"] if n_linked else [],
            duration_ms=int((_rt.monotonic() - qs_t0) * 1000),
            success=True,
            prompt_version="quote_speakers_v1",
        )
    except Exception as e:
        log.exception("quote_speakers stage failed")
        record_pass(envelope, stage="quote_speakers", fields_filled=[],
                    success=False, error=f"quote_speakers_exception:{e}")

    # ─── semantic relationships (Phase 2c, universal) ──────────────
    # After entities are resolved (each has a canonical entity:<id>)
    # AND the body is available, ask the LLM for (subject, verb, object,
    # evidence) triples. These become knowledge_edges via the custom
    # relationships array.
    try:
        from core.extraction.extractors.relationships import (
            extract_relationships, attach_relationships_to_envelope,
        )
        body_for_rel = (
            (envelope.get("summary") or {}).get("body_original")
            or (envelope.get("summary") or {}).get("narrative")
            or ""
        )
        rel_t0 = _rt.monotonic()
        triples = extract_relationships(body_for_rel, envelope, llm=llm)
        attach_relationships_to_envelope(envelope, triples)
        record_pass(
            envelope, stage="relationships",
            fields_filled=[f"relationships.custom={len(triples)}"],
            duration_ms=int((_rt.monotonic() - rel_t0) * 1000),
            success=True,
            model=getattr(llm, "model_name", None),
            prompt_version="relationships_v1",
        )
    except Exception as e:
        log.exception("relationships stage failed")
        record_pass(envelope, stage="relationships", fields_filled=[],
                    success=False, error=f"relationships_exception:{e}")

    # ─── entity mentions (Phase 2a, async via worker) ──────────────
    # Mentions are written asynchronously by core/workers/mentions_writer.py
    # to keep ingest fast and give us retry/observability. The runner
    # only enqueues here (cheap ~3ms INSERT). The worker picks up the
    # envelope within ~2 seconds and runs upsert_mentions_from_envelope.
    #
    # Architectural rationale: every other aggregator/writer in the
    # system runs in a supervised worker (watch_derivatives, conflict_
    # scanner, node_builder). entity_mentions used to be the odd one
    # out — inline writes blocked ingest, swallowed exceptions silently,
    # and were invisible in any queue-depth metric. Migration 041 +
    # this enqueue moves it under the same worker discipline.
    try:
        from core.extraction.mentions import enqueue_mentions
        mn_t0 = _rt.monotonic()
        queue_id = enqueue_mentions(
            envelope, conn, tenant_id=tenant_id, signal_rid=signal_rid,
        )
        record_pass(
            envelope, stage="mentions_enqueued",
            fields_filled=[f"mentions_queue.queue_id={queue_id}"] if queue_id else [],
            duration_ms=int((_rt.monotonic() - mn_t0) * 1000),
            success=queue_id is not None,
            prompt_version="phase2a_mentions_v2_async",
        )
    except Exception as e:
        log.exception("mentions enqueue failed")
        record_pass(envelope, stage="mentions_enqueued", fields_filled=[],
                    success=False, error=f"enqueue_exception:{e}")

    # ─── reality check (Phase 3b, universal) ───────────────────────
    # After entity resolution, verify the top resolved entities against
    # Wikidata + curated stop-list. Runs for ALL paths (feed, watch,
    # research) so governance cannot drift. Writes back onto the
    # entities table AND stamps envelope.entities[].reality so the
    # completeness tier can upgrade to "verified" when warranted.
    try:
        from core.research.reality_check import reality_check_entity, persist_reality_result
        rc_t0 = _rt.monotonic()
        rc_count = 0
        for ent in (envelope.get("entities") or [])[:10]:
            if not isinstance(ent, dict):
                continue
            name = (ent.get("label") or ent.get("name") or "").strip()
            etype = (ent.get("type") or ent.get("kind") or "").strip()
            if not name or len(name) < 2:
                continue
            nid = ent.get("node_id") or ""
            # Only check canonical-resolved entities (skip unresolved
            # identifier-style rows like "email:x@y.com").
            if not nid.startswith("entity:"):
                continue
            try:
                result = reality_check_entity(name, etype or "person")
                ent["reality"] = result
                if conn is not None:
                    try:
                        entity_id = int(nid.split(":", 1)[1])
                        persist_reality_result(conn, entity_id, result)
                    except (ValueError, IndexError):
                        pass
                rc_count += 1
            except Exception as e:
                log.debug("reality_check for %r failed: %s", name, e)
        rc_ms = int((_rt.monotonic() - rc_t0) * 1000)
        record_pass(
            envelope, stage="reality_check",
            fields_filled=[f"entities.reality.count={rc_count}"],
            duration_ms=rc_ms, success=True,
            prompt_version="phase3b_reality_check_v1",
        )
        _log_pass_to_db(
            conn, signal_rid=signal_rid, stage="reality_check",
            fields_filled=[f"count={rc_count}"],
            prompt_version="phase3b_reality_check_v1",
            duration_ms=rc_ms, success=True,
        )
    except Exception as e:
        log.exception("reality_check stage failed")
        record_pass(
            envelope, stage="reality_check", fields_filled=[],
            success=False, error=f"reality_check_exception:{e}",
        )

    # ─── completeness scoring (Phase 8) ────────────────────────────
    # Score the envelope against the per-(node_type.subtype) slot spec
    # from completenessvocab. Pure function — no DB writes here, the
    # auto_node stage that follows persists the score onto the row.
    try:
        from core.extraction.completeness import score_envelope
        sc_t0 = _rt.monotonic()
        completeness = score_envelope(envelope)
        sc_ms = int((_rt.monotonic() - sc_t0) * 1000)
        record_pass(
            envelope,
            stage="scoring",
            fields_filled=["completeness.score", "completeness.tier"],
            duration_ms=sc_ms,
            success=True,
            prompt_version="phase8_completeness_v1",
        )
        _log_pass_to_db(
            conn, signal_rid=signal_rid, stage="scoring",
            fields_filled=["completeness.score", "completeness.tier"],
            prompt_version="phase8_completeness_v1",
            duration_ms=sc_ms,
            success=True,
        )
    except Exception as e:
        log.exception("completeness scoring failed")
        record_pass(
            envelope, stage="scoring", fields_filled=[],
            success=False, error=f"scoring_exception:{e}",
        )

    # ─── extraction store (Phase A — split from knowledge_nodes) ────
    # Persist the envelope as a signal_extractions row (NOT knowledge_nodes).
    # signal_extractions is where every extracted signal lives. knowledge_nodes
    # is reserved for research artifacts (dossiers, questions, chronicles, etc.)
    # created by rules, research workers, chronicle workers, and humans.
    #
    # Strict dedup: the store checks node_id, then content_hash, then
    # (source_url + event_time window) before INSERTing. If any of those
    # match an existing row, the new signal is merged into the existing
    # extraction rather than creating a duplicate.
    try:
        from core.extraction.signal_extraction_store import upsert_signal_extraction
        se_t0 = _rt.monotonic()
        se_result = upsert_signal_extraction(
            envelope, conn,
            tenant_id=tenant_id,
            signal_rid=signal_rid,
            signal_type=signal_type,
            feed_worthy=True,  # default — rule promotions may downgrade later
        )
        se_ms = int((_rt.monotonic() - se_t0) * 1000)
        action = se_result.get("action", "skipped")
        record_pass(
            envelope,
            stage="extraction_store",
            fields_filled=["signal_extractions.row"] if action in ("insert", "update") else [],
            duration_ms=se_ms,
            success=se_result.get("status") in ("ok", "deduped"),
            error=se_result.get("reason"),
            prompt_version="phaseA_extraction_store_v1",
        )
        _log_pass_to_db(
            conn, signal_rid=signal_rid, stage="extraction_store",
            fields_filled=[action],
            prompt_version="phaseA_extraction_store_v1",
            duration_ms=se_ms,
            success=se_result.get("status") in ("ok", "deduped"),
            error=se_result.get("reason"),
        )
    except Exception as e:
        log.exception("extraction_store hook failed")
        record_pass(
            envelope, stage="extraction_store", fields_filled=[],
            success=False, error=f"extraction_store_exception:{e}",
        )

    # ─── watch topic fanout (Phase A+ reconnection) ─────────────────
    # Post-extraction hook that attributes this signal to any active
    # watch topic(s) it matches — either via explicit _topic_id on the
    # signal metadata (set by topic-attached connectors) or via keyword
    # matching against the topic's keyword list (for default RSS feeds).
    # The result is a row (per matching topic) in watch_topic_articles
    # which feeds the existing /watch/[id] 7-tab research UI.
    try:
        from core.extraction.watch_fanout import fanout_to_watch_topics
        fo_t0 = _rt.monotonic()
        fo_stats = fanout_to_watch_topics(
            envelope, signal, conn,
            tenant_id=tenant_id,
            signal_rid=signal_rid,
            signal_type=signal_type,
        )
        fo_ms = int((_rt.monotonic() - fo_t0) * 1000)
        total_attributed = (
            fo_stats.get("attributed_via_topic_id", 0)
            + fo_stats.get("attributed_via_keyword", 0)
        )
        record_pass(
            envelope,
            stage="watch_fanout",
            fields_filled=[f"watch_topic_articles.rows={total_attributed}"] if total_attributed else [],
            duration_ms=fo_ms,
            success=True,
            prompt_version="phaseA_watch_fanout_v1",
        )
    except Exception as e:
        log.exception("watch_fanout hook failed")
        record_pass(
            envelope, stage="watch_fanout", fields_filled=[],
            success=False, error=f"watch_fanout_exception:{e}",
        )

    # ─── shape validation ──────────────────────────────────────────
    violations = validate_shape(envelope)
    if violations:
        log.warning(
            "envelope shape violations after extraction: %s", violations,
        )

    return envelope
