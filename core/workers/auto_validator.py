"""
Auto-Validator Worker — background Tier 1 validation for new entities.

Runs as a daemon thread. Picks up knowledge_nodes with enrichment_status='pending'
and validates them against Wikipedia/Wikidata. Updates:
  - enrichment_status: pending → complete
  - completeness_score: 0 → 0.4 (Wikipedia) or 0.6 (Wikidata)
  - completeness_tier: stub → partial (or solid with Wikidata)
  - envelope.profile: populated with description, DOB, etc.
  - envelope.validation: validated/unvalidated status

Runs in batches of 10. Uses async-style sequential HTTP (not concurrent yet)
with 1 second between Wikipedia calls to respect rate limits.

Never crashes — exponential backoff on errors.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.auto_validator")

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "validated": 0,
    "unvalidated": 0,
    "errors": 0,
}

CHECK_INTERVAL = 60   # seconds between checks (respect rate limits)
BATCH_SIZE = 5        # nodes per batch (smaller = less API pressure)
CACHE_TTL_DAYS = 7    # skip re-research within this window
API_DELAY = 3         # seconds between Wikipedia calls (avoid 429)


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_worker_loop, args=(tenant_id,),
        name="auto-validator", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    log.info("Auto-validator worker started")
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
                _STATUS["phase"] = "idle — no pending nodes"
        except Exception as e:
            log.warning("Auto-validator error: %s", e)
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
    """Find pending entity/place nodes and validate via Wikipedia."""

    # Find nodes needing validation
    nodes = _find_pending_nodes(tenant_id, BATCH_SIZE)
    if not nodes:
        return 0

    processed = 0
    for node in nodes:
        try:
            _validate_node(node, tenant_id)
            processed += 1
        except Exception as e:
            log.debug("Validation failed for %s: %s", node["node_id"], e)
            _STATUS["errors"] += 1

        # Rate limit: respect Wikipedia's API limits
        time.sleep(API_DELAY)

    _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
    return processed


def _find_pending_nodes(tenant_id: str, limit: int) -> list:
    """Find entity/place nodes that haven't been validated yet."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT node_id, title, node_type, subtype, envelope
                    FROM knowledge_nodes
                    WHERE tenant_id = %s
                      AND node_type = 'entity'
                      AND (enrichment_status = 'pending' OR enrichment_status IS NULL)
                      AND title IS NOT NULL
                      AND LENGTH(title) >= 3
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
        log.debug("Failed to find pending nodes: %s", e)
        return []


def _validate_node(node: dict, tenant_id: str):
    """Validate a single node against Wikipedia + Wikidata."""

    name = node["title"]
    node_id = node["node_id"]
    envelope = node["envelope"] or {}

    profile: dict = {}
    validation_status = "unvalidated"
    validation_source = None
    validation_id = None
    completeness_score = 0.1  # minimum — node exists
    completeness_tier = "stub"

    # ── Sanctions check (runs before Wikipedia) ────────────────────
    try:
        from core.gdu.canonical_names import check_sanctions
        sanctions = check_sanctions(name)
        if sanctions:
            profile["sanctions"] = sanctions
            log.info("SANCTIONED entity: %s — %s", name, sanctions.get("lists"))
    except Exception:
        pass

    # ── Wikipedia lookup ──────────────────────────────────────────
    try:
        from core.research.sources import wikipedia_summary
        wiki = wikipedia_summary(name)
        if wiki and wiki.get("extract"):
            profile["description"] = wiki["extract"]
            if wiki.get("thumbnail"):
                profile["thumbnail"] = wiki["thumbnail"]
            if wiki.get("page_url"):
                profile["wikipedia_url"] = wiki["page_url"]

            wikidata_id = wiki.get("wikidata_id")
            validation_status = "validated"
            validation_source = "Wikipedia"
            completeness_score = 0.4
            completeness_tier = "partial"

            # ── Wikidata structured data ──────────────────────────
            if wikidata_id:
                try:
                    from core.research.sources import wikidata_entity
                    wd = wikidata_entity(wikidata_id)
                    if wd:
                        validation_id = wikidata_id
                        validation_source = "Wikidata"
                        completeness_score = 0.6
                        completeness_tier = "partial"

                        # Person fields
                        for key in ["date_of_birth", "date_of_death", "nationality",
                                    "occupation", "position_held", "employer",
                                    "educated_at", "twitter_handle", "official_website"]:
                            if wd.get(key):
                                profile[key] = wd[key]
                                completeness_score = min(completeness_score + 0.05, 0.85)

                        # Place/country fields
                        for key in ["population", "area_km2", "head_of_state",
                                    "head_of_government", "capital", "currency",
                                    "official_language", "continent", "country"]:
                            if wd.get(key):
                                profile[key] = wd[key]
                                completeness_score = min(completeness_score + 0.05, 0.85)

                        if completeness_score >= 0.7:
                            completeness_tier = "solid"

                        # ── Auto-correct category + subtype from Wikidata evidence ──
                        # The GDU collapse: Wikidata tells us what something IS,
                        # overriding the LLM's guess. This fixes countries showing
                        # as organizations, people as locations, etc.
                        corrected_category = None
                        corrected_subtype = None

                        if wd.get("population") or wd.get("capital") or wd.get("head_of_state"):
                            corrected_category = "place"
                            corrected_subtype = "country"
                        elif wd.get("date_of_birth") or wd.get("occupation"):
                            corrected_category = "entity"
                            corrected_subtype = "person"
                        elif wd.get("area_km2") and not wd.get("population"):
                            corrected_category = "place"
                            corrected_subtype = "admin_region"

                        if corrected_subtype and corrected_subtype != node.get("subtype"):
                            log.info("GDU reclassification: %s — %s/%s → %s/%s (Wikidata evidence)",
                                     name, node.get("node_type"), node.get("subtype"),
                                     corrected_category, corrected_subtype)
                            node["subtype"] = corrected_subtype
                            if corrected_category:
                                node["corrected_category"] = corrected_category

                except Exception as e:
                    log.debug("Wikidata lookup failed for %s: %s", name, e)

    except ImportError:
        log.debug("research sources not available")
        return
    except Exception as e:
        log.debug("Wikipedia lookup failed for %s: %s", name, e)

    # If Wikipedia returned NOTHING (rate limited or error), don't mark as complete.
    # Leave as pending so we retry later when the rate limit clears.
    if not profile and validation_status == "unvalidated":
        log.debug("No data for %s — keeping as pending for retry", name)
        return

    # ── Check for Wikidata collision (same QID = merge) ─────────
    if validation_id:
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    # Find any OTHER node with the same Wikidata ID
                    cur.execute("""
                        SELECT node_id, title, source_count
                        FROM knowledge_nodes
                        WHERE tenant_id = %s AND node_id != %s
                          AND node_type = 'entity'
                          AND envelope->'validation'->>'source_id' = %s
                        LIMIT 1
                    """, (tenant_id, node_id, validation_id))
                    collision = cur.fetchone()

                    if collision:
                        # Same Wikidata entity — merge this node into the existing one
                        canonical_id = collision[0]
                        log.info("Merging %s (%s) into %s (%s) — same Wikidata %s",
                                 node_id, name, canonical_id, collision[1], validation_id)

                        # Re-point edges
                        cur.execute("UPDATE knowledge_edges SET target_node_id = %s WHERE target_node_id = %s",
                                    (canonical_id, node_id))
                        cur.execute("UPDATE knowledge_edges SET source_node_id = %s WHERE source_node_id = %s",
                                    (canonical_id, node_id))

                        # Add source_count to canonical
                        cur.execute("""
                            UPDATE knowledge_nodes SET source_count = source_count + %s, updated_at = NOW()
                            WHERE node_id = %s
                        """, (1, canonical_id))

                        # Delete the duplicate
                        cur.execute("DELETE FROM knowledge_nodes WHERE node_id = %s", (node_id,))
                        conn.commit()

                        _STATUS["validated"] += 1
                        return  # done — merged into canonical
        except Exception as e:
            log.debug("Wikidata collision check failed: %s", e)

    # Also check for name collision (same normalized title, already validated)
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT node_id, title, source_count
                    FROM knowledge_nodes
                    WHERE tenant_id = %s AND node_id != %s
                      AND node_type = 'entity'
                      AND LOWER(title) = LOWER(%s)
                      AND enrichment_status = 'complete'
                    LIMIT 1
                """, (tenant_id, node_id, name))
                name_collision = cur.fetchone()

                if name_collision:
                    canonical_id = name_collision[0]
                    log.info("Merging %s (%s) into %s — same name",
                             node_id, name, canonical_id)
                    cur.execute("UPDATE knowledge_edges SET target_node_id = %s WHERE target_node_id = %s",
                                (canonical_id, node_id))
                    cur.execute("UPDATE knowledge_edges SET source_node_id = %s WHERE source_node_id = %s",
                                (canonical_id, node_id))
                    cur.execute("UPDATE knowledge_nodes SET source_count = source_count + 1, updated_at = NOW() WHERE node_id = %s",
                                (canonical_id,))
                    cur.execute("DELETE FROM knowledge_nodes WHERE node_id = %s", (node_id,))
                    conn.commit()
                    _STATUS["validated"] += 1
                    return
    except Exception as e:
        log.debug("Name collision check failed: %s", e)

    # ── Store results ─────────────────────────────────────────────
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Update envelope
                envelope["profile"] = profile
                envelope["validation"] = {
                    "status": validation_status,
                    "source": validation_source,
                    "source_id": validation_id,
                    "source_url": f"https://www.wikidata.org/wiki/{validation_id}" if validation_id else None,
                    "confidence": completeness_score,
                    "validated_at": datetime.now(timezone.utc).isoformat(),
                }
                envelope["research"] = {
                    "sources_used": [s for s in [validation_source] if s],
                    "researched_at": datetime.now(timezone.utc).isoformat(),
                    "cache_ttl_days": CACHE_TTL_DAYS,
                }

                # Determine corrected category + subtype from Wikidata
                corrected_sub = node.get("subtype")
                corrected_cat = node.get("corrected_category")  # set by Wikidata evidence

                # ── CROSS-CATEGORY GUARD ─────────────────────────────────
                # Before this fix, the auto_validator UPDATEd canonical_type
                # to whatever Wikidata said — even when that meant putting
                # 'country' under entity (incoherent: country is a place
                # subtype). That created the bypass leak we keep cleaning up.
                #
                # New behavior: when Wikidata evidence says the entity
                # belongs in a DIFFERENT category, route through
                # cross_category_migrator instead of overwriting in place.
                #
                # The migrator either:
                #   - Migrates the row to the right master table (Tier 2)
                #   - Marks it pending if evidence is uncertain (Tier 1)
                # Either way, we don't poison entities.canonical_type.
                node_category = (node.get("node_type") or "entity").lower()
                cross_category = (
                    corrected_cat is not None
                    and corrected_cat != node_category
                )

                # Always update knowledge_nodes envelope + enrichment status
                # (this is the row-level enrichment, distinct from the
                # category fix). Don't overwrite subtype if cross-category —
                # the migrator handles category-level moves.
                if cross_category:
                    # Don't change node_type/subtype — migrator does that.
                    cur.execute("""
                        UPDATE knowledge_nodes
                        SET envelope = %s,
                            enrichment_status = 'complete',
                            completeness_score = %s,
                            completeness_tier = %s,
                            description = COALESCE(NULLIF(%s, ''), description),
                            updated_at = NOW()
                        WHERE node_id = %s AND tenant_id = %s
                    """, (
                        json.dumps(envelope),
                        completeness_score,
                        completeness_tier,
                        profile.get("description", "")[:500],
                        node_id, tenant_id,
                    ))
                else:
                    # Same-category enrichment — refine subtype as before.
                    cur.execute("""
                        UPDATE knowledge_nodes
                        SET envelope = %s,
                            enrichment_status = 'complete',
                            completeness_score = %s,
                            completeness_tier = %s,
                            subtype = COALESCE(%s, subtype),
                            entity_type = COALESCE(%s, entity_type),
                            description = COALESCE(NULLIF(%s, ''), description),
                            updated_at = NOW()
                        WHERE node_id = %s AND tenant_id = %s
                    """, (
                        json.dumps(envelope),
                        completeness_score,
                        completeness_tier,
                        corrected_sub,
                        corrected_sub,
                        profile.get("description", "")[:500],
                        node_id, tenant_id,
                    ))

                # Update entity record (or migrate it if cross-category)
                entity_id_str = node_id.replace("entity:", "")
                if entity_id_str.isdigit():
                    entity_id = int(entity_id_str)
                    if cross_category:
                        # Pull existing aliases for the migrator
                        cur.execute(
                            "SELECT canonical_name, aliases FROM entities "
                            "WHERE entity_id = %s AND tenant_id = %s",
                            (entity_id, tenant_id),
                        )
                        row = cur.fetchone()
                        canonical_name = row[0] if row else name
                        aliases = list(row[1]) if (row and row[1]) else []

                        from core.gdu.cross_category_migrator import reclassify_entity
                        # Single Wikidata Q-ID = single candidate (Tier 2).
                        # Multi-candidate disambiguation (Tier 3) is a
                        # separate worker; for now, a single hit is treated
                        # as deterministic.
                        result = reclassify_entity(
                            conn,
                            tenant_id=tenant_id,
                            entity_id=entity_id,
                            canonical_name=canonical_name,
                            aliases=aliases,
                            target_category=corrected_cat,
                            target_subtype=corrected_sub,
                            source=validation_source or "auto_validator",
                            source_id=validation_id,
                            profile=profile,
                            completeness_score=completeness_score,
                            completeness_tier=completeness_tier,
                            multi_candidate=False,
                        )
                        log.info(
                            "Cross-category reclassify %s (%s): %s.%s → %s.%s "
                            "(action=%s, new=%s, reason=%s)",
                            node_id, canonical_name,
                            node_category, node.get("subtype"),
                            corrected_cat, corrected_sub,
                            result.action, result.new_node_id, result.reason,
                        )
                    else:
                        # Same-category enrichment — refine canonical_type
                        # only when the corrected subtype is in entityvocab.
                        # Otherwise keep the existing canonical_type (the
                        # COALESCE no-ops on NULL).
                        try:
                            from core.ldu.subtype_validator import safe_canonical_type
                            _safe_sub = safe_canonical_type(corrected_sub)
                        except Exception:
                            _safe_sub = None
                        cur.execute("""
                            UPDATE entities
                            SET envelope = %s,
                                completeness_score = GREATEST(completeness_score, %s),
                                completeness_tier = %s,
                                canonical_type = COALESCE(%s, canonical_type),
                                updated_at = NOW()
                            WHERE entity_id = %s AND tenant_id = %s
                        """, (
                            json.dumps({"profile": profile}),
                            completeness_score,
                            completeness_tier,
                            _safe_sub,
                            entity_id, tenant_id,
                        ))

            conn.commit()

        if validation_status == "validated":
            _STATUS["validated"] += 1
            log.info("Validated %s → %s (%s, %.0f%%)",
                     node_id, name, validation_source, completeness_score * 100)
        else:
            _STATUS["unvalidated"] += 1
            log.info("Unvalidated %s → %s (no Wikipedia match)", node_id, name)

    except Exception as e:
        log.warning("Failed to store validation for %s: %s", node_id, e)
        _STATUS["errors"] += 1
