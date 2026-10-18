"""
Node Enrichment Pipeline — LDU for Knowledge Nodes.

When a knowledge node is created (by AI, signal correlation, or user),
this pipeline auto-fills the structured card fields:

  L (Localize):  Geo-resolve, temporal anchor, source attribution
  D (Describe):  Entity extraction, contradiction/corroboration scan,
                  gap detection, confidence scoring
  U (Utilize):   Match detection rules, update cases, feed threads,
                  update related node confidence

The pipeline is idempotent — running it again on an already-enriched
node will update fields that have new data without destroying existing data.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.enrichment.node")


# ─── Epistemic Governance Rules ──────────────────────────────────────────────
# GDU: what a well-formed node looks like per type

EPISTEMIC_RULES = {
    "fact": {
        "min_sources": 2,
        "required_fields": ["entity_name", "event_time"],
        "can_auto_create": False,
        "promotion_from": "observation",
    },
    "observation": {
        "min_sources": 1,
        "required_fields": [],
        "can_auto_create": True,
        "promotes_to": "fact",
        "promotion_threshold": {"source_count": 2, "corroboration_count": 1},
    },
    "claim": {
        "min_sources": 1,
        "required_fields": [],
        "can_auto_create": True,
        "demotes_on_contradiction": True,
    },
    "hypothesis": {
        "min_sources": 0,
        "required_fields": [],
        "can_auto_create": True,
    },
    "question": {
        "min_sources": 0,
        "required_fields": [],
        "can_auto_create": True,
    },
    "evidence": {
        "min_sources": 1,
        "required_fields": ["source_signals"],
        "can_auto_create": True,
    },
}


def enrich_node(
    node_id: str,
    tenant_id: str,
    provider=None,
    dsn: Optional[str] = None,
) -> dict:
    """
    Run the full L→D→U enrichment pipeline on a single node.

    Returns a summary of what was enriched.
    """
    log.info(f"Enriching node {node_id}")
    result = {"node_id": node_id, "stages": {}}

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT node_id, project_id, tenant_id, title, description,
                       epistemic_type, concepts, source_authority, signal_rid,
                       latitude, longitude, entity_type, entity_name, entity_identifiers,
                       event_time, source_signals, source_count, confidence,
                       metadata, enrichment_status
                FROM knowledge_nodes WHERE node_id = %s AND tenant_id = %s
            """, (node_id, tenant_id))
            row = cur.fetchone()
            if not row:
                return {"error": "Node not found"}

    node = {
        "node_id": row[0], "project_id": row[1], "tenant_id": row[2],
        "title": row[3], "description": row[4] or "",
        "epistemic_type": row[5], "concepts": row[6] or [],
        "source_authority": row[7], "signal_rid": row[8],
        "latitude": row[9], "longitude": row[10],
        "entity_type": row[11], "entity_name": row[12],
        "entity_identifiers": _parse_jsonb(row[13]),
        "event_time": row[14],
        "source_signals": row[15] or [],
        "source_count": row[16] or 0,
        "confidence": row[17] or 0.5,
        "metadata": _parse_jsonb(row[18]),
        "enrichment_status": row[19],
    }

    # Mark as enriching
    _update_node(node_id, tenant_id, {"enrichment_status": "enriching"}, dsn)

    enrichment_log = []

    try:
        # ═══ L: LOCALIZE ═══
        l_result = _stage_localize(node, dsn)
        result["stages"]["localize"] = l_result
        enrichment_log.append({"stage": "L", "time": _now(), "result": l_result.get("summary", "")})

        # ═══ D: DESCRIBE ═══
        d_result = _stage_describe(node, provider, dsn)
        result["stages"]["describe"] = d_result
        enrichment_log.append({"stage": "D", "time": _now(), "result": d_result.get("summary", "")})

        # ═══ U: UTILIZE ═══
        u_result = _stage_utilize(node, dsn)
        result["stages"]["utilize"] = u_result
        enrichment_log.append({"stage": "U", "time": _now(), "result": u_result.get("summary", "")})

        # Mark complete
        _update_node(node_id, tenant_id, {
            "enrichment_status": "complete",
            "enrichment_log": json.dumps(enrichment_log),
            "last_enriched_at": _now(),
            "ai_enriched": True,
        }, dsn)

    except Exception as e:
        log.error(f"Enrichment failed for {node_id}: {e}")
        _update_node(node_id, tenant_id, {
            "enrichment_status": "failed",
            "enrichment_log": json.dumps(enrichment_log + [{"stage": "ERROR", "time": _now(), "result": str(e)}]),
        }, dsn)
        result["error"] = str(e)

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# L: LOCALIZE — geo-resolve, temporal anchor, source attribution
# ═══════════════════════════════════════════════════════════════════════════════

def _stage_localize(node: dict, dsn: Optional[str] = None) -> dict:
    updates = {}
    summary_parts = []

    # 1. Geo-resolve: if has lat/lon but no place_name, reverse-geocode
    if node["latitude"] and node["longitude"] and not node["metadata"].get("address"):
        try:
            import httpx
            resp = httpx.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lat": node["latitude"], "lon": node["longitude"], "format": "json", "zoom": 14},
                headers={"User-Agent": "Enki/1.0"},
                timeout=8,
            )
            if resp.status_code == 200:
                data = resp.json()
                addr = data.get("address", {})
                updates["place_name"] = data.get("display_name", "")[:500]
                updates["place_country"] = addr.get("country", "")
                updates["place_context"] = json.dumps({
                    "timezone": addr.get("timezone"),
                    "admin_region": addr.get("state") or addr.get("region"),
                    "city": addr.get("city") or addr.get("town") or addr.get("village"),
                    "address": data.get("display_name", ""),
                })
                summary_parts.append(f"geocoded to {updates['place_country']}")
        except Exception as e:
            log.debug(f"Geocode failed: {e}")

    # 2. Temporal anchor: if no event_time, use signal timestamp or creation time
    if not node["event_time"]:
        if node["signal_rid"]:
            try:
                with get_connection(dsn) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT timestamp FROM signals WHERE rid = %s", (node["signal_rid"],))
                        row = cur.fetchone()
                        if row and row[0]:
                            updates["event_time"] = row[0].isoformat()
                            updates["temporal_precision"] = "exact"
                            summary_parts.append("temporal anchored from signal")
            except Exception:
                pass

    # 3. Source attribution: count unique sources
    source_signals = node["source_signals"] or []
    if node["signal_rid"] and node["signal_rid"] not in source_signals:
        source_signals = list(source_signals) + [node["signal_rid"]]
        updates["source_signals"] = source_signals

    if source_signals:
        # Get source types from signals
        try:
            with get_connection(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT DISTINCT signal_type FROM signals WHERE rid = ANY(%s)
                    """, (source_signals,))
                    types = [r[0] for r in cur.fetchall() if r[0]]
                    updates["source_types"] = types
                    updates["source_count"] = len(source_signals)
                    summary_parts.append(f"{len(source_signals)} source(s)")
        except Exception:
            pass

    if updates:
        _update_node(node["node_id"], node["tenant_id"], updates, dsn)
        node.update({k: v for k, v in updates.items() if not isinstance(v, str) or not k.endswith("_context")})

    return {"summary": "; ".join(summary_parts) if summary_parts else "no localization needed", "updates": len(updates)}


# ═══════════════════════════════════════════════════════════════════════════════
# D: DESCRIBE — entity extraction, contradiction/corroboration, gaps, confidence
# ═══════════════════════════════════════════════════════════════════════════════

def _stage_describe(node: dict, provider=None, dsn: Optional[str] = None) -> dict:
    updates = {}
    summary_parts = []

    # 1. Entity extraction via AI → resolve through GDU-governed resolver
    if provider and not node["entity_name"] and node["description"]:
        try:
            from core.ai.provider import LLMMessage
            entity_prompt = f"""Extract the primary entity from this intelligence note. Return ONLY valid JSON.

Title: {node['title']}
Description: {node['description'][:500]}

Return: {{"entity_type": "person|vessel|aircraft|vehicle|organization|facility|account|device|unknown", "entity_name": "name", "identifiers": {{"key": "value"}}}}
If no clear entity, return: {{"entity_type": "unknown", "entity_name": "", "identifiers": {{}}}}"""

            response = provider.complete(
                [LLMMessage(role="user", content=entity_prompt)],
                max_tokens=200, temperature=0.0,
            )
            if response.content and not response.error:
                import re
                match = re.search(r'\{[^{}]*"entity_type"[^{}]*\}', response.content)
                if match:
                    entity_data = json.loads(match.group())
                    if entity_data.get("entity_name"):
                        # Resolve through canonical resolver instead of raw write
                        from core.gdu.entity_resolution import resolve_entity
                        resolution = resolve_entity(
                            tenant_id=node["tenant_id"],
                            name=entity_data["entity_name"],
                            entity_type=entity_data.get("entity_type", "unknown"),
                            identifiers=entity_data.get("identifiers", {}),
                            context=node["description"][:300],
                            latitude=node.get("latitude"),
                            longitude=node.get("longitude"),
                            source_pipeline="node_enrich",
                            provider=provider,
                            dsn=dsn,
                        )
                        updates["entity_type"] = entity_data.get("entity_type", "unknown")
                        updates["entity_name"] = entity_data["entity_name"]
                        updates["entity_identifiers"] = json.dumps(entity_data.get("identifiers", {}))
                        if resolution.entity_id:
                            updates["master_entity_id"] = resolution.entity_id
                        summary_parts.append(
                            f"entity: {updates['entity_name']} "
                            f"(canonical:{resolution.entity_id}, "
                            f"{resolution.action}@{resolution.confidence:.2f})"
                        )
        except Exception as e:
            log.debug(f"Entity extraction failed: {e}")

    # 2. Contradiction scan
    contradictions = _scan_contradictions(node, dsn)
    # Contradiction scan creates EDGES (supports/contrasts) — the durable artifacts.
    # Counts are NOT stored on the node. They're computed per-context at query time.
    if contradictions:
        summary_parts.append(f"{len(contradictions)} contradiction(s) — edges created")

    # 3. Corroboration scan — same: creates edges, no counts on node
    corroborations = _scan_corroborations(node, dsn)
    if corroborations:
        summary_parts.append(f"{len(corroborations)} corroboration(s) — edges created")

    # 4. Gap detection — stored on node as intrinsic (these are about the node's data quality)
    gaps = _detect_gaps(node)
    if gaps:
        updates["gaps"] = gaps
        updates["gap_count"] = len(gaps)
        summary_parts.append(f"{len(gaps)} gap(s)")

    # 5. Evidence quality (baseline, intrinsic to node — NOT contextual confidence)
    # This is "how good is the raw evidence?" not "how confident are we in this context?"
    eq = _compute_evidence_quality(node, len(contradictions), len(corroborations))
    updates["confidence"] = eq  # column will be renamed to evidence_quality
    updates["confidence_factors"] = json.dumps({
        "source_count": node.get("source_count", 0),
        "authority_score": _authority_score(node["source_authority"]),
        "has_location": bool(node["latitude"]),
        "has_temporal": bool(node["event_time"]),
        "has_entity": bool(node.get("entity_name")),
        "gap_count": len(gaps),
        "note": "baseline evidence quality — contextual scoring is per-thread/case",
    })

    # 6. Promotion check
    rules = EPISTEMIC_RULES.get(node["epistemic_type"], {})
    promo = rules.get("promotion_threshold")
    if promo:
        source_ok = node.get("source_count", 0) >= promo.get("source_count", 99)
        corrob_ok = len(corroborations) >= promo.get("corroboration_count", 99)
        updates["promotion_eligible"] = source_ok and corrob_ok
        if source_ok and corrob_ok:
            summary_parts.append("eligible for promotion")

    if updates:
        _update_node(node["node_id"], node["tenant_id"], updates, dsn)

    return {"summary": "; ".join(summary_parts) if summary_parts else "described", "updates": len(updates)}


def _scan_contradictions(node: dict, dsn: Optional[str] = None) -> list[dict]:
    """Find existing nodes that contradict this one (same entity, conflicting claims)."""
    contradictions = []
    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                # Strategy 1: Same entity, different claims in same timeframe
                if node.get("entity_name"):
                    cur.execute("""
                        SELECT node_id, title, description, epistemic_type, event_time, latitude, longitude
                        FROM knowledge_nodes
                        WHERE tenant_id = %s AND node_id != %s
                          AND entity_name = %s
                          AND epistemic_type IN ('observation', 'fact', 'claim')
                        ORDER BY created_at DESC LIMIT 20
                    """, (node["tenant_id"], node["node_id"], node["entity_name"]))
                    for r in cur.fetchall():
                        # Check spatial contradiction (same entity, very different locations, similar time)
                        if node["latitude"] and r[5]:
                            from math import radians, cos, sin, sqrt, atan2
                            lat1, lon1 = radians(node["latitude"]), radians(node["longitude"] or 0)
                            lat2, lon2 = radians(r[5]), radians(r[6] or 0)
                            dlat, dlon = lat2 - lat1, lon2 - lon1
                            a = sin(dlat/2)**2 + cos(lat1)*cos(lat2)*sin(dlon/2)**2
                            dist_km = 6371 * 2 * atan2(sqrt(a), sqrt(1-a))

                            if dist_km > 100:  # >100km apart — suspicious for same entity
                                contradictions.append({
                                    "node_id": r[0], "title": r[1],
                                    "reason": f"Same entity '{node['entity_name']}' at {dist_km:.0f}km apart",
                                    "distance_km": round(dist_km),
                                })
                                # Auto-create contrasts edge
                                _ensure_edge(node["node_id"], r[0], "contrasts",
                                             f"Spatial contradiction: {dist_km:.0f}km apart",
                                             node["project_id"], dsn)

                # Strategy 2: Existing "contrasts" edges pointing to this node
                cur.execute("""
                    SELECT ke.source_node_id, ke.target_node_id, ke.evidence, kn.title
                    FROM knowledge_edges ke
                    JOIN knowledge_nodes kn ON kn.node_id = CASE
                        WHEN ke.source_node_id = %s THEN ke.target_node_id
                        ELSE ke.source_node_id END
                    WHERE (ke.source_node_id = %s OR ke.target_node_id = %s)
                      AND ke.relationship = 'contrasts'
                """, (node["node_id"], node["node_id"], node["node_id"]))
                for r in cur.fetchall():
                    other_id = r[0] if r[0] != node["node_id"] else r[1]
                    if not any(c["node_id"] == other_id for c in contradictions):
                        contradictions.append({
                            "node_id": other_id, "title": r[3],
                            "reason": r[2] or "Existing contrasts edge",
                        })

    except Exception as e:
        log.debug(f"Contradiction scan error: {e}")

    return contradictions


def _scan_corroborations(node: dict, dsn: Optional[str] = None) -> list[dict]:
    """Find existing nodes that support this one (same entity + claim, different sources)."""
    corroborations = []
    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                # Same entity, similar location, different source signals
                if node.get("entity_name") and node.get("latitude"):
                    cur.execute("""
                        SELECT node_id, title, source_signals, signal_rid
                        FROM knowledge_nodes
                        WHERE tenant_id = %s AND node_id != %s
                          AND entity_name = %s
                          AND latitude IS NOT NULL
                          AND ST_DWithin(
                              ST_MakePoint(longitude, latitude)::geography,
                              ST_MakePoint(%s, %s)::geography,
                              50000)
                        ORDER BY created_at DESC LIMIT 10
                    """, (node["tenant_id"], node["node_id"], node["entity_name"],
                          node.get("longitude", 0), node["latitude"]))
                    for r in cur.fetchall():
                        other_signals = set(r[2] or [])
                        my_signals = set(node.get("source_signals", []))
                        if r[3]:
                            other_signals.add(r[3])
                        # Different source = independent corroboration
                        if not my_signals.intersection(other_signals):
                            corroborations.append({
                                "node_id": r[0], "title": r[1],
                                "reason": "Same entity, same area, independent source",
                            })
                            _ensure_edge(node["node_id"], r[0], "supports",
                                         "Independent corroboration: same entity, proximate location",
                                         node["project_id"], dsn)

                # Existing "supports" edges
                cur.execute("""
                    SELECT ke.source_node_id, ke.target_node_id, kn.title
                    FROM knowledge_edges ke
                    JOIN knowledge_nodes kn ON kn.node_id = CASE
                        WHEN ke.source_node_id = %s THEN ke.target_node_id
                        ELSE ke.source_node_id END
                    WHERE (ke.source_node_id = %s OR ke.target_node_id = %s)
                      AND ke.relationship = 'supports'
                """, (node["node_id"], node["node_id"], node["node_id"]))
                for r in cur.fetchall():
                    other_id = r[0] if r[0] != node["node_id"] else r[1]
                    if not any(c["node_id"] == other_id for c in corroborations):
                        corroborations.append({"node_id": other_id, "title": r[2], "reason": "Existing supports edge"})

    except Exception as e:
        log.debug(f"Corroboration scan error: {e}")

    return corroborations


def _detect_gaps(node: dict) -> list[str]:
    """Identify what's missing from this node card."""
    gaps = []

    if not node.get("entity_name"):
        gaps.append("No entity identified")
    if not node.get("event_time"):
        gaps.append("No temporal anchor — when did this happen?")
    if not node.get("latitude"):
        gaps.append("No location data")
    if (node.get("source_count") or 0) < 2:
        gaps.append("Single source — needs independent corroboration")
    if node.get("source_authority") == "unverified":
        gaps.append("Source authority unverified")
    if node.get("source_authority") == "disputed":
        gaps.append("Source authority is disputed")

    rules = EPISTEMIC_RULES.get(node["epistemic_type"], {})
    for field in rules.get("required_fields", []):
        if not node.get(field):
            gaps.append(f"Missing required field: {field}")

    return gaps


# ═══════════════════════════════════════════════════════════════════════════════
# U: UTILIZE — match rules, update cases, feed threads, adjust confidence
# ═══════════════════════════════════════════════════════════════════════════════

def _stage_utilize(node: dict, dsn: Optional[str] = None) -> dict:
    updates = {}
    summary_parts = []

    tenant_id = node["tenant_id"]

    # 1. Match entity against case observables (watchlists)
    try:
        if node.get("entity_name"):
            with get_connection(dsn) as conn:
                with conn.cursor() as cur:
                    # Search case_observables for matching entity name
                    cur.execute("""
                        SELECT co.case_id, co.observable_type, co.observable_value, c.title
                        FROM case_observables co
                        JOIN cases c ON c.case_id = co.case_id AND c.tenant_id = co.tenant_id
                        WHERE co.tenant_id = %s
                          AND co.normalized_value = LOWER(%s)
                          AND c.status IN ('open', 'investigating')
                    """, (tenant_id, node["entity_name"]))
                    matches = cur.fetchall()

                    for case_id, obs_type, obs_value, case_title in matches:
                        # Link node to case
                        cur.execute("""
                            INSERT INTO case_nodes (case_id, node_id, tenant_id, linked_by)
                            VALUES (%s, %s, %s, 'enrichment_pipeline')
                            ON CONFLICT DO NOTHING
                        """, (case_id, node["node_id"], tenant_id))
                        summary_parts.append(f"linked to case '{case_title}'")

                    # Also check entity_identifiers values
                    identifiers = node.get("entity_identifiers", {})
                    for key, value in identifiers.items():
                        if not value:
                            continue
                        cur.execute("""
                            SELECT co.case_id, c.title
                            FROM case_observables co
                            JOIN cases c ON c.case_id = co.case_id AND c.tenant_id = co.tenant_id
                            WHERE co.tenant_id = %s
                              AND co.normalized_value = LOWER(%s)
                              AND c.status IN ('open', 'investigating')
                        """, (tenant_id, str(value)))
                        for case_id, case_title in cur.fetchall():
                            cur.execute("""
                                INSERT INTO case_nodes (case_id, node_id, tenant_id, linked_by)
                                VALUES (%s, %s, %s, 'enrichment_pipeline')
                                ON CONFLICT DO NOTHING
                            """, (case_id, node["node_id"], tenant_id))
                            summary_parts.append(f"identifier '{key}' matched case '{case_title}'")

                conn.commit()
    except Exception as e:
        log.debug(f"Case matching error: {e}")

    # 2. Feed monitoring threads watching for this signal type
    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT thread_id, title, watch_filter
                    FROM chat_threads
                    WHERE tenant_id = %s AND auto_run = TRUE AND status = 'active'
                      AND watch_filter IS NOT NULL
                """, (tenant_id,))
                for thread_id, thread_title, watch_filter in cur.fetchall():
                    wf = _parse_jsonb(watch_filter)
                    matched = False
                    # Check signal type filter
                    watch_types = wf.get("signal_types", [])
                    if watch_types and node.get("source_types"):
                        for st in node["source_types"]:
                            if any(st.startswith(wt) for wt in watch_types):
                                matched = True
                                break
                    # Check entity filter
                    watch_entities = wf.get("entity_names", [])
                    if watch_entities and node.get("entity_name"):
                        if node["entity_name"].lower() in [e.lower() for e in watch_entities]:
                            matched = True

                    if matched:
                        # Link node to thread
                        cur.execute("""
                            INSERT INTO thread_nodes (thread_id, node_id, auto_generated, relevance)
                            VALUES (%s, %s, TRUE, 'active')
                            ON CONFLICT (thread_id, node_id) DO NOTHING
                        """, (thread_id, node["node_id"]))
                        cur.execute("""
                            UPDATE chat_threads SET node_count = node_count + 1, updated_at = NOW()
                            WHERE thread_id = %s
                        """, (thread_id,))
                        summary_parts.append(f"fed to thread '{thread_title}'")

            conn.commit()
    except Exception as e:
        log.debug(f"Thread feeding error: {e}")

    # NOTE: Confidence cascading removed. Confidence is contextual — it belongs
    # on thread_nodes/case_nodes, not on knowledge_nodes. The edges (supports/contrasts)
    # created by the D stage are the durable artifacts. Scoring happens at query time
    # when a thread or case evaluates its graph.

    return {"summary": "; ".join(summary_parts) if summary_parts else "no utilization triggers", "updates": len(summary_parts)}


def _compute_evidence_quality(node: dict, contradictions: int = 0, corroborations: int = 0) -> float:
    """Compute baseline evidence quality (0.0-1.0).

    This measures the INTRINSIC quality of the node's evidence:
    how many sources, how authoritative, how complete.

    It does NOT factor in contextual relevance (that's per-thread/case).
    Contradiction/corroboration counts are noted but weighted lightly —
    the real impact of those is contextual.
    """
    score = 0.3  # base

    # Source count (intrinsic: how much evidence backs this?)
    source_count = node.get("source_count", 0)
    if source_count >= 3:
        score += 0.25
    elif source_count >= 2:
        score += 0.15
    elif source_count >= 1:
        score += 0.05

    # Authority (intrinsic: how authoritative is the best source?)
    score += _authority_score(node.get("source_authority", "unverified")) * 0.25

    # Completeness (intrinsic: does the node have location, time, entity?)
    if node.get("latitude"):
        score += 0.05
    if node.get("event_time"):
        score += 0.05
    if node.get("entity_name"):
        score += 0.05

    return max(0.0, min(1.0, round(score, 2)))


def _authority_score(authority: str) -> float:
    return {"verified": 1.0, "trusted": 0.75, "unverified": 0.25, "disputed": 0.0}.get(authority, 0.25)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _ensure_edge(source_id: str, target_id: str, relationship: str, evidence: str,
                 project_id: str, dsn: Optional[str] = None):
    """Create a knowledge edge if it doesn't exist.

    Uses the same deterministic edge_id scheme as gateway.admit_edge —
    sha1(source|target|relationship) truncated to 16 hex chars. Combined
    with ON CONFLICT (edge_id), concurrent writers converge on a single
    row instead of racing the (source,target,rel) UNIQUE vs the PK.
    """
    import hashlib
    edge_id = hashlib.sha1(
        f"{source_id}|{target_id}|{relationship}".encode()
    ).hexdigest()[:16]
    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO knowledge_edges (edge_id, project_id, source_node_id, target_node_id,
                                                 relationship, evidence, ai_generated)
                    VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                    ON CONFLICT (edge_id) DO NOTHING
                """, (edge_id, project_id, source_id, target_id,
                      relationship, evidence))
            conn.commit()
    except Exception as e:
        log.debug(f"Edge creation failed: {e}")


def _update_node(node_id: str, tenant_id: str, updates: dict, dsn: Optional[str] = None):
    """Update specific fields on a knowledge node."""
    if not updates:
        return
    sets = []
    params = []
    for key, val in updates.items():
        if key in ("enrichment_log", "confidence_factors", "place_context", "entity_identifiers"):
            sets.append(f"{key} = %s::jsonb")
        elif key in ("source_signals",):
            sets.append(f"{key} = %s")
        elif key in ("gaps", "source_types"):
            sets.append(f"{key} = %s")
        else:
            sets.append(f"{key} = %s")
        params.append(val)

    sets.append("updated_at = NOW()")
    params.extend([node_id, tenant_id])

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE knowledge_nodes SET {', '.join(sets)}
                WHERE node_id = %s AND tenant_id = %s
            """, params)
        conn.commit()


def _parse_jsonb(val) -> dict:
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    if isinstance(val, memoryview):
        return json.loads(bytes(val))
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _now() -> str:
    return datetime.utcnow().isoformat()


# ─── Batch Enrichment ────────────────────────────────────────────────────────

def enrich_pending_nodes(tenant_id: str, limit: int = 20, provider=None, dsn: Optional[str] = None) -> list[dict]:
    """Enrich all pending nodes for a tenant. Called by background worker."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT node_id FROM knowledge_nodes
                WHERE tenant_id = %s AND enrichment_status = 'pending'
                ORDER BY created_at ASC LIMIT %s
            """, (tenant_id, limit))
            node_ids = [r[0] for r in cur.fetchall()]

    results = []
    for node_id in node_ids:
        result = enrich_node(node_id, tenant_id, provider=provider, dsn=dsn)
        results.append(result)

    return results
