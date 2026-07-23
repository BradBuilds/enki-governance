"""
Entity Investigation Worker — deep-dives public data sources for entity profiles.

Runs as a daemon thread. Picks up entity_investigations with status='pending'
and dispatches to source-specific handlers (SEC EDGAR, FEC, USAspending,
ProPublica, CourtListener, FDIC).

Each source runs as a sub-task within the investigation. Findings are stored
in investigation_findings. Discovered entities are resolved against the
existing entity graph and optionally queued for their own investigation.

Worker pattern matches existing workers (feed_enrichment, node_builder, etc.):
  - start_worker() / stop_worker() / get_status()
  - Daemon thread with exponential backoff
  - Batch processing with configurable size
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection
from core.workers.stuck_running_reaper import reap_stuck_rows


def _finding_content_hash(source: str, title: str, data: dict) -> str:
    """Stable SHA1 over a finding's identity-defining fields.

    Re-runs that produce the same (source, title, data) hash get deduped
    via migration 120's uniq_finding_content partial unique index.
    """
    try:
        canon = json.dumps(data or {}, sort_keys=True, default=str)
    except Exception:
        canon = ""
    payload = f"{source}|{title or ''}|{canon}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()

log = logging.getLogger("enki.workers.investigation")

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "investigations_completed": 0,
    "findings_stored": 0,
    "entities_discovered": 0,
    "errors": 0,
}

import os

CHECK_INTERVAL = 15   # seconds between checks when idle
BATCH_SIZE = 1        # ONE investigation per tick — trickle, never a burst
SOURCE_DELAY = 1.0    # seconds between source queries (rate limit protection)
REAP_EVERY_N_TICKS = 40   # ~10 min at CHECK_INTERVAL=15s

# Paced execution: after each investigation, wait this long before the next so the
# queue drains SLOWLY (never hammering external APIs or the GPU). Env-tunable.
PACE_SECONDS = max(15, int(os.getenv("ENKI_INVESTIGATION_PACE_SECONDS", "60") or 60))
# Yield the GPU under pressure: pause when the 5-min load avg exceeds this.
LOAD_PAUSE = float(os.getenv("ENKI_INVESTIGATION_LOAD_PAUSE", "10") or 10)


def _should_pause() -> bool:
    """Yield the GPU / back off under pressure so collapse + interactive work get
    priority: pause when the worker manager has disabled this tier, Ollama is
    unhealthy, or the box is loaded. Investigation resumes when pressure clears —
    this is how the GPU passes back and forth."""
    try:
        from core.workers.manager import is_manager_disabled
        if is_manager_disabled("investigation"):
            return True
    except Exception:
        pass
    try:
        from core.workers.supervisor import _OLLAMA_HEALTH
        if _OLLAMA_HEALTH.get("healthy") is False:
            return True
    except Exception:
        pass
    try:
        if os.getloadavg()[1] > LOAD_PAUSE:
            return True
    except Exception:
        pass
    return False


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_worker_loop, args=(tenant_id,),
        name="investigation-worker", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    _STATUS["phase"] = "starting"
    reap_stuck_rows("entity_investigations", tenant_id=tenant_id)
    log.info("Investigation worker started")
    return {"status": "started"}


def stop_worker():
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=10)
    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"
    return {"status": "stopped"}


def _worker_loop(tenant_id: str):
    _STATUS["running"] = True
    backoff = CHECK_INTERVAL
    tick = 0

    while not _STOP_EVENT.is_set():
        try:
            tick += 1
            if tick % REAP_EVERY_N_TICKS == 0:
                reap_stuck_rows("entity_investigations", tenant_id=tenant_id)
            if _should_pause():
                # Under pressure — hand the GPU to collapse / interactive work.
                _STATUS["phase"] = "paused — yielding GPU (pressure)"
                backoff = PACE_SECONDS
            else:
                processed = _process_batch(tenant_id)
                if processed > 0:
                    # Trickle: wait PACE_SECONDS before the next one, so the queue
                    # drains slowly and never hits API/GPU limits.
                    backoff = PACE_SECONDS
                    _STATUS["phase"] = f"processing (paced {PACE_SECONDS}s/run)"
                else:
                    backoff = min(backoff + 15, 180)
                    _STATUS["phase"] = "idle — no pending investigations"
        except Exception as e:
            log.warning("Investigation worker error: %s", e)
            _STATUS["errors"] += 1
            _STATUS["phase"] = f"error: {str(e)[:60]}"
            backoff = min(backoff * 2, 300)

        for _ in range(int(backoff)):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)

    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"


def _process_batch(tenant_id: str) -> int:
    """Find and process pending investigations."""
    investigations = _find_pending(tenant_id, BATCH_SIZE)
    if not investigations:
        return 0

    processed = 0
    for inv in investigations:
        try:
            _run_investigation(inv, tenant_id)
            processed += 1
            _STATUS["investigations_completed"] += 1
        except Exception as e:
            log.warning("Investigation %d failed: %s", inv["investigation_id"], e)
            _mark_failed(inv["investigation_id"], str(e))
            _STATUS["errors"] += 1

    _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
    return processed


def _find_pending(tenant_id: str, limit: int) -> list:
    """Find pending investigation requests."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT investigation_id, entity_id, seed_name, seed_type,
                           sources_requested, COALESCE(depth, 0)
                    FROM entity_investigations
                    WHERE tenant_id = %s AND status = 'pending'
                    ORDER BY priority ASC, created_at ASC
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                """, (tenant_id, limit))
                rows = cur.fetchall()

                results = []
                for r in rows:
                    # Mark as running
                    cur.execute("""
                        UPDATE entity_investigations
                        SET status = 'running', started_at = NOW()
                        WHERE investigation_id = %s
                    """, (r[0],))
                    results.append({
                        "investigation_id": r[0],
                        "entity_id": r[1],
                        "seed_name": r[2],
                        "seed_type": r[3] or "person",
                        "sources_requested": r[4] or [],
                        "depth": r[5] or 0,
                    })
                conn.commit()
                return results
    except Exception as e:
        log.debug("Failed to find pending investigations: %s", e)
        return []


def _run_investigation(inv: dict, tenant_id: str):
    """Execute a full investigation across all requested sources.

    June 4 2026: source-by-jurisdiction routing. Wikipedia and Wikidata
    always run FIRST (universal sources, no jurisdiction gating). Their
    findings are then scanned for a country_of_citizenship signal which
    we use to filter remaining sources via
    `core.investigation.sources.source_applies_to_country` — skipping
    e.g. FEC for a UK person so the dossier doesn't fill with
    confidently-wrong matches of unrelated US people sharing the name.
    """
    from core.investigation.sources import ALL_SOURCES, source_applies_to_country
    from core.research.credentials import set_tenant_context

    # Scope credential lookups to this tenant for the duration of the run.
    # Source handlers call get_credential("DATA_GOV_API_KEY") etc. which read
    # the contextvar to know which tenant's secrets to consult.
    set_tenant_context(tenant_id)

    inv_id = inv["investigation_id"]
    name = inv["seed_name"]
    entity_type = inv["seed_type"]
    requested = inv["sources_requested"] or list(ALL_SOURCES.keys())

    # Reorder so Wikipedia/Wikidata run first — their output drives the
    # jurisdiction routing for everything else.
    priority_order = ["wikipedia", "wikidata"]
    head = [s for s in priority_order if s in requested]
    tail = [s for s in requested if s not in priority_order]
    requested = head + tail

    sources_completed = []
    total_findings = 0
    total_discovered = 0
    detected_country: Optional[str] = None
    sources_skipped_by_jurisdiction: list[str] = []

    for source_name in requested:
        if _STOP_EVENT.is_set():
            break

        handler = ALL_SOURCES.get(source_name)
        if not handler:
            continue

        # Jurisdiction gate. detected_country starts as None (no filtering);
        # after Wikipedia/Wikidata run we have the entity's nationality and
        # can drop non-applicable sources.
        if not source_applies_to_country(source_name, detected_country):
            log.info(
                "Investigation %d: SKIP %s (country=%s, source jurisdictions don't match)",
                inv_id, source_name, detected_country,
            )
            sources_skipped_by_jurisdiction.append(source_name)
            continue

        try:
            _STATUS["phase"] = f"investigating {name} via {source_name}"
            log.info("Investigation %d: querying %s for '%s'", inv_id, source_name, name)

            result = handler(name, entity_type)

            # Sniff country_of_citizenship out of Wikipedia/Wikidata
            # findings so subsequent sources can be filtered. We look in
            # multiple field shapes because the bridges vary.
            if detected_country is None and source_name in ("wikipedia", "wikidata"):
                detected_country = _sniff_country_from_findings(result.get("findings", []))
                if detected_country:
                    log.info(
                        "Investigation %d: detected country=%s from %s — will filter remaining sources",
                        inv_id, detected_country, source_name,
                    )

            # Store findings (with GDU admission + edge to parent entity)
            findings_count = _store_findings(
                inv_id, tenant_id, source_name,
                result.get("findings", []),
                parent_entity_id=inv.get("entity_id"),
            )
            total_findings += findings_count
            _STATUS["findings_stored"] += findings_count

            # Store discovered entities. Structured sources (SEC/FEC/Wikidata
            # property crawl) populate result["discovered_entities"] directly;
            # encyclopedic sources (Wikipedia/DDG) leave it empty and we
            # NER-mine their summary text instead so the dossier doesn't
            # report "0 entities discovered" when the article clearly
            # mentions co-founders, employers, family.
            handler_discovered = result.get("discovered_entities", []) or []
            mined_discovered = _mine_findings_for_entities(
                source_name, result.get("findings", []), name,
            )
            if mined_discovered:
                log.info(
                    "Investigation %d: NER-mined %d entities from %s summary",
                    inv_id, len(mined_discovered), source_name,
                )
            discovered_count = _store_discovered(
                inv_id, tenant_id, source_name,
                handler_discovered + mined_discovered,
                inv.get("entity_id"),
            )
            total_discovered += discovered_count
            _STATUS["entities_discovered"] += discovered_count

            if result.get("status") != "error":
                sources_completed.append(source_name)

            # Rate limit between sources
            time.sleep(SOURCE_DELAY)

        except Exception as e:
            log.warning("Source %s failed for investigation %d: %s", source_name, inv_id, e)

    # Mark investigation complete
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE entity_investigations
                SET status = 'complete',
                    sources_completed = %s,
                    findings_count = %s,
                    entities_discovered = %s,
                    completed_at = NOW()
                WHERE investigation_id = %s
            """, (sources_completed, total_findings, total_discovered, inv_id))

            # Persist detected jurisdiction onto the entity envelope so
            # future investigations + other workers can use it without
            # re-running Wikipedia. The pack-level _source_scope filter
            # in core/research/source_routing.py reads this back.
            if detected_country and inv.get("entity_id"):
                cur.execute("""
                    UPDATE entities
                       SET envelope = COALESCE(envelope, '{}'::jsonb)
                                      || jsonb_build_object('iso_country_code', %s),
                           updated_at = NOW()
                     WHERE entity_id = %s
                       AND (envelope ->> 'iso_country_code') IS DISTINCT FROM %s
                """, (detected_country, inv["entity_id"], detected_country))
                if cur.rowcount:
                    log.info(
                        "Investigation %d: persisted iso_country_code=%s to entity_id=%s envelope",
                        inv_id, detected_country, inv["entity_id"],
                    )
        conn.commit()

    log.info("Investigation %d complete: %d findings, %d entities from %s",
             inv_id, total_findings, total_discovered, sources_completed)

    # Geocode addresses found in this investigation
    try:
        from core.workers.geocoder import geocode_investigation_findings
        _STATUS["phase"] = f"geocoding findings for investigation {inv_id}"
        geocode_investigation_findings(inv_id, tenant_id)
    except Exception as e:
        log.debug("Post-investigation geocoding failed: %s", e)

    # Cascade: auto-queue discovered entities for deeper investigation
    _cascade_discovered_entities(inv_id, tenant_id, inv.get("depth", 0))

    # Refresh the collapse views (migration 047) so the dossier endpoint
    # sees the new findings/discovered without waiting for a periodic job.
    # CONCURRENTLY = non-blocking, requires the UNIQUE indexes from the
    # migration. Best-effort — failure here doesn't roll back the
    # investigation itself.
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY investigation_finding_collapsed")
                cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY investigation_discovered_collapsed")
            conn.commit()
    except Exception as e:
        log.warning("Collapse view refresh failed for investigation %d: %s", inv_id, e)

    # Propagate the Wikipedia bio extract to entities.description so the
    # dossier renders a real narrative, not 'No description yet'. The
    # Wikipedia source's `extract` field is a 1-3 paragraph plain-text
    # summary — perfect for a 'description' slot. Without this step the
    # extract sat in investigation_findings.data and nothing ever surfaced
    # it. Also flip enrichment_status off 'pending' so the badge clears.
    if inv.get("entity_id"):
        try:
            _promote_bio_to_description(inv["entity_id"], inv_id, tenant_id)
        except Exception as e:
            log.warning("Bio promotion failed for investigation %d: %s", inv_id, e)


def _promote_bio_to_description(entity_id: int, inv_id: int, tenant_id: str):
    """Copy the just-finished Wikipedia extract into entity_kb_cache so
    the dossier's 'About' section renders a real bio paragraph instead
    of 'No description yet'. The entity API joins entity_kb_cache on
    LOWER(canonical_name) and reads kb.description for the dossier blurb.

    Idempotent: only writes when the cache row has no description (or
    didn't exist) so we don't clobber operator edits.

    Also bumps entities.last_researched_at so the 'pending' badge clears.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Pull the Wikipedia finding (any finding_type — sources vary
            # in how they label the bio blob; we'll inspect data fields).
            cur.execute(
                """
                SELECT data, finding_type
                  FROM investigation_findings
                 WHERE investigation_id = %s
                   AND tenant_id = %s
                   AND source = 'wikipedia'
                 ORDER BY confidence DESC NULLS LAST
                 LIMIT 5
                """,
                (inv_id, tenant_id),
            )
            rows = cur.fetchall()
            extract: Optional[str] = None
            thumbnail: Optional[str] = None
            page_url: Optional[str] = None
            wikidata_id: Optional[str] = None
            for data, _ftype in rows:
                if not isinstance(data, dict):
                    continue
                if not extract:
                    # core/investigation/sources.py:investigate_wikipedia stores
                    # the long bio paragraph at `data.summary` (not 'extract')
                    # because it renames the field in the bridge. `data.description`
                    # is the SHORT 1-liner (e.g. 'British activist'); we prefer
                    # the long paragraph and fall back to it only if missing.
                    cand = data.get("summary") or data.get("extract") or data.get("description")
                    if isinstance(cand, str) and len(cand.strip()) >= 20:
                        extract = cand
                if not thumbnail:
                    thumbnail = data.get("thumbnail") or data.get("thumbnail_url")
                if not page_url:
                    page_url = data.get("page_url") or data.get("url")
                if not wikidata_id:
                    wikidata_id = data.get("wikidata_id")
            if not extract:
                return

            # Get the entity's canonical name to use as the kb_cache key.
            cur.execute(
                "SELECT canonical_name, canonical_type FROM entities WHERE entity_id = %s",
                (entity_id,),
            )
            row = cur.fetchone()
            if not row:
                return
            canonical_name, canonical_type = row
            if not canonical_name:
                return
            normalized = canonical_name.strip().lower()
            kind = "entity" if (canonical_type or "person") not in ("place", "city", "country", "region") else "place"

            # UPSERT into entity_kb_cache. Only overwrite description if
            # the row's existing description is null/empty.
            cur.execute(
                """
                INSERT INTO entity_kb_cache (
                    tenant_id, normalized_name, kind,
                    canonical_name, canonical_type,
                    source, source_id, source_url,
                    description, thumbnail_url,
                    confidence, hit_count, first_seen_at, last_hit_at, last_verified_at
                )
                VALUES (
                    %s, %s, %s,
                    %s, %s,
                    'wikipedia', %s, %s,
                    %s, %s,
                    0.85, 1, NOW(), NOW(), NOW()
                )
                ON CONFLICT (tenant_id, normalized_name, kind) DO UPDATE
                   SET description    = COALESCE(NULLIF(entity_kb_cache.description, ''), EXCLUDED.description),
                       thumbnail_url  = COALESCE(entity_kb_cache.thumbnail_url, EXCLUDED.thumbnail_url),
                       source_url     = COALESCE(entity_kb_cache.source_url, EXCLUDED.source_url),
                       last_hit_at    = NOW(),
                       last_verified_at = NOW()
                """,
                (
                    tenant_id, normalized, kind,
                    canonical_name, canonical_type or "person",
                    wikidata_id, page_url,
                    extract[:2000], thumbnail,
                ),
            )

            # Mark research complete so the 'pending' badge clears.
            cur.execute(
                """
                UPDATE entities
                   SET last_researched_at = NOW()
                 WHERE entity_id = %s
                """,
                (entity_id,),
            )
        conn.commit()


def _store_findings(inv_id: int, tenant_id: str, source: str, findings: list,
                    parent_entity_id: Optional[int] = None) -> int:
    """Store investigation findings in the database AND admit through GDU.

    Each finding is stored in investigation_findings (raw archive) and also
    admitted through the GDU gateway as an assessment node — so findings
    appear in the knowledge graph with proper base-60 addresses and audit trail.

    June 4 disambiguation pass: each finding's title is scored against the
    investigation's seed name. Findings whose source-returned name shape
    looks like a different real-world entity (extra middle name, REALTY
    INC suffix, etc.) are still stored, but with a lowered confidence that
    the dossier UI uses to push them into a 'Possible matches' section.
    Findings below a hard cutoff (0.1) are dropped entirely to keep noise
    out. The seed name is fetched once per call.
    """
    if not findings:
        return 0

    # Pull the canonical seed name for disambiguation. Cheap one-row read;
    # the alternative was passing seed_name through every caller, which
    # would touch a lot of code.
    seed_name = _lookup_seed_name(inv_id)

    count = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            for f in findings:
                try:
                    raw_conf = float(f.get("confidence", 0.7))
                    match_score = _score_name_match(seed_name, f.get("title"))
                    # Final confidence = source's confidence × name-match score.
                    # If the source was sure but the name looks wrong, the
                    # finding ends up around 0.3-0.5 — visible in 'possible
                    # matches' but not promoted.
                    final_conf = round(raw_conf * match_score, 3)
                    if final_conf < 0.10:
                        # Almost certainly a different real-world entity.
                        # Drop it rather than poison the dossier.
                        log.debug(
                            "Dropping finding (name match %.2f): source=%s title=%r seed=%r",
                            match_score, source, f.get("title"), seed_name,
                        )
                        continue
                    title = (f.get("title") or "")[:500]
                    data = f.get("data") or {}
                    chash = _finding_content_hash(source, title, data)
                    cur.execute("""
                        INSERT INTO investigation_findings
                            (investigation_id, tenant_id, source, finding_type,
                             title, data, confidence, content_hash)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (tenant_id, source, content_hash)
                           WHERE content_hash IS NOT NULL
                           DO NOTHING
                        RETURNING finding_id
                    """, (
                        inv_id, tenant_id, source,
                        f.get("type", "unknown"),
                        title,
                        json.dumps(data),
                        final_conf,
                        chash,
                    ))
                    if cur.fetchone():
                        count += 1
                except Exception as e:
                    log.debug("Failed to store finding: %s", e)
        conn.commit()

    # Admit key findings through GDU as assessment nodes
    _admit_findings_to_gdu(inv_id, tenant_id, source, findings, parent_entity_id)

    return count


# Sources whose findings carry summary/extract text worth NER-mining
# for named entities. We don't mine structured-data findings (SEC officer
# lists, FEC donations) because those already populate discovered_entities
# directly with typed relationships.
_NER_MINEABLE_SOURCES = {"wikipedia", "wikidata", "duckduckgo", "loc",
                          "google_books", "wayback"}

# How many chars of summary text to pass to NER per finding. Caps LLM cost.
_NER_MAX_TEXT_CHARS = 1500


def _mine_findings_for_entities(
    source: str,
    findings: list,
    seed_name: str,
) -> list[dict]:
    """Run NER over summary/extract text in findings to surface named
    entities mentioned in encyclopedic content. Returns discovered-entity
    dicts in the same shape source handlers produce.

    Bridges that hand-populate discovered_entities (SEC, FEC, Wikidata
    property crawl) are not double-mined — their structured output is
    higher quality than NER over a paragraph.
    """
    if source not in _NER_MINEABLE_SOURCES or not findings:
        return []

    # Gather text from summary/extract/description fields across all
    # findings into a single corpus for one NER call (cheaper than
    # per-finding calls).
    chunks: list[str] = []
    for f in findings:
        data = f.get("data") or {}
        if not isinstance(data, dict):
            continue
        for key in ("summary", "extract", "description", "abstract"):
            v = data.get(key)
            if isinstance(v, str) and v.strip():
                chunks.append(v.strip())
    if not chunks:
        return []

    text = " ".join(chunks)[:_NER_MAX_TEXT_CHARS]
    if not text:
        return []

    try:
        from core.extraction.llm.factory import get_default_client
        from core.extraction.extractors.entity_extractor import extract_entities_deep
    except Exception as e:
        log.debug("NER mining: extractor import failed: %s", e)
        return []

    llm = get_default_client()
    if llm is None:
        log.debug("NER mining: LLM unavailable, skipping")
        return []

    try:
        raw = extract_entities_deep(text, llm=llm)
    except Exception as e:
        log.debug("NER mining: extract_entities_deep failed: %s", e)
        return []

    seed_lower = (seed_name or "").lower()
    out: list[dict] = []
    seen: set[str] = set()
    for e in raw or []:
        if not isinstance(e, dict):
            continue
        name = (e.get("name_raw") or e.get("name") or "").strip()
        if not name or name.lower() in seen:
            continue
        # Drop self-references — the seed entity isn't a discovered entity.
        if seed_lower and name.lower() == seed_lower:
            continue
        seen.add(name.lower())

        ident = e.get("identifier_type") or ""
        # Map identifier_type back to a discovery entity_type guess.
        if "person" in ident:
            etype = "person"
        elif "place" in ident or "country" in ident or "city" in ident:
            etype = "place"
        elif "org" in ident or "company" in ident:
            etype = "organization"
        else:
            etype = "unknown"

        out.append({
            "name": name,
            "type": etype,
            "relationship": "mentioned_in_summary",
            "detail": {
                "ner_source": source,
                "ner_confidence": float(e.get("confidence") or 0.5),
                "ner_identifier_type": ident,
            },
        })
    return out


def _sniff_country_from_findings(findings: list) -> Optional[str]:
    """Extract an ISO 3166 alpha-2 country code from Wikipedia/Wikidata
    finding payloads, used by _run_investigation to route subsequent
    source calls by jurisdiction.

    Looks for the most reliable signals first (Wikidata
    country_of_citizenship, country_of_origin), falling back to
    nationality strings in Wikipedia summaries via a small name→ISO map.
    Returns None when we can't determine the country — caller treats
    that as 'no filtering, run all sources'.
    """
    if not findings:
        return None

    # First pass — explicit ISO/country fields (Wikidata bridge).
    for f in findings:
        data = f.get("data") or {}
        if not isinstance(data, dict):
            continue
        for key in ("country_of_citizenship_code", "country_code", "iso_country"):
            v = data.get(key)
            if isinstance(v, str) and 2 <= len(v.strip()) <= 3:
                return v.strip().upper()[:2]
        # Wikidata sometimes uses verbose country names — map to ISO.
        for key in ("country_of_citizenship", "country_of_origin", "nationality", "country"):
            v = data.get(key)
            if isinstance(v, str) and v:
                iso = _country_name_to_iso(v)
                if iso:
                    return iso

    # Second pass — scrape from Wikipedia summary text. Patterns like
    # "is a British", "is an American", "British political activist".
    for f in findings:
        data = f.get("data") or {}
        if not isinstance(data, dict):
            continue
        summary = data.get("summary") or data.get("description") or ""
        if not isinstance(summary, str):
            continue
        for keyword, iso in _NATIONALITY_KEYWORDS:
            if keyword in summary[:500].lower():
                return iso
    return None


# Lowercase nationality / country-name → ISO 3166 alpha-2 mapping.
# Order matters: more-specific matches first to avoid false positives
# (e.g. 'British' before 'British Indian Ocean Territory').
_NATIONALITY_KEYWORDS: list[tuple[str, str]] = [
    ("british", "GB"), ("english", "GB"), ("scottish", "GB"), ("welsh", "GB"),
    ("united kingdom", "GB"), ("uk-", "GB"),
    ("irish", "IE"), ("ireland", "IE"),
    ("american", "US"), ("united states", "US"), ("u.s.-", "US"),
    ("canadian", "CA"), ("canada", "CA"),
    ("french", "FR"), ("france", "FR"),
    ("german", "DE"), ("germany", "DE"),
    ("italian", "IT"), ("italy", "IT"),
    ("spanish", "ES"), ("spain", "ES"),
    ("dutch", "NL"), ("netherlands", "NL"),
    ("belgian", "BE"), ("belgium", "BE"),
    ("swiss", "CH"), ("switzerland", "CH"),
    ("austrian", "AT"), ("austria", "AT"),
    ("swedish", "SE"), ("sweden", "SE"),
    ("norwegian", "NO"), ("norway", "NO"),
    ("danish", "DK"), ("denmark", "DK"),
    ("finnish", "FI"), ("finland", "FI"),
    ("polish", "PL"), ("poland", "PL"),
    ("russian", "RU"), ("russia", "RU"),
    ("ukrainian", "UA"), ("ukraine", "UA"),
    ("israeli", "IL"), ("israel", "IL"),
    ("japanese", "JP"), ("japan", "JP"),
    ("chinese", "CN"), ("china", "CN"),
    ("indian", "IN"), ("india", "IN"),
    ("australian", "AU"), ("australia", "AU"),
    ("new zealand", "NZ"),
    ("brazilian", "BR"), ("brazil", "BR"),
    ("mexican", "MX"), ("mexico", "MX"),
    ("south african", "ZA"), ("south africa", "ZA"),
]


def _country_name_to_iso(name: str) -> Optional[str]:
    """Map a single 'United Kingdom' / 'British' style string to ISO code."""
    if not name:
        return None
    n = name.strip().lower()
    for keyword, iso in _NATIONALITY_KEYWORDS:
        if keyword in n:
            return iso
    return None


def _lookup_seed_name(inv_id: int) -> str:
    """One-shot lookup of an investigation's seed_name. Empty string on
    error so callers can still proceed with raw confidence."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT seed_name FROM entity_investigations WHERE investigation_id = %s",
                    (inv_id,),
                )
                row = cur.fetchone()
                return (row[0] or "") if row else ""
    except Exception:
        return ""


# Common "this is a different entity" tokens that show up in source-returned
# names. If a candidate name has any of these AND the seed name does not,
# treat as a strong negative signal — the source likely returned a different
# real-world entity (a company, a relative, a different person sharing the
# surname). Add liberally; false positives here just lower confidence on a
# row that's still stored, never dropped outright unless ALSO failing the
# token-overlap test.
_DIFFERENT_ENTITY_TOKENS = {
    # Company / org suffixes
    "inc", "incorporated", "llc", "ltd", "limited", "plc", "co", "corp",
    "corporation", "realty", "holdings", "group", "trust", "foundation",
    "associates", "partners", "lp", "llp", "pllc", "international",
    "services", "consulting", "enterprises", "industries", "estate",
    # Generational / familial qualifiers
    "jr", "sr", "ii", "iii", "iv", "junior", "senior",
    # Common middle-name red flags (more reliable than first names)
    # — kept empty; middle name handled separately below
}


def _score_name_match(seed_name: str, candidate_title: str) -> float:
    """Return a 0-1 score for 'is this finding's named subject the same
    real-world entity as the investigation seed name'.

    The candidate_title is the finding's title field; sources prefix it
    (e.g. 'Wikipedia: Sam Rowan', 'FEC Candidate: ROWAN, SAM
    FRANKLIN'). We strip the source label off, then compare tokens.

    Score breakdown:
      1.0      exact match (case-insensitive, normalized)
      0.7-0.95 substring or token-superset (Wikipedia returns 'Sam
               Rowan' for seed 'Sam Rowan' → high)
      0.3-0.5  partial token overlap, candidate has extra tokens that
               look like a company suffix / middle name / etc.
      0.1-0.2  some shared surname but mostly different name shape
      0.0      no shared tokens
    """
    if not seed_name or not candidate_title:
        return 0.7  # No seed/title to compare → assume source's own confidence

    def _norm(s: str) -> list[str]:
        # Strip a leading "Source: " or "Source Candidate: " prefix; the
        # source label always comes before a colon in our finding titles.
        if ":" in s:
            s = s.split(":", 1)[1]
        # Strip parenthesized qualifiers like '(musician)' or '(Q12345)'
        import re
        s = re.sub(r"\([^)]*\)", " ", s)
        # Lowercase, strip non-alnum, split.
        s = re.sub(r"[^a-z0-9\s]", " ", s.lower())
        return [t for t in s.split() if t]

    seed_tokens = _norm(seed_name)
    cand_tokens = _norm(candidate_title)
    if not seed_tokens or not cand_tokens:
        return 0.4

    seed_set = set(seed_tokens)
    cand_set = set(cand_tokens)

    # Exact equality after normalization — same name, same order, same length.
    if seed_tokens == cand_tokens:
        return 1.0

    # Candidate is a strict superset of seed → either same person with more
    # detail OR a different entity with seed-as-substring (REALTY INC after
    # 'Sam Rowan'). The DIFFERENT_ENTITY_TOKENS check resolves.
    if seed_set <= cand_set:
        extras = cand_set - seed_set
        if extras & _DIFFERENT_ENTITY_TOKENS:
            return 0.30  # 'SAM ROWAN REALTY INC' — almost certainly different
        # Probably a fuller version of the same name (middle name added,
        # full title etc.). Bias toward match but not full credit since
        # we can't be sure.
        # Heuristic: if the extras are ALL likely-name tokens (capitalized
        # words, not in our suffix list), it's probably a middle name —
        # which IS a meaningful disambiguation signal (e.g. 'SAM FRANKLIN
        # ROWAN' is a different person than 'Sam Rowan').
        if all(len(e) > 1 for e in extras):
            return 0.45  # Middle-name divergence
        return 0.70

    # Candidate is a strict subset (returned only surname). Mild penalty —
    # could be the same person, but we don't have enough to confirm.
    if cand_set <= seed_set:
        # Single shared token (just a surname): weak signal.
        if len(cand_set) == 1:
            return 0.30
        return 0.55

    # Partial overlap.
    overlap = seed_set & cand_set
    if not overlap:
        return 0.05
    jaccard = len(overlap) / max(1, len(seed_set | cand_set))
    return round(0.2 + 0.6 * jaccard, 2)


def _admit_findings_to_gdu(inv_id: int, tenant_id: str, source: str,
                           findings: list, parent_entity_id: Optional[int] = None):
    """Route investigation findings through the GDU gateway.

    Creates assessment nodes for important findings (company registrations,
    financials, court cases) so they appear in the knowledge graph.
    Links each finding back to the investigated entity via 'investigated' edge.
    """
    import os
    if not os.environ.get("ENKI_USE_GDU_GATEWAY", "").lower() in ("true", "1", "yes"):
        return  # Legacy mode — skip GDU routing

    try:
        from core.gdu.gateway import get_gateway
        gateway = get_gateway()
    except Exception:
        return  # GDU not available

    parent_node_id = f"entity:{parent_entity_id}" if parent_entity_id else None

    # Defense-in-depth seed-name check: even if a source's investigator
    # missed its fetch-time filter, don't let an assessment node land in
    # the graph that doesn't actually concern the parent entity.
    # Identifier-anchored sources (Wikipedia, Wikidata) are exempt
    # because they may return findings using the entity's real name
    # (Simon Lowe vs Sam Rowan).
    parent_seed_name: Optional[str] = None
    if parent_entity_id and source not in ("wikipedia", "wikidata"):
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT canonical_name FROM entities WHERE entity_id = %s",
                        (parent_entity_id,),
                    )
                    r = cur.fetchone()
                    if r:
                        parent_seed_name = r[0]
        except Exception:
            pass

    for f in findings:
        ftype = f.get("type", "")
        title = f.get("title", "")[:200]

        # Only admit substantive findings as assessment nodes (not summaries)
        if ftype in ("court_summary",):
            continue
        if len(title) < 10:
            continue

        # Seed-name validation: title or finding data must mention the
        # parent entity name (or a token thereof). Catches fuzzy-search
        # leaks past fetch-time filters.
        if parent_seed_name:
            from core.investigation.sources import _text_mentions_seed
            haystack = title + " " + json.dumps(f.get("data", {}))[:500]
            if not _text_mentions_seed(parent_seed_name, haystack,
                                        require_surname=False):
                continue

        try:
            collapse_key = f"inv:{source}:{title.lower().strip()[:150]}"
            result = gateway.admit_node(
                tenant_id, "assessment", "fact",
                title, json.dumps(f.get("data", {}))[:2000],
                signal_rid=0,  # no signal — investigation-sourced
                collapse_key=collapse_key,
                metadata={
                    "investigation_id": inv_id,
                    "source": source,
                    "confidence": f.get("confidence", 0.7),
                    "finding_type": ftype,
                },
            )

            # Create edge: investigated entity → finding node
            if result and result.node_id and parent_node_id:
                gateway.admit_edge(
                    tenant_id, parent_node_id, result.node_id,
                    "investigated",
                    f"investigation finding from {source}: {ftype}",
                    signal_rid=0,
                )

            # If finding has coordinates (EPA, geocoded), write to knowledge_node
            fdata = f.get("data", {})
            lat = fdata.get("latitude")
            lon = fdata.get("longitude")
            if result and result.node_id and lat and lon:
                try:
                    with get_connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute("""
                                UPDATE knowledge_nodes
                                SET latitude = %s, longitude = %s, updated_at = NOW()
                                WHERE node_id = %s AND latitude IS NULL
                            """, (float(lat), float(lon), result.node_id))
                        conn.commit()
                except Exception:
                    pass

        except Exception as e:
            log.debug("GDU admission failed for finding '%s': %s", title[:50], e)


# Relationships we trust enough to auto-admit a discovered entity to the GDU.
# Anything else (LOC's bare "associated", Google Books "author_of" with no
# disambiguation, generic substring matches) goes to the raw archive only —
# never to the entity graph. Phase A admission gate (May 7 2026).
TRUSTED_DISCOVERY_RELATIONSHIPS = {
    "officer_of",            # SEC Form 4 — structured XML, federal filing
    "director_of",           # SEC Form 4 — same
    "associated_company",    # SEC EDGAR — has CIK identifier
    "political_candidate",   # FEC — has candidate_id
    "treasurer_of",          # FEC — has committee_id
    "associated_committee",  # FEC — the committee itself, keyed by committee_id
    "donor_to",              # FEC — only large donors (>$5k threshold in source)
    "awarded_contract_from", # USAspending — has award_id
    "associated_nonprofit",  # ProPublica — has EIN
    "subsidiary_of",         # SEC parent/sub disclosure
    "parent_org",            # same
    "federal_contractor",    # SAM.gov — has UEI/CAGE
    "legislator",            # Congress.gov — has bioguide_id
}


def _store_discovered(inv_id: int, tenant_id: str, source: str,
                      discovered: list, parent_entity_id: Optional[int]) -> int:
    """Store discovered entities, admit through GDU, and create relationship edges.

    Two-track storage (Phase A admission gate):
      ALWAYS — Stored in investigation_discovered (raw archive — every match)
      GATED  — Admitted to GDU only if relationship is in
               TRUSTED_DISCOVERY_RELATIONSHIPS. This blocks LOC's bare
               "associated" contributors, Google Books substring authors, and
               other noise that previously polluted the entity graph.
    """
    if not discovered:
        return 0

    count = 0
    seen = set()  # dedup within this batch
    admitted_entities: dict[str, int] = {}  # name → entity_id from GDU
    blocked_count = 0

    with get_connection() as conn:
        with conn.cursor() as cur:
            for d in discovered:
                name = d.get("name", "").strip()
                if not name or name.lower() in seen:
                    continue
                seen.add(name.lower())

                relationship = d.get("relationship", "associated")
                trusted = relationship in TRUSTED_DISCOVERY_RELATIONSHIPS

                # Store in investigation_discovered (raw archive — always)
                entity_id_resolved = None
                try:
                    cur.execute("""
                        INSERT INTO investigation_discovered
                            (investigation_id, tenant_id, name, entity_type,
                             source, relationship, relationship_detail)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        RETURNING discovered_id
                    """, (
                        inv_id, tenant_id, name,
                        d.get("type", "unknown"),
                        source, relationship,
                        json.dumps(d.get("detail", {})),
                    ))
                    count += 1
                except Exception as e:
                    log.debug("Failed to store discovered entity: %s", e)
                    continue

                # Gate: only admit trusted relationships to the GDU
                if not trusted:
                    blocked_count += 1
                    continue

                # Admit through GDU gateway (creates real entity + knowledge node)
                entity_id_resolved = _admit_discovered_entity(
                    tenant_id, name, d.get("type", "organization"), d.get("detail", {}),
                )
                if entity_id_resolved:
                    admitted_entities[name] = entity_id_resolved
                    # Update the discovered record with resolved entity_id
                    try:
                        cur.execute("""
                            UPDATE investigation_discovered
                            SET entity_id = %s
                            WHERE investigation_id = %s AND tenant_id = %s
                              AND LOWER(name) = LOWER(%s) AND entity_id IS NULL
                        """, (entity_id_resolved, inv_id, tenant_id, name))
                    except Exception:
                        pass

        conn.commit()

    # Create relationship edges between parent and discovered entities.
    # Only iterate the entities we actually admitted to the GDU — _admit_discovered
    # was already gated on TRUSTED_DISCOVERY_RELATIONSHIPS, so admitted_entities
    # is the trusted subset.
    if parent_entity_id and admitted_entities:
        _create_investigation_edges(
            tenant_id, parent_entity_id, admitted_entities,
            [d for d in discovered if d.get("name", "").strip() in admitted_entities],
        )

    if blocked_count > 0:
        log.info(
            "Investigation %d: admission gate blocked %d/%d discovered entities "
            "(untrusted relationship — kept in archive)",
            inv_id, blocked_count, count,
        )

    return count


def _admit_discovered_entity(
    tenant_id: str, name: str, entity_type: str, detail: dict,
) -> Optional[int]:
    """Admit a discovered entity through the GDU gateway.

    Returns entity_id if admitted, None if gateway is off or admission fails.
    """
    import os
    if not os.environ.get("ENKI_USE_GDU_GATEWAY", "").lower() in ("true", "1", "yes"):
        # Legacy mode: resolve through entity_resolution directly
        try:
            from core.gdu.entity_resolution import resolve_entity
            result = resolve_entity(
                tenant_id=tenant_id, name=name,
                entity_type=entity_type,
                context="investigation_discovered",
                source_pipeline="investigation",
            )
            return result.entity_id if result else None
        except Exception as e:
            log.debug("Legacy entity resolution failed for %s: %s", name, e)
            return None

    try:
        from core.gdu.gateway import get_gateway
        gateway = get_gateway()

        # Route the investigation-discovered type through the LDU subtype
        # validator (entityvocab). This replaces a hand-maintained type_map
        # that silently fell back to "organization" for unknown types — a
        # known contamination vector that landed places/vehicles/etc. as
        # organizations. Now: accept → canonical alias, route → log+skip
        # (the cross-category target table writes its own rows), drop →
        # skip, unknown → skip (let LDU-P pick it up via admit_candidate's
        # own validation).
        from core.ldu.subtype_validator import validate_for_ldu
        v = validate_for_ldu(entity_type)
        if v.action == "accept":
            gdu_type = v.canonical_subtype or entity_type
        elif v.action == "route":
            log.debug(
                "investigation skipped %s: routes to %s/%s — not an entity",
                name, v.route_to[0], v.route_to[1],
            )
            return None
        elif v.action == "drop":
            log.debug("investigation dropped %s: %s", name, v.reason)
            return None
        else:
            # unknown — let admit_candidate decide (it may route to LDU-P)
            gdu_type = entity_type

        # Build identifiers from detail
        identifiers = {}
        for key in ("cik", "ein", "fec_id", "committee_id", "ticker", "cert",
                    "uei", "cage", "duns", "lei", "company_number", "qid",
                    "wikidata_id", "source_qid"):
            if detail.get(key):
                identifiers[key] = str(detail[key])
        # Collapse the three Wikidata-id spellings to one canonical `qid` so the
        # same QID under different source keys hashes to the same identity.
        for _alias in ("wikidata_id", "source_qid"):
            if _alias in identifiers:
                identifiers.setdefault("qid", identifiers.pop(_alias))
            identifiers.pop(_alias, None)

        # An ORGANIZATION identifier must never attach to a PERSON. Discovered
        # people carry the org's id in `detail` only as CONTEXT — a treasurer's
        # `committee_id` names the committee they serve, a director's `cik` names
        # the company. Extracting it as the person's own id makes the person
        # collapse INTO the org (a treasurer becomes "PAC TO DEFEAT DONALD J
        # VALE"). Strip org-only ids from person admissions; a person's valid
        # ids are qid / fec_id (candidate) / bioguide etc.
        _ORG_ONLY_IDS = ("committee_id", "cik", "ein", "uei", "cage", "duns",
                         "lei", "company_number", "ticker")
        _is_person = (entity_type or "").strip().lower() in ("person", "individual") \
            or (gdu_type or "").strip().lower() == "person"
        if _is_person:
            for _k in _ORG_ONLY_IDS:
                identifiers.pop(_k, None)

        # Phase 6 of the May 20 rebuild: investigation discoveries route
        # to LDU. Structured identifiers from SEC/FEC/court/contract feeds
        # are strong type evidence and make these candidates promotion-
        # ready quickly once signal_count thresholds are met.
        from core.ldu.admit_candidate import admit_candidate, DECISIVE_IDENTIFIER_KEYS
        type_evidence = {k: v for k, v in (identifiers or {}).items() if v} or None
        result = admit_candidate(
            surface_form=name,
            proposed_type=gdu_type,
            signal_rid=0,  # no signal — investigation-sourced
            tenant_id=tenant_id,
            type_evidence=type_evidence,
        )
        if not result:
            return None
        if result.entity_id:
            return result.entity_id

        # Identifier-decisive inline promotion. A discovered org carrying a
        # registry identifier (FEC committee_id, EIN, CIK, UEI, QID, …) is a
        # distinct real entity by that identifier alone — promote its concept
        # to a first-class entity NOW, keyed by identifier_hash, instead of
        # leaving it parked pending signal-count thresholds. This is what lets
        # the org become its own linkable node (edge to the investigated
        # entity) rather than a namesake alias. Runs even when the background
        # promoter worker is disabled (sealed collapse mode) because it is
        # deterministic and identifier-gated, not a bulk sweep.
        if result.concept_id and type_evidence and any(
            type_evidence.get(k) for k in DECISIVE_IDENTIFIER_KEYS
        ):
            try:
                from core.ldu.promote import promote_concept
                promo = promote_concept(result.concept_id)
                if promo and promo.get("entity_id"):
                    return int(promo["entity_id"])
            except Exception as e:
                log.debug("inline identifier promotion failed for %s: %s", name, e)
        return None

    except Exception as e:
        log.debug("GDU admission failed for discovered entity %s: %s", name, e)
        return None


def _create_investigation_edges(
    tenant_id: str, parent_entity_id: int,
    admitted: dict[str, int], discovered: list,
):
    """Create GDU edges between the investigated entity and discovered entities.

    Uses relationship types from edgevocab (officer_of, donor_to, contracted_with,
    filed_with, litigant_in, etc.).
    """
    import os
    use_gateway = os.environ.get("ENKI_USE_GDU_GATEWAY", "").lower() in ("true", "1", "yes")

    # Map investigation relationships to edgevocab edge types. Phase 8b.6
    # follow-up: expanded set so the OpenCorporates + Wikidata bridges'
    # richer ownership / family / corporate-structure edges survive into
    # entity_relationships with meaningful labels (instead of being flattened
    # to a single `co_occurs_with` fallback).
    rel_map = {
        # Existing — keep verbatim
        "associated_company": "filed_with",
        "political_candidate": "filed_with",
        "treasurer_of": "officer_of",
        "associated_committee": "filed_with",
        "awarded_contract_from": "contracted_with",
        "associated_nonprofit": "filed_with",
        "officer_of": "officer_of",
        "director_of": "director_of",
        "donor_to": "donor_to",
        # Ownership / corporate structure (Wikidata + OpenCorporates)
        "owns": "owns",
        "owned_by": "owned_by",
        "subsidiary_of": "subsidiary_of",
        "parent_org": "parent_of",
        "founded_by": "founded_by",
        "beneficial_owner_of": "beneficial_owner_of",
        "ceo_of": "ceo_of",
        "cfo_of": "officer_of",
        "coo_of": "officer_of",
        "cto_of": "officer_of",
        "president_of": "officer_of",
        "chairperson_of": "chairperson_of",
        "board_member_of": "director_of",
        "member_of": "member_of",
        # Family (Wikidata)
        "spouse_of": "spouse_of",
        "parent_of": "parent_of",
        "child_of": "child_of",
        "sibling_of": "sibling_of",
        # Place / asset (Wikidata)
        "headquartered_in": "located_at",
        "located_in": "located_at",
        "operated_by": "operated_by",
        "manufactured_by": "manufactured_by",
        "from_country": "from_country",
        # Asset registration (FAA bridge)
        "registered_to": "registered_to",
        # Wikidata structured-profile (single-value, existing emissions)
        "employed_by": "employed_by",
        "holds_position": "holds_position",
        "educated_at": "educated_at",
        "citizen_of": "citizen_of",
    }

    parent_node_id = f"entity:{parent_entity_id}"

    for d in discovered:
        name = d.get("name", "").strip()
        target_entity_id = admitted.get(name)
        if not target_entity_id or target_entity_id == parent_entity_id:
            continue

        inv_rel = d.get("relationship", "associated")
        edge_rel = rel_map.get(inv_rel, "co_occurs_with")
        target_node_id = f"entity:{target_entity_id}"

        if use_gateway:
            try:
                from core.gdu.gateway import get_gateway
                gateway = get_gateway()
                gateway.admit_edge(
                    tenant_id, parent_node_id, target_node_id,
                    edge_rel, f"investigation: {inv_rel}",
                    signal_rid=0,
                )
                # Also create the investigated→entity lineage edge
                gateway.admit_edge(
                    tenant_id, parent_node_id, target_node_id,
                    "investigated", f"discovered via investigation ({inv_rel})",
                    signal_rid=0,
                )
            except Exception as e:
                log.debug("GDU edge creation failed: %s", e)
        else:
            # Legacy: direct SQL
            try:
                with get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO entity_relationships
                                (tenant_id, source_entity_id, target_entity_id,
                                 relationship, confidence, source, evidence,
                                 is_current, article_count, created_at, updated_at)
                            VALUES (%s, %s, %s, %s, 0.85, 'investigation', %s,
                                    TRUE, 1, NOW(), NOW())
                            ON CONFLICT DO NOTHING
                        """, (tenant_id, parent_entity_id, target_entity_id,
                              edge_rel, f"investigation: {inv_rel}"))
                    conn.commit()
            except Exception as e:
                log.debug("Legacy edge creation failed: %s", e)


MAX_CASCADE_DEPTH = 3

# Relationship strings that justify a recursive cascade investigation.
# IMPORTANT: must match the actual values written into
# investigation_discovered.relationship by the source handlers — those
# carry an `_of` suffix (`director_of`, `officer_of`) that the original
# bare-noun list ("director", "officer") never matched. Result: cascade
# silently skipped every discovered entity for weeks.
#
# Conservative list — only cascade for relationships that point at
# canonical-graph-worth nodes:
#   - board members and named officers (people with structured filings)
#   - parent / subsidiary corporate structure (org-to-org backbone)
#   - beneficial ownership (Form 13G/D disclosures)
#
# Deliberately NOT cascading on:
#   - treasurer_of  → too noisy, mostly random PAC paperwork people
#   - associated_nonprofit → mostly weak (employee clubs, retirees assoc.)
#   - awarded_contract_from → recipients are already top-tier orgs
#                             (DoD/DoE/NASA) that get news-signal triggered
#   - associated_company → tautological (the SEC entity for the seed)
#   - political_candidate → the candidate IS the seed
CASCADE_RELATIONSHIP_TYPES = {
    # Corporate-officer / governance edges — investigating these surfaces the
    # rest of the corporate hierarchy that's worth its own graph node.
    "director_of",
    "officer_of",
    "ceo_of",
    "chairperson_of",
    "founded_by",
    # Corporate structure — Phase 8b.6 follow-up. Walking these gives us
    # the Vale → Vale Org → DV Holdings I LLC → N123AB chain.
    "subsidiary_of",
    "parent_org",
    "parent_org_of",
    "parent_of",
    "owns",
    "owned_by",
    "beneficial_owner_of",
    # Asset registration — investigating the asset-side surfaces operator,
    # manufacturer, base location, etc.
    "registered_to",
    "operated_by",
}


def _cascade_discovered_entities(inv_id: int, tenant_id: str, current_depth: int):
    """Auto-queue discovered entities for deeper investigation (max depth 3)."""
    if current_depth >= MAX_CASCADE_DEPTH:
        return

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT discovered_id, name, entity_type, relationship
                    FROM investigation_discovered
                    WHERE investigation_id = %s AND tenant_id = %s
                """, (inv_id, tenant_id))
                discovered = cur.fetchall()

                queued = 0
                for disc_id, name, entity_type, relationship in discovered:
                    if not name or not relationship:
                        continue
                    # Only cascade for high-value relationship types
                    if relationship.lower() not in CASCADE_RELATIONSHIP_TYPES:
                        continue

                    # Check cooldown: don't re-investigate within 24h
                    cur.execute("""
                        SELECT COUNT(*) FROM entity_investigations
                        WHERE tenant_id = %s AND seed_name = %s
                          AND started_at > NOW() - INTERVAL '24 hours'
                    """, (tenant_id, name))
                    if cur.fetchone()[0] > 0:
                        continue

                    # Queue new investigation at depth+1.
                    #
                    # sources_requested is text[] (PostgreSQL array). Passing
                    # NULL lets _run_investigation default to ALL_SOURCES via
                    #   `requested = inv["sources_requested"] or list(ALL_SOURCES.keys())`
                    # This was previously the literal string "all", which
                    # type-errored on every INSERT and was swallowed by the
                    # bare-except below — cascade silently no-op'd for weeks.
                    cur.execute("""
                        INSERT INTO entity_investigations
                            (tenant_id, seed_name, seed_type, status,
                             sources_requested, depth, discovered_from)
                        VALUES (%s, %s, %s, 'pending', NULL, %s, %s)
                        ON CONFLICT DO NOTHING
                    """, (
                        tenant_id, name, entity_type or "organization",
                        current_depth + 1, f"cascade from investigation {inv_id}",
                    ))
                    queued += 1

                if queued > 0:
                    conn.commit()
                    log.info("Cascaded %d new investigations from investigation %d (depth %d→%d)",
                             queued, inv_id, current_depth, current_depth + 1)
    except Exception as e:
        # Was log.debug — the silent-failure mode that hid the
        # sources_requested type bug for weeks. Surface at warning so
        # any future cascade breakage shows up in normal logs.
        log.warning("Investigation cascade failed for %d: %s", inv_id, e)


def _mark_failed(inv_id: int, error: str):
    """Mark an investigation as failed."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE entity_investigations
                    SET status = 'failed', error = %s, completed_at = NOW()
                    WHERE investigation_id = %s
                """, (error[:1000], inv_id))
            conn.commit()
    except Exception as e:
        log.debug("Failed to mark investigation failed: %s", e)


# ─── One-shot runner (for API trigger without daemon) ─────────────────────────

def run_investigation_sync(
    name: str,
    entity_type: str = "person",
    tenant_id: str = "default",
    sources: Optional[list[str]] = None,
    entity_id: Optional[int] = None,
) -> dict:
    """Run an investigation synchronously (blocking). Used by API endpoint.

    Creates the investigation record, runs all sources, stores findings,
    and returns the complete result.
    """
    from core.investigation.sources import ALL_SOURCES

    if sources is None:
        sources = list(ALL_SOURCES.keys())

    # Create investigation record. If a pending row already exists
    # for this entity (queued by the gateway or sweep), claim it
    # instead of inserting — migration 119's partial unique index
    # would otherwise reject our insert.
    #
    # When sync-claiming we preserve the stored sources_requested so
    # the jurisdiction-aware routing from the original queue path
    # survives. Only fall back to the caller's sources (typically
    # ALL_SOURCES) when the stored row has nothing to run.
    with get_connection() as conn:
        with conn.cursor() as cur:
            if entity_id is not None:
                cur.execute("""
                    UPDATE entity_investigations
                       SET status = 'running',
                           started_at = NOW(),
                           sources_requested = COALESCE(
                               NULLIF(sources_requested, ARRAY[]::text[]),
                               %s
                           )
                     WHERE tenant_id = %s AND entity_id = %s
                       AND status = 'pending'
                    RETURNING investigation_id, sources_requested
                """, (sources, tenant_id, entity_id))
                row = cur.fetchone()
                if row:
                    inv_id = row[0]
                    sources = row[1] or sources
                    conn.commit()
                    log.info("sync-claimed existing pending investigation %s for entity_id=%s, %d sources",
                             inv_id, entity_id, len(sources))
                else:
                    cur.execute("""
                        INSERT INTO entity_investigations
                            (tenant_id, entity_id, seed_name, seed_type,
                             sources_requested, status, started_at, discovered_from)
                        VALUES (%s, %s, %s, %s, %s, 'running', NOW(), 'sync_run')
                        ON CONFLICT (tenant_id, entity_id)
                           WHERE entity_id IS NOT NULL
                             AND status IN ('pending', 'running')
                           DO NOTHING
                        RETURNING investigation_id
                    """, (tenant_id, entity_id, name, entity_type, sources))
                    row = cur.fetchone()
                    if not row:
                        # Race: another writer beat us. Pick up theirs.
                        cur.execute("""
                            SELECT investigation_id FROM entity_investigations
                             WHERE tenant_id=%s AND entity_id=%s
                               AND status IN ('pending', 'running')
                             ORDER BY created_at DESC LIMIT 1
                        """, (tenant_id, entity_id))
                        row = cur.fetchone()
                    inv_id = row[0]
                    conn.commit()
            else:
                cur.execute("""
                    INSERT INTO entity_investigations
                        (tenant_id, entity_id, seed_name, seed_type,
                         sources_requested, status, started_at, discovered_from)
                    VALUES (%s, NULL, %s, %s, %s, 'running', NOW(), 'sync_run_by_name')
                    RETURNING investigation_id
                """, (tenant_id, name, entity_type, sources))
                inv_id = cur.fetchone()[0]
                conn.commit()

    # Run the investigation
    inv = {
        "investigation_id": inv_id,
        "entity_id": entity_id,
        "seed_name": name,
        "seed_type": entity_type,
        "sources_requested": sources,
    }

    try:
        _run_investigation(inv, tenant_id)
    except Exception as e:
        _mark_failed(inv_id, str(e))

    # Load and return results
    return load_investigation_results(inv_id, tenant_id)


def load_investigation_results(inv_id: int, tenant_id: str) -> dict:
    """Load full investigation results from DB."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Investigation header
            cur.execute("""
                SELECT investigation_id, entity_id, seed_name, seed_type,
                       status, sources_requested, sources_completed,
                       findings_count, entities_discovered,
                       started_at, completed_at, error
                FROM entity_investigations
                WHERE investigation_id = %s AND tenant_id = %s
            """, (inv_id, tenant_id))
            row = cur.fetchone()
            if not row:
                return {"error": "not_found"}

            investigation = {
                "investigation_id": row[0],
                "entity_id": row[1],
                "seed_name": row[2],
                "seed_type": row[3],
                "status": row[4],
                "sources_requested": row[5] or [],
                "sources_completed": row[6] or [],
                "findings_count": row[7],
                "entities_discovered": row[8],
                "started_at": row[9].isoformat() if row[9] else None,
                "completed_at": row[10].isoformat() if row[10] else None,
                "error": row[11],
            }

            # Findings grouped by source
            cur.execute("""
                SELECT finding_id, source, finding_type, title, data, confidence
                FROM investigation_findings
                WHERE investigation_id = %s AND tenant_id = %s
                ORDER BY source, confidence DESC
            """, (inv_id, tenant_id))

            findings_by_source: dict[str, list] = {}
            for f in cur.fetchall():
                source = f[1]
                if source not in findings_by_source:
                    findings_by_source[source] = []
                findings_by_source[source].append({
                    "finding_id": f[0],
                    "finding_type": f[2],
                    "title": f[3],
                    "data": f[4],
                    "confidence": f[5],
                })

            # Discovered entities
            cur.execute("""
                SELECT discovered_id, name, entity_type, source,
                       relationship, relationship_detail, entity_id
                FROM investigation_discovered
                WHERE investigation_id = %s AND tenant_id = %s
                ORDER BY source, name
            """, (inv_id, tenant_id))

            discovered = []
            for d in cur.fetchall():
                discovered.append({
                    "discovered_id": d[0],
                    "name": d[1],
                    "entity_type": d[2],
                    "source": d[3],
                    "relationship": d[4],
                    "relationship_detail": d[5],
                    "entity_id": d[6],
                })

    investigation["findings_by_source"] = findings_by_source
    investigation["discovered_entities"] = discovered
    return investigation
