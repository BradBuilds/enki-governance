"""
GDU Enforcement Layer — Simplified gateway wrappers.

Every module that creates knowledge_nodes or entities must use these
functions instead of direct SQL. This is the single enforcement point.

Usage:
    from core.gdu.enforce import admit_entity, admit_node, admit_edge

    result = admit_entity(conn_or_dsn, tenant_id, "Acme Corp", "organization", signal_rid)
    result = admit_node(conn_or_dsn, tenant_id, "event", "meeting", "Board Meeting", "...", signal_rid)
"""

from __future__ import annotations

import logging
from typing import Optional

from core.gdu.gateway import get_gateway, AdmissionResult, EdgeResult

_log = logging.getLogger("enki.gdu.enforce")


def admit_entity(
    tenant_id: str,
    name: str,
    entity_type: str,
    signal_rid: int,
    identifiers: Optional[dict] = None,
    context: str = "",
    dsn: Optional[str] = None,
) -> AdmissionResult:
    """Admit an entity through the GDU gateway.

    This is the ONLY way to create or update an entity. Handles:
      - 5-layer dedup (identifier → name → alias → trigram → create)
      - Base-60 address assignment
      - gdu_canonical stamping
      - Hash-chained admission logging

    Returns AdmissionResult with entity_id, node_id, action (admit/collapse/route_ldup).
    """
    gw = get_gateway()
    try:
        result = gw.admit_entity(
            tenant_id=tenant_id,
            name=name,
            entity_type=entity_type,
            signal_rid=signal_rid,
            identifiers=identifiers,
            context=context,
            dsn=dsn,
        )
        if result.action == "route_ldup":
            _log.info("Entity routed to LDU-P: %s (type=%s)", name, entity_type)
        elif result.is_new:
            _log.debug("Entity admitted: %s → %s [%s]", name, result.node_id, result.base60_address)
        else:
            _log.debug("Entity collapsed: %s → %s", name, result.node_id)
        return result
    except Exception:
        _log.exception("Failed to admit entity: %s (type=%s)", name, entity_type)
        return AdmissionResult(action="error")


def admit_node(
    tenant_id: str,
    category: str,
    subtype: str,
    title: str,
    description: str,
    signal_rid: int,
    entity_id: Optional[int] = None,
    collapse_key: Optional[str] = None,
    metadata: Optional[dict] = None,
    dsn: Optional[str] = None,
) -> AdmissionResult:
    """Admit a knowledge node through the GDU gateway.

    This is the ONLY way to create or update a knowledge node. Handles:
      - Vocabulary validation against nodevocab
      - Collapse by collapse_key (merge duplicate events/topics/assessments)
      - Base-60 address assignment
      - gdu_canonical stamping
      - Hash-chained admission logging

    Returns AdmissionResult with node_id, action (admit/collapse/route_ldup).
    """
    gw = get_gateway()
    try:
        result = gw.admit_node(
            tenant_id=tenant_id,
            category=category,
            subtype=subtype,
            title=title,
            description=description,
            signal_rid=signal_rid,
            entity_id=entity_id,
            collapse_key=collapse_key,
            metadata=metadata,
            dsn=dsn,
        )
        if result.action == "route_ldup":
            _log.info("Node routed to LDU-P: %s (%s.%s)", title[:50], category, subtype)
        elif result.is_new:
            _log.debug("Node admitted: %s → %s [%s]", title[:50], result.node_id, result.base60_address)
        else:
            _log.debug("Node collapsed: %s → %s (count=%d)", title[:50], result.node_id, result.source_count)
        return result
    except Exception:
        _log.exception("Failed to admit node: %s (%s.%s)", title[:50], category, subtype)
        return AdmissionResult(action="error")


def admit_edge(
    tenant_id: str,
    source_node_id: str,
    target_node_id: str,
    relationship: str,
    evidence: str = "",
    signal_rid: int = 0,
    dsn: Optional[str] = None,
) -> EdgeResult:
    """Admit an edge through the GDU gateway.

    Creates or strengthens a relationship between two nodes. Handles:
      - Edge vocabulary validation
      - Strength accumulation on re-admission
      - Hash-chained admission logging

    Returns EdgeResult with edge_id, action (admit/strengthen/reject).
    """
    gw = get_gateway()
    try:
        result = gw.admit_edge(
            tenant_id=tenant_id,
            source_node_id=source_node_id,
            target_node_id=target_node_id,
            relationship=relationship,
            evidence=evidence,
            signal_rid=signal_rid,
            dsn=dsn,
        )
        return result
    except Exception:
        _log.exception("Failed to admit edge: %s → %s [%s]", source_node_id, target_node_id, relationship)
        return EdgeResult(action="error")
