"""
GDU Completeness Scoring

Computes completeness scores from the slot specifications defined in
completenessvocab.slot_specs.json. Each node type has required slots
(denominator) and optional slots (bonus). The score determines the
tier: stub, partial, solid, or verified.

Called by the gateway after every admit_node() to keep scores current.

Tier thresholds (from completenessvocab.tiers.json):
  stub:     0.00 - 0.39
  partial:  0.40 - 0.69
  solid:    0.70 - 0.89
  verified: 0.90 - 1.00 (also requires ≥1 corroboration, 0 contradictions)
"""

from __future__ import annotations

import logging
from typing import Optional

_log = logging.getLogger("enki.gdu.completeness")


def compute_score(
    node_type: str,
    subtype: str,
    data: dict,
) -> tuple[float, str]:
    """Compute completeness score and tier for a node.

    Args:
        node_type: Node category (entity, event, document, assessment, etc.)
        subtype: Node subtype (person, strike, article, claim, etc.)
        data: Dict of node data to check against slot specs. Keys should
              use dot notation matching the spec (e.g., "identity.title_display").
              For flat records, the function also checks top-level keys.

    Returns:
        (score, tier) — score is 0.0-1.0, tier is stub/partial/solid/verified
    """
    from core.gdu.pack_registry import get_slot_specs

    specs = get_slot_specs()
    all_specs = specs.get("specs", {})

    # Look up spec for this node type
    spec_key = f"{node_type}.{subtype}"
    spec = all_specs.get(spec_key)

    if not spec:
        # No spec defined — use a generic score based on filled fields
        filled = sum(1 for v in data.values() if v is not None and v != "" and v != [])
        total = max(len(data), 1)
        score = min(filled / total, 1.0)
        return (round(score, 2), _score_to_tier(score))

    required_slots = spec.get("required_slots", [])
    optional_slots = spec.get("optional_slots", [])

    if not required_slots:
        return (0.5, "partial")  # No requirements defined, default to partial

    # Count filled required slots
    filled_required = 0
    for slot in required_slots:
        if _slot_filled(data, slot):
            filled_required += 1

    total_required = len(required_slots)
    base_score = filled_required / total_required if total_required > 0 else 0.0

    # Optional slot bonus (capped at +0.15)
    filled_optional = 0
    for slot in optional_slots:
        if _slot_filled(data, slot):
            filled_optional += 1

    optional_bonus = min(filled_optional * 0.05, 0.15)
    score = min(base_score + optional_bonus, 1.0)

    return (round(score, 2), _score_to_tier(score))


def score_node(node_id: str, tenant_id: str = "default",
               dsn: Optional[str] = None) -> Optional[tuple[float, str]]:
    """Compute and persist completeness score for an existing knowledge_node.

    Reads the node from DB, computes score, writes back.
    Returns (score, tier) or None if node not found.
    """
    from storage.postgres.db import get_connection

    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT node_type, subtype, title, description,
                           confidence, source_count, entity_name,
                           master_entity_id, signal_rid,
                           base60_address, enrichment_status
                    FROM knowledge_nodes
                    WHERE node_id = %s AND tenant_id = %s
                """, (node_id, tenant_id))
                row = cur.fetchone()
                if not row:
                    return None

                # Build data dict for scoring
                data = {
                    "identity.title_display": row[2],
                    "summary.narrative": row[3],
                    "evidence.confidence": row[4],
                    "evidence.source_count": row[5],
                    "identity.entity_name": row[6],
                    "master_entity_id": row[7],
                    "signal_rid": row[8],
                    "base60_address": row[9],
                    "enrichment_status": row[10],
                }

                # If entity node, pull entity details for richer scoring
                if row[7]:  # master_entity_id
                    cur.execute("""
                        SELECT canonical_name, aliases, identifiers,
                               signal_count, first_seen, last_seen
                        FROM entities WHERE entity_id = %s
                    """, (row[7],))
                    erow = cur.fetchone()
                    if erow:
                        data["identity.canonical_name"] = erow[0]
                        data["identity.aliases"] = erow[1]
                        data["identifiers"] = erow[2]
                        data["evidence.source_count"] = erow[3]
                        data["temporal.first_seen"] = erow[4]
                        data["temporal.last_seen"] = erow[5]

                score, tier = compute_score(row[0] or "entity", row[1] or "person", data)

                # Write back
                cur.execute("""
                    UPDATE knowledge_nodes
                    SET completeness_score = %s, completeness_tier = %s, updated_at = NOW()
                    WHERE node_id = %s
                """, (score, tier, node_id))

                # Also update entity if linked
                if row[7]:
                    cur.execute("""
                        UPDATE entities
                        SET completeness_score = %s, completeness_tier = %s, updated_at = NOW()
                        WHERE entity_id = %s
                    """, (score, tier, row[7]))

            conn.commit()
            return (score, tier)

    except Exception as e:
        _log.debug("Completeness scoring failed for %s: %s", node_id, e)
        return None


def _slot_filled(data: dict, slot: str) -> bool:
    """Check if a slot is filled (non-empty) in the data dict.

    Supports dot-notation keys (e.g., "identity.title_display") by
    checking both the full key and nested dict access.
    """
    # Direct key match
    val = data.get(slot)
    if val is not None and val != "" and val != [] and val != {}:
        return True

    # Try nested access (e.g., "summary.headline" → data["summary"]["headline"])
    parts = slot.split(".")
    current = data
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return False
    return current is not None and current != "" and current != [] and current != {}


def _score_to_tier(score: float) -> str:
    """Map a completeness score to its tier name."""
    if score >= 0.90:
        return "verified"
    elif score >= 0.70:
        return "solid"
    elif score >= 0.40:
        return "partial"
    else:
        return "stub"
