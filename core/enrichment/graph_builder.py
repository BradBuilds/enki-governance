"""
Graph Builder — Creates knowledge nodes and edges from entity resolution results.

When entities are resolved (companies, people, aircraft, locations), this module
persists them as knowledge graph nodes with edges connecting them. It also detects
cross-case references: if an entity already exists in another case, it flags the
overlap for intelligence correlation.

Every resolved entity becomes:
1. A knowledge node (persistent in DB)
2. An edge to related entities
3. A case observable (for auto-correlation)
4. A cross-case reference check
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection
from core.gdu.enforce import admit_node as gdu_admit_node

logger = logging.getLogger("enki.graph_builder")


def build_entity_graph(
    resolution_result: dict,
    entity_type: str,
    entity_value: str,
    project_id: str,
    tenant_id: str,
    case_id: Optional[str] = None,
    dsn: Optional[str] = None,
) -> dict:
    """
    Take entity resolution results and build a knowledge graph.

    Creates nodes for each discovered entity, edges between them,
    observables on the case, and checks for cross-case overlaps.

    Returns:
        {
            "nodes_created": [...],
            "edges_created": [...],
            "observables_added": [...],
            "cross_case_hits": [...]
        }
    """
    nodes_created = []
    edges_created = []
    observables_added = []
    cross_case_hits = []

    # Create the root node (the seed entity)
    root_node_id = _create_node(
        project_id=project_id,
        tenant_id=tenant_id,
        title=entity_value,
        epistemic_type=entity_type,
        description=f"Investigation target: {entity_value}",
        metadata={"resolution": resolution_result, "entity_type": entity_type},
        latitude=resolution_result.get("latitude"),
        longitude=resolution_result.get("longitude"),
        dsn=dsn,
    )
    if root_node_id:
        nodes_created.append({"node_id": root_node_id, "title": entity_value, "type": entity_type})

    # Process companies discovered
    for company in resolution_result.get("companies", []):
        company_node_id = _create_node(
            project_id=project_id,
            tenant_id=tenant_id,
            title=company.get("name", "Unknown Company"),
            epistemic_type="company",
            description=_build_company_description(company),
            metadata={"entity_data": company},
            concepts=_extract_company_concepts(company),
            dsn=dsn,
        )
        if company_node_id:
            nodes_created.append({"node_id": company_node_id, "title": company["name"], "type": "company"})

            # Edge: root → company
            if root_node_id:
                edge_type = "located_at" if entity_type == "location" else "associated_with"
                _create_edge(project_id, root_node_id, company_node_id, edge_type, dsn=dsn)
                edges_created.append({"from": entity_value, "to": company["name"], "type": edge_type})

            # Add observable
            if case_id:
                _add_observable(case_id, tenant_id, "company_name", company["name"], dsn=dsn)
                observables_added.append({"type": "company_name", "value": company["name"]})

            # GLEIF LEI
            if company.get("gleif", {}).get("lei"):
                lei = company["gleif"]["lei"]
                if case_id:
                    _add_observable(case_id, tenant_id, "lei", lei, dsn=dsn)
                    observables_added.append({"type": "lei", "value": lei})

            # SEC CIK
            if company.get("cik"):
                if case_id:
                    _add_observable(case_id, tenant_id, "sec_cik", company["cik"], dsn=dsn)

            # Aircraft registered to company
            for aircraft in company.get("aircraft", []):
                ac_node_id = _create_node(
                    project_id=project_id,
                    tenant_id=tenant_id,
                    title=f"Aircraft {aircraft.get('n_number', '?')}",
                    epistemic_type="aircraft",
                    description=f"{aircraft.get('model', '?')} registered to {company['name']}",
                    metadata={"aircraft": aircraft},
                    dsn=dsn,
                )
                if ac_node_id:
                    nodes_created.append({"node_id": ac_node_id, "title": aircraft.get("n_number"), "type": "aircraft"})
                    _create_edge(project_id, ac_node_id, company_node_id, "registered_to", dsn=dsn)
                    edges_created.append({"from": aircraft["n_number"], "to": company["name"], "type": "registered_to"})

                    if case_id:
                        _add_observable(case_id, tenant_id, "aircraft_registration", aircraft["n_number"], dsn=dsn)
                        observables_added.append({"type": "aircraft_registration", "value": aircraft["n_number"]})

            # Court cases
            for legal_case in company.get("legal_cases", []):
                case_node_id = _create_node(
                    project_id=project_id,
                    tenant_id=tenant_id,
                    title=legal_case.get("case_name", "Court Case"),
                    epistemic_type="document",
                    description=f"{legal_case.get('court', '')} · Filed: {legal_case.get('date_filed', '')}",
                    metadata={"legal_case": legal_case},
                    dsn=dsn,
                )
                if case_node_id:
                    nodes_created.append({"node_id": case_node_id, "title": legal_case.get("case_name"), "type": "document"})
                    _create_edge(project_id, company_node_id, case_node_id, "party_to", dsn=dsn)

            # Cross-case check: does this company appear in other cases?
            cross = _check_cross_case(tenant_id, "company_name", company["name"], case_id, dsn=dsn)
            if cross:
                cross_case_hits.extend(cross)

    # Process standalone entity resolution fields (company type)
    if entity_type == "company" and root_node_id:
        # SEC data on root
        sec = resolution_result.get("sec", {})
        if sec:
            _update_node_metadata(root_node_id, {"sec": sec}, dsn=dsn)

        financials = resolution_result.get("financials", {})
        if financials:
            _update_node_metadata(root_node_id, {"financials": financials}, dsn=dsn)

        gleif = resolution_result.get("gleif", {})
        if gleif:
            _update_node_metadata(root_node_id, {"gleif": gleif}, dsn=dsn)
            if gleif.get("lei") and case_id:
                _add_observable(case_id, tenant_id, "lei", gleif["lei"], dsn=dsn)
                observables_added.append({"type": "lei", "value": gleif["lei"]})

        contracts = resolution_result.get("contracts", {})
        if contracts:
            _update_node_metadata(root_node_id, {"contracts": contracts}, dsn=dsn)

        # Legal cases
        for legal in resolution_result.get("legal", []):
            legal_node = _create_node(
                project_id=project_id, tenant_id=tenant_id,
                title=legal.get("case_name", "Court Case"),
                epistemic_type="document",
                description=f"{legal.get('court', '')} · {legal.get('date_filed', '')}",
                metadata={"legal_case": legal}, dsn=dsn,
            )
            if legal_node:
                nodes_created.append({"node_id": legal_node, "title": legal.get("case_name"), "type": "document"})
                _create_edge(project_id, root_node_id, legal_node, "party_to", dsn=dsn)

        # Cross-case
        cross = _check_cross_case(tenant_id, "company_name", entity_value, case_id, dsn=dsn)
        if cross:
            cross_case_hits.extend(cross)

    # Process person type
    if entity_type == "person" and root_node_id:
        for finra in resolution_result.get("finra", []):
            _update_node_metadata(root_node_id, {"finra": finra}, dsn=dsn)

        for legal in resolution_result.get("legal", []):
            legal_node = _create_node(
                project_id=project_id, tenant_id=tenant_id,
                title=legal.get("case_name", "Court Case"),
                epistemic_type="document",
                metadata={"legal_case": legal}, dsn=dsn,
            )
            if legal_node:
                nodes_created.append({"node_id": legal_node, "type": "document"})
                _create_edge(project_id, root_node_id, legal_node, "party_to", dsn=dsn)

        if case_id:
            _add_observable(case_id, tenant_id, "person_name", entity_value, dsn=dsn)
            observables_added.append({"type": "person_name", "value": entity_value})

        cross = _check_cross_case(tenant_id, "person_name", entity_value, case_id, dsn=dsn)
        if cross:
            cross_case_hits.extend(cross)

    return {
        "root_node_id": root_node_id,
        "nodes_created": nodes_created,
        "edges_created": edges_created,
        "observables_added": observables_added,
        "cross_case_hits": cross_case_hits,
    }


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _create_node(
    project_id: str, tenant_id: str, title: str, epistemic_type: str,
    description: str = None, metadata: dict = None, concepts: list = None,
    latitude: float = None, longitude: float = None, dsn=None,
) -> Optional[str]:
    """Create a knowledge node. Returns node_id or None."""
    node_id = f"node_{uuid.uuid4().hex[:12]}"
    search_text = f"{title} {description or ''} {' '.join(concepts or [])}"

    # Admit through GDU gateway
    gdu_admit_node(
        tenant_id=tenant_id,
        category=epistemic_type if epistemic_type in ("fact", "observation", "claim", "hypothesis") else "assessment",
        subtype=epistemic_type or "observation",
        title=title[:200],
        description=(description or "")[:500],
        signal_rid=0,
        collapse_key=node_id,
    )

    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO knowledge_nodes
                        (node_id, project_id, tenant_id, title, description,
                         epistemic_type, concepts, latitude, longitude,
                         metadata, search_vector, depth, gdu_canonical)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            to_tsvector('english', %s), 0, TRUE)
                    ON CONFLICT (node_id) DO UPDATE SET
                        title = EXCLUDED.title, description = EXCLUDED.description,
                        gdu_canonical = TRUE, updated_at = NOW()
                """, (
                    node_id, project_id, tenant_id, title, description,
                    epistemic_type, concepts or [], latitude, longitude,
                    json.dumps(metadata or {}), search_text,
                ))
            conn.commit()
        return node_id
    except Exception as e:
        logger.warning(f"Failed to create node: {e}")
        return None


def _create_edge(
    project_id: str, source_id: str, target_id: str, relationship: str,
    strength: float = 0.8, dsn=None,
) -> bool:
    """Create an edge between two nodes."""
    edge_id = f"edge_{uuid.uuid4().hex[:12]}"
    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO knowledge_edges
                        (edge_id, project_id, source_node_id, target_node_id, relationship, strength)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (source_node_id, target_node_id, relationship) DO NOTHING
                """, (edge_id, project_id, source_id, target_id, relationship, strength))
            conn.commit()
        return True
    except Exception:
        return False


def _add_observable(
    case_id: str, tenant_id: str, obs_type: str, obs_value: str, dsn=None,
) -> bool:
    """Add an observable to a case watchlist."""
    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO case_observables
                        (case_id, tenant_id, observable_type, observable_value,
                         normalized_value, added_by, notes)
                    VALUES (%s, %s, %s, %s, %s, 'graph_builder', 'Auto-discovered from entity resolution')
                    ON CONFLICT (case_id, observable_type, normalized_value) DO NOTHING
                """, (case_id, tenant_id, obs_type, obs_value, obs_value.lower().strip()))
            conn.commit()
        return True
    except Exception:
        return False


def _update_node_metadata(node_id: str, new_data: dict, dsn=None):
    """Merge new data into a node's metadata."""
    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT metadata FROM knowledge_nodes WHERE node_id = %s", (node_id,))
                row = cur.fetchone()
                if not row:
                    return
                existing = row[0]
                if isinstance(existing, memoryview):
                    existing = json.loads(bytes(existing))
                elif isinstance(existing, str):
                    existing = json.loads(existing)
                elif existing is None:
                    existing = {}
                existing.update(new_data)
                cur.execute("UPDATE knowledge_nodes SET metadata = %s, updated_at = NOW() WHERE node_id = %s",
                           (json.dumps(existing), node_id))
            conn.commit()
    except Exception:
        pass


def _check_cross_case(
    tenant_id: str, obs_type: str, obs_value: str,
    exclude_case_id: Optional[str] = None, dsn=None,
) -> list[dict]:
    """Check if an observable appears in other cases (cross-case correlation)."""
    hits = []
    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT co.case_id, c.title, c.status, c.priority, co.observable_type
                    FROM case_observables co
                    JOIN cases c ON c.case_id = co.case_id AND c.tenant_id = co.tenant_id
                    WHERE co.tenant_id = %s
                      AND co.observable_type = %s
                      AND co.normalized_value = %s
                      AND co.active = TRUE
                      AND c.status IN ('open', 'investigating', 'awaiting_input')
                      AND co.case_id != %s
                """, (tenant_id, obs_type, obs_value.lower().strip(), exclude_case_id or ''))
                for r in cur.fetchall():
                    hits.append({
                        "case_id": r[0], "case_title": r[1],
                        "case_status": r[2], "case_priority": r[3],
                        "match_type": r[4], "match_value": obs_value,
                    })
    except Exception:
        pass
    return hits


def _build_company_description(company: dict) -> str:
    """Build a human-readable description for a company node."""
    parts = []
    if company.get("sic_description"):
        parts.append(company["sic_description"])
    if company.get("state"):
        parts.append(f"Incorporated in {company['state']}")
    if company.get("financials", {}).get("revenue"):
        rev = company["financials"]["revenue"]
        if rev >= 1e9:
            parts.append(f"Revenue: ${rev/1e9:.1f}B")
        elif rev >= 1e6:
            parts.append(f"Revenue: ${rev/1e6:.1f}M")
    if company.get("government_contracts", {}).get("total_amount"):
        parts.append(f"Gov contracts: ${company['government_contracts']['total_amount']:,.0f}")
    return " · ".join(parts) if parts else "Company"


def _extract_company_concepts(company: dict) -> list[str]:
    """Extract searchable concepts from company data."""
    concepts = []
    if company.get("name"):
        concepts.append(company["name"].lower())
    if company.get("sic_description"):
        concepts.extend(company["sic_description"].lower().split())
    if company.get("state"):
        concepts.append(company["state"].lower())
    if company.get("gleif", {}).get("country"):
        concepts.append(company["gleif"]["country"].lower())
    return list(set(concepts))[:10]
