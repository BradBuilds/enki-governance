"""
Entity Governance — LDU-P, chain, conflicts, audit events.

Mirrors the signal LDU-T → LDU-P → GDU pattern for entities. Every
entity creation, mutation, merge, alias add must flow through here
to maintain a verifiable audit chain and surface conflicts honestly.

Key functions:
- emit_entity_event(): writes audit event + entity_receipt with chain hash
- log_conflict(): records governance_conflicts entries
- check_vocab(): validates entity_type against entityvocab GDU pack
- stage_provisional(): writes to entity_ldu_p instead of entities
- detect_alias_conflict(): checks if a new alias likely belongs to a different entity
- detect_property_conflict(): checks if a property update contradicts existing
- mark_revalidated(): updates last_researched_at after click revalidation
"""

from __future__ import annotations

import json
import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("enki.governance.entity")


def check_vocab(entity_type: str) -> bool:
    """Return True if entity_type resolves to a canonical entityvocab subtype.

    Delegates to subtype_validator.validate_for_ldu so the in-Python check
    matches the entityvocab pack AND migration 116's CHECK constraint. The
    previous hardcoded set drifted out of sync and let through types
    ('location', 'concept', 'event', ...) that the DB then rejected.
    """
    if not entity_type:
        return False
    try:
        from core.ldu.subtype_validator import validate_for_ldu
        return validate_for_ldu(entity_type).action == "accept"
    except Exception:
        return False


def stage_provisional(
    tenant_id: str,
    raw_name: str,
    suspected_type: str,
    source_pipeline: str,
    source_signal_id: Optional[str] = None,
    confidence: float = 0.5,
    evidence: Optional[str] = None,
    rejection_reason: Optional[str] = None,
    dsn: Optional[str] = None,
) -> Optional[str]:
    """Stage a provisional entity in entity_ldu_p when GDU rejects it.

    Returns ldu_p_id on success.
    """
    from storage.postgres.db import get_connection

    normalized = raw_name.lower().strip()
    ldu_p_id = f"eldp_{hashlib.sha256(f'{tenant_id}:{normalized}:{datetime.now(timezone.utc).isoformat()}'.encode()).hexdigest()[:16]}"

    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO entity_ldu_p
                        (ldu_p_id, tenant_id, raw_name, normalized_name,
                         suspected_type, source_pipeline, source_signal_id,
                         confidence, evidence, rejection_reason, status, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', NOW())
                """, (
                    ldu_p_id, tenant_id, raw_name, normalized,
                    suspected_type, source_pipeline, source_signal_id,
                    confidence, evidence, rejection_reason,
                ))
            conn.commit()
        return ldu_p_id
    except Exception as e:
        log.debug("Failed to stage provisional entity: %s", e)
        return None


def emit_entity_event(
    tenant_id: str,
    mutation_type: str,
    entity_ids: list[int],
    payload: dict,
    source_pipeline: str = "unknown",
    operator_id: Optional[str] = None,
    dsn: Optional[str] = None,
) -> Optional[int]:
    """Emit an entity mutation event with chain hash.

    mutation_type: create, merge, alias_add, property_change, tier_change, verify
    payload: {before: ..., after: ..., aliases_added: [...], etc.}
    """
    from storage.postgres.db import get_connection

    if not entity_ids:
        return None

    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                # Get previous chain hash
                cur.execute("""
                    SELECT current_chain_hash FROM entity_chain_state WHERE tenant_id = %s
                """, (tenant_id,))
                row = cur.fetchone()
                previous_hash = row[0] if row else "0" * 64

                # Compute receipt digest
                receipt_data = {
                    "tenant_id": tenant_id,
                    "mutation_type": mutation_type,
                    "entity_ids": sorted(entity_ids),
                    "payload": payload,
                    "source_pipeline": source_pipeline,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                receipt_digest = hashlib.sha256(
                    json.dumps(receipt_data, sort_keys=True, default=str).encode()
                ).hexdigest()

                # New chain hash = sha256(previous_hash || receipt_digest)
                chain_hash = hashlib.sha256(
                    (previous_hash + receipt_digest).encode()
                ).hexdigest()

                # Insert receipt
                cur.execute("""
                    INSERT INTO entity_receipts
                        (tenant_id, mutation_type, entity_ids, payload,
                         source_pipeline, receipt_digest, chain_hash,
                         previous_chain_hash, operator_id, commit_time)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    RETURNING rid
                """, (
                    tenant_id, mutation_type, entity_ids,
                    json.dumps(payload, default=str),
                    source_pipeline, receipt_digest, chain_hash, previous_hash,
                    operator_id,
                ))
                receipt_rid = cur.fetchone()[0]

                # Update chain state
                cur.execute("""
                    INSERT INTO entity_chain_state
                        (tenant_id, current_chain_hash, last_receipt_rid,
                         entity_count, mutation_count, updated_at)
                    VALUES (%s, %s, %s, 1, 1, NOW())
                    ON CONFLICT (tenant_id) DO UPDATE SET
                        current_chain_hash = EXCLUDED.current_chain_hash,
                        last_receipt_rid = EXCLUDED.last_receipt_rid,
                        mutation_count = entity_chain_state.mutation_count + 1,
                        updated_at = NOW()
                """, (tenant_id, chain_hash, receipt_rid))

                # Also emit to audit_events for cross-chain anchor
                try:
                    audit_payload = {
                        "event_type": f"entity.{mutation_type}",
                        "entity_ids": entity_ids,
                        "receipt_rid": receipt_rid,
                        "chain_hash": chain_hash,
                        "source_pipeline": source_pipeline,
                    }
                    cid = hashlib.sha256(
                        json.dumps(audit_payload, sort_keys=True, default=str).encode()
                    ).hexdigest()
                    cur.execute("""
                        INSERT INTO audit_events
                            (tenant_id, cid, payload_json, event_type, timestamp, node_id)
                        VALUES (%s, %s, %s, %s, NOW(), %s)
                        ON CONFLICT (tenant_id, cid) DO NOTHING
                    """, (
                        tenant_id, cid, json.dumps(audit_payload, default=str),
                        f"entity.{mutation_type}", "local",
                    ))
                except Exception:
                    pass  # Audit emission is best-effort

            conn.commit()
            return receipt_rid

    except Exception as e:
        log.debug("Failed to emit entity event: %s", e)
        return None


# ─── Conflict Detection ──────────────────────────────────────────────────

def log_conflict(
    tenant_id: str,
    conflict_type: str,
    competing_values: list[dict],
    detected_by: str,
    entity_id: Optional[int] = None,
    related_entity_ids: Optional[list[int]] = None,
    severity: str = "routine",
    auto_resolved: bool = False,
    auto_resolution: Optional[str] = None,
    auto_confidence: Optional[float] = None,
    metadata: Optional[dict] = None,
    dsn: Optional[str] = None,
) -> Optional[int]:
    """Log a governance conflict for review.

    conflict_type: alias, type, property, fact, merge_proposal
    competing_values: [{value, source, confidence, signal_ids[]}]
    severity: urgent | routine | info
    """
    from storage.postgres.db import get_connection

    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO governance_conflicts
                        (tenant_id, conflict_type, severity, entity_id,
                         related_entity_ids, competing_values, auto_resolved,
                         auto_resolution, auto_confidence, detected_by,
                         detected_at, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s)
                    RETURNING conflict_id
                """, (
                    tenant_id, conflict_type, severity, entity_id,
                    related_entity_ids or [],
                    json.dumps(competing_values, default=str),
                    auto_resolved, auto_resolution, auto_confidence,
                    detected_by,
                    json.dumps(metadata or {}, default=str),
                ))
                conflict_id = cur.fetchone()[0]

                # Bump entity's conflict_count if entity_id provided
                if entity_id and not auto_resolved:
                    cur.execute("""
                        UPDATE entities SET conflict_count = conflict_count + 1
                        WHERE entity_id = %s
                    """, (entity_id,))

            conn.commit()
            return conflict_id
    except Exception as e:
        log.debug("Failed to log conflict: %s", e)
        return None


def detect_alias_conflict(
    entity_id: int,
    new_alias: str,
    existing_aliases: list[str],
    canonical_name: str,
    source_pipeline: str = "unknown",
    source_signal_id: Optional[str] = None,
    tenant_id: str = "default",
    dsn: Optional[str] = None,
) -> bool:
    """Detect if a new alias is suspiciously different from existing ones.

    Uses simple heuristics:
    - Token overlap with canonical name < 0.3 → suspicious
    - Length differs > 2x from average alias → suspicious

    Returns True if conflict logged, False if alias looks safe.
    """
    if not new_alias or not canonical_name:
        return False

    new_tokens = set(new_alias.lower().split())
    canon_tokens = set(canonical_name.lower().split())

    # Compute Jaccard similarity
    overlap = len(new_tokens & canon_tokens) / max(len(new_tokens | canon_tokens), 1)

    if overlap < 0.3:
        # Suspicious — looks like a different entity
        log_conflict(
            tenant_id=tenant_id,
            conflict_type="alias",
            entity_id=entity_id,
            severity="routine",
            competing_values=[
                {"value": canonical_name, "source": "existing canonical", "confidence": 1.0},
                {"value": new_alias, "source": source_pipeline,
                 "confidence": 0.5, "signal_id": source_signal_id},
            ],
            detected_by="alias_token_overlap",
            auto_resolved=False,
            metadata={"jaccard_overlap": round(overlap, 3), "existing_aliases": existing_aliases[:5]},
            dsn=dsn,
        )
        return True
    return False


def detect_property_conflict(
    entity_id: int,
    property_name: str,
    existing_value: str,
    new_value: str,
    existing_source: str,
    new_source: str,
    tenant_id: str = "default",
    dsn: Optional[str] = None,
) -> bool:
    """Detect when a property update contradicts the existing value.

    e.g., Wikidata says head_of_state=X, articles say head_of_state=Y.
    """
    if not existing_value or not new_value:
        return False
    if str(existing_value).strip().lower() == str(new_value).strip().lower():
        return False  # Same value, no conflict

    # Severity: if both values come from authoritative sources, urgent
    authoritative = {"wikidata", "rest_countries", "operator", "verified"}
    severity = "urgent" if (existing_source in authoritative and new_source in authoritative) else "routine"

    log_conflict(
        tenant_id=tenant_id,
        conflict_type="property",
        entity_id=entity_id,
        severity=severity,
        competing_values=[
            {"value": str(existing_value), "source": existing_source, "confidence": 0.8, "first_seen": "existing"},
            {"value": str(new_value), "source": new_source, "confidence": 0.8, "first_seen": "new"},
        ],
        detected_by="property_diff",
        auto_resolved=False,
        metadata={"property": property_name},
        dsn=dsn,
    )
    return True


def detect_type_conflict(
    name: str,
    existing_type: str,
    new_type: str,
    tenant_id: str = "default",
    source_pipeline: str = "unknown",
    sample_signal_ids: Optional[list[str]] = None,
    dsn: Optional[str] = None,
) -> bool:
    """Detect when an entity is being assigned a different canonical_type.

    e.g., "Jordan" tagged as country in some articles, as person in others.
    """
    if existing_type == new_type:
        return False

    log_conflict(
        tenant_id=tenant_id,
        conflict_type="type",
        severity="urgent",
        competing_values=[
            {"value": existing_type, "source": "existing entity"},
            {"value": new_type, "source": source_pipeline, "signal_ids": sample_signal_ids or []},
        ],
        detected_by="type_disagreement",
        auto_resolved=False,
        metadata={"name": name},
        dsn=dsn,
    )
    return True


def mark_revalidated(
    entity_id: int,
    dsn: Optional[str] = None,
) -> None:
    """Update last_researched_at after click-triggered revalidation."""
    from storage.postgres.db import get_connection

    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE entities SET last_researched_at = NOW() WHERE entity_id = %s
                """, (entity_id,))
            conn.commit()
    except Exception:
        pass


def get_pending_conflict_count(tenant_id: str = "default", dsn: Optional[str] = None) -> dict:
    """Count of unreviewed conflicts by severity."""
    from storage.postgres.db import get_connection

    counts = {"urgent": 0, "routine": 0, "info": 0, "total": 0}
    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT severity, count(*)
                    FROM governance_conflicts
                    WHERE tenant_id = %s AND reviewed_at IS NULL
                    GROUP BY severity
                """, (tenant_id,))
                for r in cur.fetchall():
                    counts[r[0]] = r[1]
                    counts["total"] += r[1]
    except Exception:
        pass
    return counts
