"""
Entity Resolver — deduplication gateway for knowledge nodes.

EVERY node creation must go through this resolver. It prevents duplicates
by finding existing nodes that represent the same entity.

Resolution strategies (in order):
  1. Exact identifier match (MMSI, VIN, passport #, email — canonical IDs)
  2. Normalized name match (case-insensitive, abbreviation-expanded)
  3. Fuzzy name + proximity match (similar name + near same location)

Returns:
  - MERGE: existing node found, update it with new data
  - CREATE: no match, safe to create new node
  - REVIEW: possible match but uncertain, create with same_concept edge

This is the single chokepoint that prevents node explosion.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.resolver")

# Identifier types that are globally unique — exact match = same entity
CANONICAL_ID_TYPES = {
    "mmsi", "imo", "vin", "icao_hex", "faa_registration", "passport",
    "ssn", "ein", "drivers_license", "email", "crypto_wallet",
}


class ResolveResult:
    """Result of entity resolution."""
    def __init__(self, action: str, node_id: str = None, confidence: float = 0.0,
                 match_reason: str = ""):
        self.action = action        # "merge", "create", "review"
        self.node_id = node_id      # existing node_id if merge/review
        self.confidence = confidence # match confidence (0-1)
        self.match_reason = match_reason


def resolve_entity(
    tenant_id: str,
    entity_name: str = None,
    entity_type: str = None,
    entity_identifiers: dict = None,
    latitude: float = None,
    longitude: float = None,
    dsn: Optional[str] = None,
) -> ResolveResult:
    """
    Find an existing knowledge node for this entity.

    This is the ONLY path to deciding whether to create a new node or update
    an existing one. Every node creation function must call this first.
    """
    identifiers = entity_identifiers or {}

    # Strategy 1: Exact identifier match (highest confidence)
    result = _match_by_identifier(tenant_id, identifiers, dsn)
    if result:
        return result

    # Strategy 2: Normalized name match
    if entity_name:
        result = _match_by_name(tenant_id, entity_name, entity_type, dsn)
        if result:
            return result

    # Strategy 3: Fuzzy name + spatial proximity
    if entity_name and latitude is not None:
        result = _match_by_name_and_location(tenant_id, entity_name, entity_type,
                                              latitude, longitude, dsn)
        if result:
            return result

    # No match — safe to create
    return ResolveResult(action="create")


def _match_by_identifier(tenant_id: str, identifiers: dict, dsn=None) -> Optional[ResolveResult]:
    """Match by canonical identifiers. MMSI, VIN, passport = definitive match."""
    if not identifiers:
        return None

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            for key, value in identifiers.items():
                if not value:
                    continue
                normalized_key = key.lower().strip()
                normalized_value = str(value).strip().lower()

                # Check entity_identifiers table first
                cur.execute("""
                    SELECT ei.node_id, ei.id_type, kn.title
                    FROM entity_identifiers ei
                    JOIN knowledge_nodes kn ON kn.node_id = ei.node_id
                    WHERE ei.tenant_id = %s AND ei.node_id IS NOT NULL
                      AND LOWER(ei.id_type) = %s AND LOWER(ei.id_number) = %s
                    LIMIT 1
                """, (tenant_id, normalized_key, normalized_value))
                row = cur.fetchone()
                if row:
                    return ResolveResult(
                        action="merge", node_id=row[0], confidence=1.0,
                        match_reason=f"identifier match: {key}={value}",
                    )

                # Check knowledge_nodes entity_identifiers JSONB
                # Use containment operator for JSONB key match
                cur.execute("""
                    SELECT node_id, title FROM knowledge_nodes
                    WHERE tenant_id = %s
                      AND entity_identifiers IS NOT NULL
                      AND entity_identifiers != '{}'::jsonb
                      AND LOWER(entity_identifiers->>%s) = %s
                    LIMIT 1
                """, (tenant_id, key, normalized_value))
                row = cur.fetchone()
                if row:
                    is_canonical = normalized_key in CANONICAL_ID_TYPES
                    return ResolveResult(
                        action="merge" if is_canonical else "review",
                        node_id=row[0],
                        confidence=1.0 if is_canonical else 0.85,
                        match_reason=f"identifier match: {key}={value}",
                    )

    return None


def _match_by_name(tenant_id: str, entity_name: str, entity_type: str = None,
                    dsn=None) -> Optional[ResolveResult]:
    """Match by normalized entity name. Handles abbreviations and case."""
    normalized = _normalize_name(entity_name)
    if not normalized or len(normalized) < 3:
        return None

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Exact normalized match
            type_filter = "AND entity_type = %s" if entity_type else ""
            params = [tenant_id, normalized]
            if entity_type:
                params.append(entity_type)

            cur.execute(f"""
                SELECT node_id, entity_name, entity_type, title
                FROM knowledge_nodes
                WHERE tenant_id = %s AND LOWER(entity_name) = %s
                {type_filter}
                LIMIT 1
            """, params)
            row = cur.fetchone()
            if row:
                return ResolveResult(
                    action="merge", node_id=row[0], confidence=0.95,
                    match_reason=f"exact name match: '{entity_name}' = '{row[1]}'",
                )

            # Abbreviation-expanded match (LLC, Inc, Corp, Ltd, Co)
            stripped = _strip_suffixes(normalized)
            if stripped != normalized:
                params2 = [tenant_id, f"{stripped}%"]
                if entity_type:
                    params2.append(entity_type)
                cur.execute(f"""
                    SELECT node_id, entity_name, title
                    FROM knowledge_nodes
                    WHERE tenant_id = %s AND LOWER(entity_name) LIKE %s
                    {type_filter}
                    ORDER BY LENGTH(entity_name) ASC LIMIT 3
                """, params2)
                for row in cur.fetchall():
                    if _strip_suffixes(row[1].lower()) == stripped:
                        return ResolveResult(
                            action="merge", node_id=row[0], confidence=0.9,
                            match_reason=f"name match (suffix-normalized): '{entity_name}' ≈ '{row[1]}'",
                        )

    return None


def _match_by_name_and_location(
    tenant_id: str, entity_name: str, entity_type: str,
    latitude: float, longitude: float, dsn=None,
) -> Optional[ResolveResult]:
    """Fuzzy name match + spatial proximity = probable same entity."""
    normalized = _normalize_name(entity_name)
    if not normalized:
        return None

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Trigram similarity would be ideal but we'll use LIKE with prefix
            prefix = normalized[:min(len(normalized), 10)]
            type_filter = "AND entity_type = %s" if entity_type else ""
            params = [tenant_id, f"{prefix}%", longitude, latitude]
            if entity_type:
                params.append(entity_type)

            cur.execute(f"""
                SELECT node_id, entity_name, latitude, longitude,
                       ST_Distance(
                           ST_MakePoint(longitude, latitude)::geography,
                           ST_MakePoint(%s, %s)::geography
                       ) as dist_m
                FROM knowledge_nodes
                WHERE tenant_id = %s AND LOWER(entity_name) LIKE %s
                  AND latitude IS NOT NULL
                  AND ST_DWithin(
                      ST_MakePoint(longitude, latitude)::geography,
                      ST_MakePoint(%s, %s)::geography,
                      50000)
                {type_filter}
                ORDER BY dist_m ASC LIMIT 3
            """, [tenant_id, f"{prefix}%", longitude, latitude, longitude, latitude] +
                 ([entity_type] if entity_type else []))

            for row in cur.fetchall():
                name_sim = _name_similarity(normalized, row[1].lower())
                if name_sim > 0.7:
                    return ResolveResult(
                        action="review" if name_sim < 0.9 else "merge",
                        node_id=row[0],
                        confidence=name_sim * 0.9,
                        match_reason=f"name+location: '{entity_name}' ≈ '{row[1]}' ({row[4]:.0f}m apart)",
                    )

    return None


# ─── Name Normalization ──────────────────────────────────────────────────────

_SUFFIXES = [
    " llc", " inc", " corp", " corporation", " ltd", " limited",
    " co", " company", " group", " holdings", " partners", " lp",
    " plc", " gmbh", " ag", " sa", " srl", " bv", " nv",
]

def _normalize_name(name: str) -> str:
    if not name:
        return ""
    return " ".join(name.lower().strip().split())

def _strip_suffixes(name: str) -> str:
    result = name.lower().strip()
    for suffix in _SUFFIXES:
        if result.endswith(suffix):
            result = result[:-len(suffix)].strip().rstrip(",").rstrip(".")
    return result

def _name_similarity(a: str, b: str) -> float:
    """Simple character-level similarity (Jaccard on character bigrams)."""
    if not a or not b:
        return 0.0
    a_stripped = _strip_suffixes(a)
    b_stripped = _strip_suffixes(b)
    if a_stripped == b_stripped:
        return 1.0
    a_bigrams = set(a_stripped[i:i+2] for i in range(len(a_stripped) - 1))
    b_bigrams = set(b_stripped[i:i+2] for i in range(len(b_stripped) - 1))
    if not a_bigrams or not b_bigrams:
        return 0.0
    intersection = len(a_bigrams & b_bigrams)
    union = len(a_bigrams | b_bigrams)
    return intersection / union if union > 0 else 0.0


# ─── Update Existing Node ───────────────────────────────────────────────────

def merge_into_node(
    node_id: str,
    tenant_id: str,
    new_source_signals: list[int] = None,
    new_source_thread: str = None,
    new_description: str = None,
    new_identifiers: dict = None,
    new_latitude: float = None,
    new_longitude: float = None,
    new_event_time: str = None,
    dsn: Optional[str] = None,
) -> bool:
    """Merge new data into an existing node (append sources, update fields)."""
    updates = []
    params = []

    if new_source_signals:
        updates.append("source_signals = ARRAY(SELECT DISTINCT unnest(COALESCE(source_signals, '{}') || %s))")
        params.append(new_source_signals)
        updates.append("source_count = (SELECT COUNT(DISTINCT v) FROM unnest(COALESCE(source_signals, '{}') || %s) v)")
        params.append(new_source_signals)

    if new_source_thread:
        updates.append("source_threads = ARRAY(SELECT DISTINCT unnest(COALESCE(source_threads, '{}') || %s))")
        params.append([new_source_thread])

    if new_identifiers:
        updates.append("entity_identifiers = COALESCE(entity_identifiers, '{}'::jsonb) || %s::jsonb")
        params.append(json.dumps(new_identifiers))

    if new_latitude is not None:
        updates.append("latitude = %s")
        params.append(new_latitude)
        updates.append("longitude = %s")
        params.append(new_longitude)

    if new_event_time:
        updates.append("event_time = GREATEST(event_time, %s::timestamptz)")
        params.append(new_event_time)

    if new_description:
        # Append to description if it adds new info
        updates.append("description = CASE WHEN description IS NULL THEN %s ELSE description || E'\\n' || %s END")
        params.extend([new_description, new_description])

    if not updates:
        return False

    updates.append("updated_at = NOW()")
    updates.append("enrichment_status = 'pending'")  # re-enrich after merge
    params.extend([node_id, tenant_id])

    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    UPDATE knowledge_nodes SET {', '.join(updates)}
                    WHERE node_id = %s AND tenant_id = %s
                """, params)
            conn.commit()
        return True
    except Exception as e:
        log.warning(f"Merge failed for {node_id}: {e}")
        return False
