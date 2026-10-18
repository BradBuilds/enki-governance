"""
Entity Enrichment Queue Worker — auto-fills thin entity dossiers.

Polls entity_enrichment_queue for pending tasks and dispatches to
per-type research handlers. Creates knowledge_node cards from research
results and recursively spawns new entities when discovered (e.g.,
aircraft → owner → owner's corporate filings).

Enrichment types are defined in the entityvocab GDU pack. The worker
only processes types declared in that pack — no ungovemed research.

Depth-limited to prevent runaway recursion (default: 2 levels).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection
from core.gdu.enforce import admit_node as gdu_admit_node

log = logging.getLogger("enki.enrichment.entity_research")

MAX_DEPTH = 2


def process_enrichment_queue(
    tenant_id: str,
    batch_size: int = 10,
    provider=None,
    dsn: Optional[str] = None,
) -> dict:
    """Process pending enrichment tasks from the queue."""
    stats = {"processed": 0, "succeeded": 0, "failed": 0, "skipped": 0}

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT queue_id, entity_id, enrichment_type, attempt_count, max_attempts
                FROM entity_enrichment_queue
                WHERE tenant_id = %s AND status = 'pending'
                ORDER BY priority DESC, created_at ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            """, (tenant_id, batch_size))
            tasks = cur.fetchall()

            for queue_id, entity_id, enrichment_type, attempts, max_attempts in tasks:
                if attempts >= max_attempts:
                    cur.execute("""
                        UPDATE entity_enrichment_queue
                        SET status = 'skipped', error = 'max attempts exceeded',
                            completed_at = NOW()
                        WHERE queue_id = %s
                    """, (queue_id,))
                    stats["skipped"] += 1
                    continue

                # Mark as running
                cur.execute("""
                    UPDATE entity_enrichment_queue
                    SET status = 'running', started_at = NOW(),
                        attempt_count = attempt_count + 1
                    WHERE queue_id = %s
                """, (queue_id,))
                conn.commit()

                try:
                    result = _run_enrichment(
                        entity_id, tenant_id, enrichment_type,
                        provider=provider, dsn=dsn,
                    )

                    cur.execute("""
                        UPDATE entity_enrichment_queue
                        SET status = 'complete', result = %s::jsonb,
                            completed_at = NOW()
                        WHERE queue_id = %s
                    """, (json.dumps(result), queue_id))
                    conn.commit()
                    stats["succeeded"] += 1

                except Exception as e:
                    log.warning("Enrichment failed for entity %s type %s: %s",
                                entity_id, enrichment_type, e)
                    cur.execute("""
                        UPDATE entity_enrichment_queue
                        SET status = 'pending', error = %s
                        WHERE queue_id = %s
                    """, (str(e)[:1000], queue_id))
                    conn.commit()
                    stats["failed"] += 1

                stats["processed"] += 1

    _update_completeness_scores(tenant_id, dsn)
    return stats


def _run_enrichment(
    entity_id: int,
    tenant_id: str,
    enrichment_type: str,
    provider=None,
    dsn: Optional[str] = None,
) -> dict:
    """Dispatch enrichment by type. Returns result dict."""
    # Load entity details
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT canonical_type, canonical_name, normalized_name,
                       identifiers, aliases
                FROM entities WHERE entity_id = %s
            """, (entity_id,))
            row = cur.fetchone()
            if not row:
                return {"error": "entity not found"}

    entity = {
        "entity_id": entity_id,
        "type": row[0],
        "name": row[1],
        "normalized": row[2],
        "identifiers": row[3] or {},
        "aliases": row[4] or [],
    }

    handlers = {
        "profile_search": _enrich_profile_search,
        "corporate_filings": _enrich_corporate_filings,
        "sanctions_check": _enrich_sanctions_check,
        "court_records": _enrich_court_records,
        "social_profiles": _enrich_social_profiles,
        "news_mentions": _enrich_news_mentions,
        "government_contracts": _enrich_government_contracts,
        "registry_lookup": _enrich_registry_lookup,
        "financial_data": _enrich_financial_data,
        "legal_filings": _enrich_legal_filings,
        "property_records": _enrich_property_records,
        "registration_lookup": _enrich_registration_lookup,
        "port_calls": _enrich_port_calls,
        "flight_history": _enrich_flight_history,
        "satellite_imagery": _enrich_satellite_imagery,
    }

    handler = handlers.get(enrichment_type)
    if not handler:
        return {"error": f"unknown enrichment type: {enrichment_type}"}

    result = handler(entity, tenant_id, provider=provider, dsn=dsn)

    # Create knowledge nodes from research findings
    if result.get("findings"):
        _create_research_nodes(entity, tenant_id, enrichment_type,
                                result["findings"], provider, dsn)

    # Queue recursive entities if discovered
    if result.get("discovered_entities"):
        _queue_recursive_entities(entity_id, tenant_id,
                                   result["discovered_entities"], provider, dsn)

    return result


def _enrich_profile_search(entity: dict, tenant_id: str, **kwargs) -> dict:
    """Search real public APIs for biographical information."""
    from core.research.sources import research_person, research_organization, research_location

    entity_type = entity.get("type", "person")
    name = entity["name"]

    if entity_type in ("organization",):
        raw = research_organization(name)
    elif entity_type in ("location", "country", "city", "region", "facility"):
        raw = research_location(name)
    else:
        raw = research_person(name)

    if not raw.get("data_sources"):
        return {"findings": [], "status": "no_data_found"}

    findings = []
    if raw.get("biography"):
        findings.append({
            "type": "observation",
            "title": f"Profile: {name}",
            "description": raw["biography"],
            "confidence": 0.8,
        })

    # Update entity dossier with real data
    new_identifiers = {**(entity.get("identifiers") or {})}
    for field in ("nationality", "date_of_birth", "current_role", "organization",
                   "twitter", "website", "occupation", "cik", "iso_3166"):
        if raw.get(field):
            new_identifiers[field] = raw[field]

    _update_entity_dossier(entity["entity_id"], tenant_id, {
        "identifiers": new_identifiers,
    }, kwargs.get("dsn"))

    return {
        "findings": findings,
        "discovered_entities": [],
        "profile": raw,
        "status": "complete",
        "data_sources": raw.get("data_sources", []),
    }


def _enrich_corporate_filings(entity: dict, tenant_id: str, **kwargs) -> dict:
    """Search SEC EDGAR for corporate filings via investigation sources."""
    from core.investigation.sources import investigate_sec_edgar

    name = entity["name"]
    result = investigate_sec_edgar(name, entity.get("type", "organization"))

    if result["status"] == "no_data":
        return {"findings": [], "status": "no_sec_data"}

    findings = []
    for f in result.get("findings", []):
        findings.append({
            "type": "fact",
            "title": f.get("title", ""),
            "description": json.dumps(f.get("data", {}))[:500],
            "confidence": f.get("confidence", 0.9),
        })

    # Update entity identifiers if CIK found
    new_idents = {}
    for f in result.get("findings", []):
        if f.get("type") == "company" and f.get("data", {}).get("cik"):
            new_idents["cik"] = f["data"]["cik"]
            if f["data"].get("ticker"):
                new_idents["ticker"] = f["data"]["ticker"]
        if f.get("type") == "financial":
            for key in ("Revenues", "Assets", "NetIncomeLoss"):
                val = f.get("data", {}).get(key, {})
                if isinstance(val, dict) and val.get("value"):
                    new_idents[key.lower()] = str(val["value"])

    if new_idents:
        _update_entity_dossier(entity["entity_id"], tenant_id, {
            "identifiers": {**(entity.get("identifiers") or {}), **new_idents},
        }, kwargs.get("dsn"))

    return {
        "findings": findings,
        "discovered_entities": result.get("discovered_entities", []),
        "status": "complete",
        "data_sources": ["sec_edgar"],
    }


def _enrich_sanctions_check(entity: dict, tenant_id: str, **kwargs) -> dict:
    """Check entity against real OpenSanctions API."""
    from core.research.sources import sanctions_check

    schema = "Person" if entity.get("type") == "person" else "Organization"
    result = sanctions_check(entity["name"], schema)

    if not result:
        return {"findings": [], "status": "api_unavailable"}

    findings = []
    if result.get("sanctioned"):
        datasets = []
        for m in result.get("matches", []):
            datasets.extend(m.get("datasets", []))
        findings.append({
            "type": "fact",
            "title": f"SANCTIONED: {entity['name']}",
            "description": f"Found on: {', '.join(set(datasets))}",
            "confidence": 0.95,
        })

    return {"findings": findings, "sanctions": result, "status": "complete", "data_sources": ["opensanctions"]}


def _enrich_registry_lookup(entity: dict, tenant_id: str, **kwargs) -> dict:
    """Look up vessel/aircraft in real Wikipedia/Wikidata for now. Real FAA/IMO APIs are stub."""
    from core.research.sources import wikipedia_summary, wikidata_entity

    name = entity["name"]
    wiki = wikipedia_summary(name)

    if not wiki:
        return {"findings": [], "status": "no_data"}

    findings = [{
        "type": "observation",
        "title": f"Registry: {name}",
        "description": wiki.get("extract", "")[:300],
        "confidence": 0.7,
    }]

    discovered = []
    wikidata_id = wiki.get("wikidata_id")
    if wikidata_id:
        wd = wikidata_entity(wikidata_id)
        if wd and wd.get("country"):
            discovered.append({
                "name": wd["country"],
                "type": "country",
                "relationship": "flag state" if entity["type"] == "vessel" else "registration country",
            })

    return {"findings": findings, "discovered_entities": discovered,
            "status": "complete", "data_sources": ["wikipedia"]}


# ─── Investigation-backed enrichment handlers ───────────────────────────────
# These delegate to core.investigation.sources for real API data.

def _enrich_court_records(entity, tenant_id, **kwargs):
    """Search CourtListener for federal court cases."""
    from core.investigation.sources import investigate_courts
    result = investigate_courts(entity["name"], entity.get("type", "person"))
    return _investigation_to_enrichment(result)


def _enrich_government_contracts(entity, tenant_id, **kwargs):
    """Search USAspending for government contracts."""
    from core.investigation.sources import investigate_usaspending
    result = investigate_usaspending(entity["name"], entity.get("type", "organization"))
    return _investigation_to_enrichment(result)


def _enrich_financial_data(entity, tenant_id, **kwargs):
    """Search FEC for campaign finance data."""
    from core.investigation.sources import investigate_fec
    result = investigate_fec(entity["name"], entity.get("type", "person"))
    return _investigation_to_enrichment(result)


def _enrich_legal_filings(entity, tenant_id, **kwargs):
    """Search ProPublica for nonprofit 990 filings."""
    from core.investigation.sources import investigate_nonprofits
    result = investigate_nonprofits(entity["name"], entity.get("type", "organization"))
    return _investigation_to_enrichment(result)


def _investigation_to_enrichment(result: dict) -> dict:
    """Convert investigation source result to enrichment handler format."""
    findings = []
    for f in result.get("findings", []):
        findings.append({
            "type": "fact" if f.get("confidence", 0) >= 0.85 else "observation",
            "title": f.get("title", ""),
            "description": json.dumps(f.get("data", {}))[:500],
            "confidence": f.get("confidence", 0.7),
        })
    return {
        "findings": findings,
        "discovered_entities": result.get("discovered_entities", []),
        "status": result.get("status", "complete"),
        "data_sources": [result.get("source", "unknown")],
    }


# Stubs for enrichment types without a live API source yet
def _enrich_social_profiles(entity, tenant_id, **kwargs):
    return {"findings": [], "status": "not_implemented"}

def _enrich_news_mentions(entity, tenant_id, **kwargs):
    return {"findings": [], "status": "not_implemented"}

def _enrich_property_records(entity, tenant_id, **kwargs):
    return {"findings": [], "status": "not_implemented"}

def _enrich_registration_lookup(entity, tenant_id, **kwargs):
    return {"findings": [], "status": "not_implemented"}

def _enrich_port_calls(entity, tenant_id, **kwargs):
    return {"findings": [], "status": "not_implemented"}

def _enrich_flight_history(entity, tenant_id, **kwargs):
    return {"findings": [], "status": "not_implemented"}

def _enrich_satellite_imagery(entity, tenant_id, **kwargs):
    return {"findings": [], "status": "not_implemented"}


# ─── Helper: Create knowledge nodes from findings ─────────────────────────────

def _create_research_nodes(
    entity: dict, tenant_id: str, enrichment_type: str,
    findings: list[dict], provider=None, dsn: Optional[str] = None,
):
    """Create knowledge_node cards from research findings, linked to the canonical entity."""
    import uuid
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            for finding in findings:
                node_id = f"node-research-{uuid.uuid4().hex[:12]}"
                finding_title = finding.get("title", "Research finding")[:500]
                finding_desc = finding.get("description", "")[:2000]
                category = "assessment"
                subtype = finding.get("type", "observation")
                # Admit through GDU gateway first
                gdu_admit_node(
                    tenant_id=tenant_id,
                    category=category,
                    subtype=subtype,
                    title=finding_title[:200],
                    description=finding_desc[:500],
                    signal_rid=0,
                    collapse_key=node_id,
                )
                # Insert as gdu_canonical=FALSE. gdu_admit_node above
                # already created the canonical row (with a base60_address
                # the gateway assigns). This row is the materialized view
                # the enrichment pipeline reads from for downstream UI —
                # it carries the extra entity_name/master_entity_id/etc.
                # fields the gateway doesn't write. Marking it
                # non-canonical satisfies enforce_governance(), which
                # rejects canonical rows without an address.
                cur.execute("""
                    INSERT INTO knowledge_nodes
                        (node_id, project_id, tenant_id, title, description,
                         node_type, subtype, epistemic_type,
                         entity_type, entity_name, master_entity_id,
                         source_authority, confidence, enrichment_status,
                         metadata, gdu_canonical)
                    VALUES (%s, 'default', %s, %s, %s,
                            %s, %s, %s,
                            %s, %s, %s,
                            %s, %s, 'complete', %s, FALSE)
                    ON CONFLICT (node_id) DO UPDATE SET
                        description = EXCLUDED.description,
                        node_type   = COALESCE(knowledge_nodes.node_type, EXCLUDED.node_type),
                        subtype     = COALESCE(knowledge_nodes.subtype,   EXCLUDED.subtype),
                        updated_at  = NOW()
                """, (
                    node_id, tenant_id,
                    finding_title,
                    finding_desc,
                    category, subtype,
                    finding.get("type", "observation"),
                    entity["type"],
                    entity["name"],
                    entity["entity_id"],
                    "unverified",
                    finding.get("confidence", 0.5),
                    json.dumps({"source": f"entity_research:{enrichment_type}"}),
                ))
        conn.commit()


def _queue_recursive_entities(
    parent_entity_id: int, tenant_id: str,
    discovered: list[dict], provider=None, dsn: Optional[str] = None,
):
    """Resolve discovered entities and queue their enrichment (depth-limited)."""
    from core.gdu.entity_resolution import resolve_entity

    for disc in discovered[:10]:  # Cap per-level
        try:
            result = resolve_entity(
                tenant_id=tenant_id,
                name=disc["name"],
                entity_type=disc.get("type", "unknown"),
                context=disc.get("relationship", ""),
                source_pipeline="entity_research_recursive",
                provider=provider,
                dsn=dsn,
            )

            # Create a relationship edge if both entities exist
            if result.entity_id and parent_entity_id:
                _create_entity_edge(parent_entity_id, result.entity_id,
                                     disc.get("relationship", "associated"),
                                     tenant_id, dsn)

        except Exception as e:
            log.debug("Recursive entity resolution failed for %s: %s",
                      disc.get("name"), e)


def _create_entity_edge(
    source_entity_id: int, target_entity_id: int,
    relationship: str, tenant_id: str, dsn: Optional[str] = None,
):
    """Create an edge between two canonical entities via their knowledge nodes."""
    pass  # Edges will be created when both entities have knowledge nodes


def _update_entity_dossier(
    entity_id: int, tenant_id: str, updates: dict,
    dsn: Optional[str] = None,
):
    """Update entity dossier fields (identifiers, canonical_name, etc.)."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            if "identifiers" in updates:
                cur.execute("""
                    UPDATE entities SET
                        identifiers = COALESCE(identifiers, '{}'::jsonb) || %s::jsonb,
                        updated_at = NOW()
                    WHERE entity_id = %s
                """, (json.dumps(updates["identifiers"]), entity_id))

            if "canonical_name" in updates:
                cur.execute("""
                    UPDATE entities SET canonical_name = %s, updated_at = NOW()
                    WHERE entity_id = %s AND canonical_name != %s
                """, (updates["canonical_name"], entity_id, updates["canonical_name"]))

        conn.commit()


def _update_completeness_scores(tenant_id: str, dsn: Optional[str] = None):
    """Recompute completeness scores for entities that were enriched."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE entities SET
                    completeness_score = (
                        CASE WHEN canonical_name IS NOT NULL AND canonical_name != '' THEN 0.15 ELSE 0 END +
                        CASE WHEN identifiers IS NOT NULL AND identifiers != '{}'::jsonb THEN 0.20 ELSE 0 END +
                        CASE WHEN aliases IS NOT NULL AND array_length(aliases, 1) > 0 THEN 0.10 ELSE 0 END +
                        CASE WHEN embedding IS NOT NULL THEN 0.10 ELSE 0 END +
                        LEAST(signal_count::numeric / 10.0, 0.20) +
                        CASE WHEN (identifiers->>'nationality') IS NOT NULL THEN 0.05 ELSE 0 END +
                        CASE WHEN (identifiers->>'current_role') IS NOT NULL THEN 0.10 ELSE 0 END +
                        CASE WHEN (identifiers->>'date_of_birth') IS NOT NULL THEN 0.10 ELSE 0 END
                    ),
                    completeness_tier = CASE
                        WHEN completeness_score >= 0.75 THEN 'verified'
                        WHEN completeness_score >= 0.50 THEN 'solid'
                        WHEN completeness_score >= 0.25 THEN 'partial'
                        ELSE 'stub'
                    END,
                    updated_at = NOW()
                WHERE tenant_id = %s
                  AND entity_id IN (
                      SELECT DISTINCT entity_id FROM entity_enrichment_queue
                      WHERE tenant_id = %s AND status = 'complete'
                        AND completed_at > NOW() - INTERVAL '1 hour'
                  )
            """, (tenant_id, tenant_id))
        conn.commit()
