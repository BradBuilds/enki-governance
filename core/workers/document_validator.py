"""
Document/Source Validator Worker — assigns editorial stance and source authority.

For every document node, looks up the source domain in the editorial stance
dictionary and assigns:
  - editorial_stance: independent | state_media | partisan | satire | etc.
  - reliability: high | medium | low | mixed
  - bias: left | center | right | varies
  - is_satire: bool (Onion, Babylon Bee, etc.) — never produces confirmed assessments
  - is_state_media: bool — multiple state media sources don't count as independent
  - is_aggregator: bool (HN, Reddit) — point to original source

This dramatically improves the assessment validator's corroboration logic:
  - 5 confirmed by 5 independent outlets = real corroboration
  - 5 "confirmed" by 5 state media outlets = 1 narrative, NOT independent
  - 5 satire sites "agreeing" = fiction, no corroboration value
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.document_validator")

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "validated": 0,
    "satire_flagged": 0,
    "state_media_flagged": 0,
    "errors": 0,
}

CHECK_INTERVAL = 30
BATCH_SIZE = 20  # docs are cheap (no API calls), can do larger batches


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_worker_loop, args=(tenant_id,),
        name="document-validator", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    log.info("Document validator worker started")
    return {"status": "started"}


def stop_worker():
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=10)
    _STATUS["running"] = False
    return {"status": "stopped"}


def _worker_loop(tenant_id: str):
    _STATUS["running"] = True
    backoff = CHECK_INTERVAL

    while not _STOP_EVENT.is_set():
        try:
            processed = _process_batch(tenant_id)
            if processed > 0:
                backoff = CHECK_INTERVAL
                _STATUS["phase"] = "processing"
            else:
                backoff = min(backoff + 30, 300)
                _STATUS["phase"] = "idle — no pending documents"
        except Exception as e:
            log.warning("Document validator error: %s", e)
            _STATUS["errors"] += 1
            _STATUS["phase"] = f"error: {str(e)[:60]}"
            backoff = min(backoff * 2, 600)

        for _ in range(int(backoff)):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)

    _STATUS["running"] = False
    _STATUS["phase"] = "stopped"


def _process_batch(tenant_id: str) -> int:
    nodes = _find_pending_documents(tenant_id, BATCH_SIZE)
    if not nodes:
        return 0

    processed = 0
    for node in nodes:
        try:
            _validate_document(node, tenant_id)
            processed += 1
        except Exception as e:
            log.debug("Document validation failed for %s: %s", node["node_id"], e)
            _STATUS["errors"] += 1

    _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
    return processed


def _find_pending_documents(tenant_id: str, limit: int) -> list:
    """Find document nodes that haven't been classified yet."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT node_id, title, subtype, envelope, signal_rid
                    FROM knowledge_nodes
                    WHERE tenant_id = %s
                      AND node_type = 'document'
                      AND (envelope->'validation'->>'status') IS DISTINCT FROM 'validated'
                      AND title IS NOT NULL
                    ORDER BY created_at DESC
                    LIMIT %s
                """, (tenant_id, limit))

                return [
                    {"node_id": r[0], "title": r[1], "subtype": r[2],
                     "envelope": r[3] or {}, "signal_rid": r[4]}
                    for r in cur.fetchall()
                ]
    except Exception as e:
        log.debug("Failed to find pending documents: %s", e)
        return []


def _validate_document(node: dict, tenant_id: str):
    """Look up the source URL in the editorial stance dictionary."""
    node_id = node["node_id"]
    envelope = node["envelope"] or {}
    signal_rid = node.get("signal_rid")

    # Get the source URL from the signal payload
    source_url = ""
    outlet_name = ""
    if signal_rid:
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT payload->>'url' as url,
                               payload->>'link' as link,
                               payload->>'source' as source,
                               payload->>'feed_title' as feed_title,
                               payload->'data'->>'url' as nested_url,
                               payload->'data'->>'link' as nested_link
                        FROM signals
                        WHERE rid = %s
                    """, (signal_rid,))
                    row = cur.fetchone()
                    if row:
                        source_url = row[0] or row[1] or row[4] or row[5] or ""
                        outlet_name = row[2] or row[3] or ""
        except Exception as e:
            log.debug("Failed to get signal URL for %s: %s", node_id, e)

    # Look up domain in stance dictionary
    domain = _extract_domain(source_url)
    stance_info = _lookup_stance(domain) or _lookup_stance_by_outlet(outlet_name)

    if not stance_info:
        # Unknown source — mark as unvalidated, leave for next pass
        stance_info = {
            "stance": "unknown",
            "reliability": "unverified",
            "bias": "unknown",
            "is_satire": False,
            "is_state_media": False,
            "is_aggregator": False,
        }

    # Update node envelope
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                envelope.setdefault("source", {})
                envelope["source"]["editorial_stance"] = stance_info["stance"]
                envelope["source"]["reliability"] = stance_info["reliability"]
                envelope["source"]["bias"] = stance_info["bias"]
                envelope["source"]["domain"] = domain
                envelope["source"]["url"] = source_url
                envelope["source"]["outlet_name"] = outlet_name

                envelope["validation"] = {
                    "status": "validated" if stance_info["stance"] != "unknown" else "unvalidated",
                    "source": "editorial_stance_dictionary",
                    "is_satire": stance_info["is_satire"],
                    "is_state_media": stance_info["is_state_media"],
                    "is_aggregator": stance_info["is_aggregator"],
                    "validated_at": datetime.now(timezone.utc).isoformat(),
                }

                cur.execute("""
                    UPDATE knowledge_nodes
                    SET envelope = %s,
                        enrichment_status = 'complete',
                        completeness_score = GREATEST(completeness_score, 0.5),
                        completeness_tier = CASE
                            WHEN completeness_tier IN ('stub', NULL) THEN 'partial'
                            ELSE completeness_tier
                        END,
                        updated_at = NOW()
                    WHERE node_id = %s AND tenant_id = %s
                """, (json.dumps(envelope), node_id, tenant_id))

            conn.commit()

        if stance_info["is_satire"]:
            _STATUS["satire_flagged"] += 1
            log.info("SATIRE flagged: %s (%s)", node["title"][:60], domain)
        if stance_info["is_state_media"]:
            _STATUS["state_media_flagged"] += 1
            log.info("STATE MEDIA flagged: %s (%s)", node["title"][:60], domain)

        _STATUS["validated"] += 1

    except Exception as e:
        log.warning("Failed to store document validation for %s: %s", node_id, e)
        _STATUS["errors"] += 1


def _extract_domain(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        # Strip www., m., mobile.
        host = re.sub(r"^(www|m|mobile)\.", "", host)
        return host
    except Exception:
        return ""


def _lookup_stance(domain: str) -> Optional[dict]:
    """Look up domain in the editorial stance dictionary."""
    if not domain:
        return None

    # Try exact match first
    if domain in EDITORIAL_STANCE:
        return EDITORIAL_STANCE[domain]

    # Try without subdomain (e.g., apnews.com matches news.apnews.com)
    parts = domain.split(".")
    if len(parts) > 2:
        base_domain = ".".join(parts[-2:])
        if base_domain in EDITORIAL_STANCE:
            return EDITORIAL_STANCE[base_domain]

    return None


def _lookup_stance_by_outlet(outlet: str) -> Optional[dict]:
    """Fallback: match by outlet name when URL isn't available."""
    if not outlet:
        return None
    name_lower = outlet.lower().strip()
    for key, stance in OUTLET_NAME_INDEX.items():
        if key in name_lower:
            return stance
    return None


# ─── Editorial Stance Dictionary ──────────────────────────────────────
# Curated classification of news sources by editorial stance, reliability,
# and political bias. This is the foundation for source authority validation.
#
# Categories:
#   stance: independent | state_media | partisan | satire | opinion |
#           official_government | aggregator | advocacy
#   reliability: high | medium | low | mixed | unverified
#   bias: left | center-left | center | center-right | right | varies | unknown
#   is_satire: True for satire sites (their content is NOT factual)
#   is_state_media: True for government-controlled media
#   is_aggregator: True for sites that link to other sources

EDITORIAL_STANCE: dict[str, dict] = {
    # ─── Major Independent News (high reliability) ───
    "reuters.com":         {"stance": "independent", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "apnews.com":          {"stance": "independent", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "bbc.com":             {"stance": "independent", "reliability": "high", "bias": "center-left", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "bbc.co.uk":           {"stance": "independent", "reliability": "high", "bias": "center-left", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "npr.org":             {"stance": "independent", "reliability": "high", "bias": "center-left", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "pbs.org":             {"stance": "independent", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "csmonitor.com":       {"stance": "independent", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "economist.com":       {"stance": "independent", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "ft.com":              {"stance": "independent", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "wsj.com":             {"stance": "independent", "reliability": "high", "bias": "center-right", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "bloomberg.com":       {"stance": "independent", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},

    # ─── US Major Outlets ───
    "nytimes.com":         {"stance": "independent", "reliability": "high", "bias": "center-left", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "washingtonpost.com":  {"stance": "independent", "reliability": "high", "bias": "center-left", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "cnn.com":             {"stance": "independent", "reliability": "medium", "bias": "left", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "foxnews.com":         {"stance": "partisan", "reliability": "mixed", "bias": "right", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "msnbc.com":           {"stance": "partisan", "reliability": "mixed", "bias": "left", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "usatoday.com":        {"stance": "independent", "reliability": "medium", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "abcnews.go.com":      {"stance": "independent", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "cbsnews.com":         {"stance": "independent", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "nbcnews.com":         {"stance": "independent", "reliability": "high", "bias": "center-left", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "axios.com":           {"stance": "independent", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "politico.com":        {"stance": "independent", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "thehill.com":         {"stance": "independent", "reliability": "medium", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},

    # ─── International ───
    "aljazeera.com":       {"stance": "independent", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "france24.com":        {"stance": "independent", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "dw.com":              {"stance": "independent", "reliability": "high", "bias": "center-left", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "theguardian.com":     {"stance": "independent", "reliability": "high", "bias": "center-left", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "telegraph.co.uk":     {"stance": "independent", "reliability": "high", "bias": "center-right", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "thetimes.co.uk":      {"stance": "independent", "reliability": "high", "bias": "center-right", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "lemonde.fr":          {"stance": "independent", "reliability": "high", "bias": "center-left", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "spiegel.de":          {"stance": "independent", "reliability": "high", "bias": "center-left", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "nhk.or.jp":           {"stance": "official_government", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "scmp.com":            {"stance": "independent", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "haaretz.com":         {"stance": "independent", "reliability": "high", "bias": "center-left", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "timesofisrael.com":   {"stance": "independent", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "jpost.com":           {"stance": "independent", "reliability": "medium", "bias": "center-right", "is_satire": False, "is_state_media": False, "is_aggregator": False},

    # ─── State Media (FLAGGED) ───
    "rt.com":              {"stance": "state_media", "reliability": "low", "bias": "varies", "is_satire": False, "is_state_media": True, "is_aggregator": False},
    "sputniknews.com":     {"stance": "state_media", "reliability": "low", "bias": "varies", "is_satire": False, "is_state_media": True, "is_aggregator": False},
    "tass.com":            {"stance": "state_media", "reliability": "low", "bias": "varies", "is_satire": False, "is_state_media": True, "is_aggregator": False},
    "xinhuanet.com":       {"stance": "state_media", "reliability": "low", "bias": "varies", "is_satire": False, "is_state_media": True, "is_aggregator": False},
    "globaltimes.cn":      {"stance": "state_media", "reliability": "low", "bias": "varies", "is_satire": False, "is_state_media": True, "is_aggregator": False},
    "cgtn.com":            {"stance": "state_media", "reliability": "low", "bias": "varies", "is_satire": False, "is_state_media": True, "is_aggregator": False},
    "presstv.ir":          {"stance": "state_media", "reliability": "low", "bias": "varies", "is_satire": False, "is_state_media": True, "is_aggregator": False},
    "tehrantimes.com":     {"stance": "state_media", "reliability": "low", "bias": "varies", "is_satire": False, "is_state_media": True, "is_aggregator": False},
    "kcna.kp":             {"stance": "state_media", "reliability": "low", "bias": "varies", "is_satire": False, "is_state_media": True, "is_aggregator": False},
    "saudigazette.com.sa": {"stance": "state_media", "reliability": "medium", "bias": "varies", "is_satire": False, "is_state_media": True, "is_aggregator": False},
    "wam.ae":              {"stance": "state_media", "reliability": "medium", "bias": "varies", "is_satire": False, "is_state_media": True, "is_aggregator": False},

    # ─── Satire (FLAGGED — never produces facts) ───
    "theonion.com":        {"stance": "satire", "reliability": "low", "bias": "varies", "is_satire": True, "is_state_media": False, "is_aggregator": False},
    "babylonbee.com":      {"stance": "satire", "reliability": "low", "bias": "right", "is_satire": True, "is_state_media": False, "is_aggregator": False},
    "clickhole.com":       {"stance": "satire", "reliability": "low", "bias": "varies", "is_satire": True, "is_state_media": False, "is_aggregator": False},
    "duffelblog.com":      {"stance": "satire", "reliability": "low", "bias": "varies", "is_satire": True, "is_state_media": False, "is_aggregator": False},
    "private-eye.co.uk":   {"stance": "satire", "reliability": "medium", "bias": "varies", "is_satire": True, "is_state_media": False, "is_aggregator": False},
    "newsthump.com":       {"stance": "satire", "reliability": "low", "bias": "varies", "is_satire": True, "is_state_media": False, "is_aggregator": False},
    "thedailymash.co.uk":  {"stance": "satire", "reliability": "low", "bias": "varies", "is_satire": True, "is_state_media": False, "is_aggregator": False},
    "waterfordwhispersnews.com": {"stance": "satire", "reliability": "low", "bias": "varies", "is_satire": True, "is_state_media": False, "is_aggregator": False},

    # ─── Aggregators ───
    "news.ycombinator.com": {"stance": "aggregator", "reliability": "mixed", "bias": "varies", "is_satire": False, "is_state_media": False, "is_aggregator": True},
    "ycombinator.com":      {"stance": "aggregator", "reliability": "mixed", "bias": "varies", "is_satire": False, "is_state_media": False, "is_aggregator": True},
    "reddit.com":           {"stance": "aggregator", "reliability": "mixed", "bias": "varies", "is_satire": False, "is_state_media": False, "is_aggregator": True},
    "drudgereport.com":     {"stance": "aggregator", "reliability": "mixed", "bias": "right", "is_satire": False, "is_state_media": False, "is_aggregator": True},
    "memeorandum.com":      {"stance": "aggregator", "reliability": "mixed", "bias": "varies", "is_satire": False, "is_state_media": False, "is_aggregator": True},
    "news.google.com":      {"stance": "aggregator", "reliability": "mixed", "bias": "varies", "is_satire": False, "is_state_media": False, "is_aggregator": True},
    "google.com":           {"stance": "aggregator", "reliability": "mixed", "bias": "varies", "is_satire": False, "is_state_media": False, "is_aggregator": True},
    "websmith.studio":      {"stance": "opinion", "reliability": "unverified", "bias": "unknown", "is_satire": False, "is_state_media": False, "is_aggregator": False},

    # ─── Tech / Specialty (high reliability for their domains) ───
    "krebsonsecurity.com": {"stance": "independent", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "arstechnica.com":     {"stance": "independent", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "wired.com":           {"stance": "independent", "reliability": "high", "bias": "center-left", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "techcrunch.com":      {"stance": "independent", "reliability": "medium", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "theverge.com":        {"stance": "independent", "reliability": "high", "bias": "center-left", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "404media.co":         {"stance": "independent", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "thehackernews.com":   {"stance": "independent", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "bleepingcomputer.com": {"stance": "independent", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "darkreading.com":     {"stance": "independent", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},

    # ─── Defense / Intelligence / Geopolitics ───
    "defensenews.com":     {"stance": "independent", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "janes.com":           {"stance": "independent", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "csis.org":            {"stance": "advocacy", "reliability": "high", "bias": "center-right", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "rand.org":            {"stance": "advocacy", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "iiss.org":            {"stance": "advocacy", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "isw.us":              {"stance": "advocacy", "reliability": "high", "bias": "center-right", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "understandingwar.org": {"stance": "advocacy", "reliability": "high", "bias": "center-right", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "warontherocks.com":   {"stance": "advocacy", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},

    # ─── Tabloid / Low Reliability ───
    "dailymail.co.uk":     {"stance": "partisan", "reliability": "mixed", "bias": "right", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "express.co.uk":       {"stance": "partisan", "reliability": "low", "bias": "right", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "thesun.co.uk":        {"stance": "partisan", "reliability": "low", "bias": "right", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "nypost.com":          {"stance": "partisan", "reliability": "mixed", "bias": "right", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "infowars.com":        {"stance": "partisan", "reliability": "low", "bias": "right", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "breitbart.com":       {"stance": "partisan", "reliability": "low", "bias": "right", "is_satire": False, "is_state_media": False, "is_aggregator": False},

    # ─── Official Government ───
    "whitehouse.gov":      {"stance": "official_government", "reliability": "high", "bias": "varies", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "state.gov":           {"stance": "official_government", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "defense.gov":         {"stance": "official_government", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "treasury.gov":        {"stance": "official_government", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "fbi.gov":             {"stance": "official_government", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "cisa.gov":            {"stance": "official_government", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "nist.gov":            {"stance": "official_government", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "europa.eu":           {"stance": "official_government", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "un.org":              {"stance": "official_government", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "iaea.org":            {"stance": "official_government", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
    "who.int":             {"stance": "official_government", "reliability": "high", "bias": "center", "is_satire": False, "is_state_media": False, "is_aggregator": False},
}


# Index by outlet name keywords for fallback lookup when URL isn't available
OUTLET_NAME_INDEX: dict[str, dict] = {
    "reuters":      EDITORIAL_STANCE["reuters.com"],
    "ap news":      EDITORIAL_STANCE["apnews.com"],
    "bbc":          EDITORIAL_STANCE["bbc.com"],
    "the onion":    EDITORIAL_STANCE["theonion.com"],
    "babylon bee":  EDITORIAL_STANCE["babylonbee.com"],
    "rt news":      EDITORIAL_STANCE["rt.com"],
    "russia today": EDITORIAL_STANCE["rt.com"],
    "sputnik":      EDITORIAL_STANCE["sputniknews.com"],
    "tass":         EDITORIAL_STANCE["tass.com"],
    "xinhua":       EDITORIAL_STANCE["xinhuanet.com"],
    "global times": EDITORIAL_STANCE["globaltimes.cn"],
    "cgtn":         EDITORIAL_STANCE["cgtn.com"],
    "press tv":     EDITORIAL_STANCE["presstv.ir"],
    "al jazeera":   EDITORIAL_STANCE["aljazeera.com"],
    "guardian":     EDITORIAL_STANCE["theguardian.com"],
    "daily mail":   EDITORIAL_STANCE["dailymail.co.uk"],
    "fox news":     EDITORIAL_STANCE["foxnews.com"],
    "msnbc":        EDITORIAL_STANCE["msnbc.com"],
    "new york times": EDITORIAL_STANCE["nytimes.com"],
    "washington post": EDITORIAL_STANCE["washingtonpost.com"],
    "wall street journal": EDITORIAL_STANCE["wsj.com"],
    "krebs":        EDITORIAL_STANCE["krebsonsecurity.com"],
    "ars technica": EDITORIAL_STANCE["arstechnica.com"],
    "hacker news":  EDITORIAL_STANCE["news.ycombinator.com"],
    "reddit":       EDITORIAL_STANCE["reddit.com"],
}
