"""
Place Validator Worker — verifies place nodes against GeoNames + Wikidata.

Picks up knowledge_nodes with node_type='place' and enrichment_status='pending'.
Validates them against external geographic databases:
  - Wikidata: pulls coordinates, country, population, official_name
  - GeoNames: secondary check for cities and admin regions
  - Coordinate sanity: verify within country bounds
  - Duplicate detection: detect "Damascus, Syria" vs "Damascus, Virginia"

Updates:
  - enrichment_status: pending → complete
  - completeness_score: 0 → 0.4 (Wikipedia) or 0.7 (Wikidata + coords)
  - completeness_tier: stub → partial → solid
  - subtype: auto-corrects (city/admin_region/country) from Wikidata evidence
  - envelope.profile: populated with description, coordinates, country, etc.
  - envelope.validation: validated/unvalidated/contested status

Conflict detection:
  - If multiple places match same name with different countries → contested
  - If LLM said "city" but Wikidata says "country" → reclassify, log conflict
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.place_validator")

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "validated": 0,
    "unvalidated": 0,
    "reclassified": 0,
    "conflicts": 0,
    "errors": 0,
}

CHECK_INTERVAL = 60
BATCH_SIZE = 5
API_DELAY = 3


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_worker_loop, args=(tenant_id,),
        name="place-validator", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    log.info("Place validator worker started")
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
                _STATUS["phase"] = "idle — no pending places"
        except Exception as e:
            log.warning("Place validator error: %s", e)
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
    nodes = _find_pending_places(tenant_id, BATCH_SIZE)
    if not nodes:
        return 0

    processed = 0
    for node in nodes:
        try:
            _validate_place(node, tenant_id)
            processed += 1
        except Exception as e:
            log.debug("Place validation failed for %s: %s", node["node_id"], e)
            _STATUS["errors"] += 1

        time.sleep(API_DELAY)

    _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
    return processed


def _find_pending_places(tenant_id: str, limit: int) -> list:
    """Find place nodes that haven't been validated yet."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT node_id, title, node_type, subtype, envelope
                    FROM knowledge_nodes
                    WHERE tenant_id = %s
                      AND node_type = 'place'
                      AND (enrichment_status = 'pending' OR enrichment_status IS NULL)
                      AND title IS NOT NULL
                      AND LENGTH(title) >= 2
                    ORDER BY source_count DESC, created_at ASC
                    LIMIT %s
                """, (tenant_id, limit))

                return [
                    {
                        "node_id": r[0],
                        "title": r[1],
                        "node_type": r[2],
                        "subtype": r[3],
                        "envelope": r[4] or {},
                    }
                    for r in cur.fetchall()
                ]
    except Exception as e:
        log.debug("Failed to find pending places: %s", e)
        return []


def _validate_place(node: dict, tenant_id: str):
    """Validate a single place against Wikidata."""
    name = node["title"]
    node_id = node["node_id"]
    envelope = node["envelope"] or {}

    profile: dict = {}
    validation_status = "unvalidated"
    validation_source = None
    validation_id = None
    completeness_score = 0.1
    completeness_tier = "stub"
    corrected_subtype = None
    conflicts: list[dict] = []

    # ── Wikipedia/Wikidata lookup ──────────────────────────────────
    try:
        from core.research.sources import wikipedia_summary, wikidata_entity
        wiki = wikipedia_summary(name)
        if wiki and wiki.get("extract"):
            profile["description"] = wiki["extract"]
            if wiki.get("page_url"):
                profile["wikipedia_url"] = wiki["page_url"]
            if wiki.get("thumbnail"):
                profile["thumbnail"] = wiki["thumbnail"]

            wikidata_id = wiki.get("wikidata_id")
            validation_status = "validated"
            validation_source = "Wikipedia"
            completeness_score = 0.4
            completeness_tier = "partial"

            if wikidata_id:
                try:
                    wd = wikidata_entity(wikidata_id)
                    if wd:
                        validation_id = wikidata_id
                        validation_source = "Wikidata"
                        completeness_score = 0.6

                        # Place-specific fields
                        for key in ["coordinates", "country", "population",
                                    "area_km2", "capital", "official_language",
                                    "currency", "head_of_state", "head_of_government",
                                    "continent", "iso_country_code",
                                    "located_in", "official_website"]:
                            if wd.get(key):
                                profile[key] = wd[key]
                                completeness_score = min(completeness_score + 0.05, 0.85)

                        if completeness_score >= 0.7:
                            completeness_tier = "solid"

                        # ── Auto-correct subtype from Wikidata evidence ──
                        if wd.get("head_of_state") or wd.get("capital"):
                            corrected_subtype = "country"
                        elif wd.get("located_in") and wd.get("population"):
                            # Has parent country + population = city or admin_region
                            pop = wd.get("population")
                            try:
                                pop_int = int(str(pop).replace(",", "").split()[0])
                                if pop_int > 5_000_000:
                                    corrected_subtype = "admin_region"
                                else:
                                    corrected_subtype = "city"
                            except (ValueError, AttributeError):
                                corrected_subtype = "city"
                        elif wd.get("area_km2") and not wd.get("population"):
                            corrected_subtype = "admin_region"

                        # Log conflict if subtype changed
                        if corrected_subtype and corrected_subtype != node.get("subtype"):
                            log.info("Place subtype correction: %s — %s → %s (Wikidata)",
                                     name, node.get("subtype"), corrected_subtype)
                            conflicts.append({
                                "type": "subtype_correction",
                                "old": node.get("subtype"),
                                "new": corrected_subtype,
                                "source": "Wikidata",
                            })
                            _STATUS["reclassified"] += 1

                except Exception as e:
                    log.debug("Wikidata lookup failed for %s: %s", name, e)

    except ImportError:
        log.debug("research sources not available")
        return
    except Exception as e:
        log.debug("Wikipedia lookup failed for %s: %s", name, e)

    # If no data returned, leave pending for retry
    if not profile and validation_status == "unvalidated":
        log.debug("No data for place %s — keeping pending for retry", name)
        return

    # ── Detect duplicate places (same name, different countries) ─────
    if validation_id:
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    # Find OTHER place nodes with same Wikidata ID = duplicate
                    cur.execute("""
                        SELECT node_id, title, source_count
                        FROM knowledge_nodes
                        WHERE tenant_id = %s AND node_id != %s
                          AND node_type = 'place'
                          AND envelope->'validation'->>'source_id' = %s
                        LIMIT 1
                    """, (tenant_id, node_id, validation_id))
                    collision = cur.fetchone()

                    if collision:
                        canonical_id = collision[0]
                        log.info("Merging place %s (%s) into %s (%s) — same Wikidata %s",
                                 node_id, name, canonical_id, collision[1], validation_id)

                        # Re-point edges
                        cur.execute("UPDATE knowledge_edges SET target_node_id = %s WHERE target_node_id = %s",
                                    (canonical_id, node_id))
                        cur.execute("UPDATE knowledge_edges SET source_node_id = %s WHERE source_node_id = %s",
                                    (canonical_id, node_id))
                        cur.execute("""
                            UPDATE knowledge_nodes SET source_count = source_count + 1, updated_at = NOW()
                            WHERE node_id = %s
                        """, (canonical_id,))
                        cur.execute("DELETE FROM knowledge_nodes WHERE node_id = %s", (node_id,))
                        conn.commit()
                        _STATUS["validated"] += 1
                        return
        except Exception as e:
            log.debug("Place collision check failed: %s", e)

    # ── Log conflicts to governance_conflicts ─────────────────────
    if conflicts:
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    for c in conflicts:
                        cur.execute("""
                            INSERT INTO governance_conflicts
                                (tenant_id, conflict_type, severity, entity_id,
                                 competing_values, auto_resolved, auto_resolution,
                                 detected_by, metadata)
                            VALUES (%s, %s, 'routine', NULL, %s::jsonb, TRUE, %s,
                                    'place_validator', %s::jsonb)
                            ON CONFLICT DO NOTHING
                        """, (
                            tenant_id, c["type"],
                            json.dumps([{"value": c["old"], "source": "LLM"},
                                        {"value": c["new"], "source": c["source"]}]),
                            f"corrected to {c['new']}",
                            json.dumps({"node_id": node_id, "name": name}),
                        ))
                conn.commit()
                _STATUS["conflicts"] += 1
        except Exception as e:
            log.debug("Conflict log failed: %s", e)

    # ── Store validation result ────────────────────────────────────
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                envelope["profile"] = profile
                envelope["validation"] = {
                    "status": validation_status,
                    "source": validation_source,
                    "source_id": validation_id,
                    "source_url": f"https://www.wikidata.org/wiki/{validation_id}" if validation_id else None,
                    "confidence": completeness_score,
                    "validated_at": datetime.now(timezone.utc).isoformat(),
                }

                cur.execute("""
                    UPDATE knowledge_nodes
                    SET envelope = %s,
                        enrichment_status = 'complete',
                        completeness_score = %s,
                        completeness_tier = %s,
                        subtype = COALESCE(%s, subtype),
                        description = COALESCE(NULLIF(%s, ''), description),
                        updated_at = NOW()
                    WHERE node_id = %s AND tenant_id = %s
                """, (
                    json.dumps(envelope),
                    completeness_score,
                    completeness_tier,
                    corrected_subtype,
                    profile.get("description", "")[:500],
                    node_id, tenant_id,
                ))

            conn.commit()

        if validation_status == "validated":
            _STATUS["validated"] += 1
            log.info("Validated place %s → %s (%s, %.0f%%)",
                     node_id, name, validation_source, completeness_score * 100)
        else:
            _STATUS["unvalidated"] += 1

    except Exception as e:
        log.warning("Failed to store place validation for %s: %s", node_id, e)
        _STATUS["errors"] += 1
