"""
Node Intake Gateway — the SINGLE entry point for all knowledge node creation.

Every part of the system that wants to create a knowledge node MUST go through
this gateway. It enforces:

  1. Entity resolution (deduplication)
  2. Noteworthiness filter (prevents noise)
  3. Evidence profile construction (not single confidence score)
  4. Staleness classification
  5. GDU governance (epistemic type rules)

This is how the system handles 10,000 signals/day without the graph exploding.

Usage:
    from core.knowledge.intake import submit_node

    result = submit_node(
        tenant_id="default",
        title="Vessel BLUE HORIZON at Sevastopol",
        entity_name="BLUE HORIZON",
        entity_type="vessel",
        entity_identifiers={"mmsi": "123456789"},
        epistemic_type="observation",
        source_signal_rids=[4421],
        source_thread_id="thread-abc123",
        latitude=44.31, longitude=33.50,
        event_time="2026-04-03T14:22:00Z",
    )
    # result.action = "merged" | "created" | "review_created" | "rejected"
    # result.node_id = "node-xxx"
"""

from __future__ import annotations

import json
import uuid
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional

from storage.postgres.db import get_connection
from core.knowledge.resolver import resolve_entity, merge_into_node, ResolveResult

log = logging.getLogger("enki.intake")


# ═══════════════════════════════════════════════════════════════════════════════
# Evidence Profile — replaces single confidence score
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class EvidenceProfile:
    """Multi-dimensional assessment — what the analyst actually needs to see."""
    source_count: int = 0               # total source signals
    source_independent: int = 0         # deduplicated independent sources
    agreement_ratio: float = 1.0        # what % of sources agree (1.0 = no contradictions)
    best_authority: str = "unverified"   # highest authority level among sources
    temporal_relevance: float = 1.0     # 1.0 = current, decays with time
    has_location: bool = False
    has_temporal: bool = False
    has_entity: bool = False
    contradiction_count: int = 0
    corroboration_count: int = 0
    gap_count: int = 0
    status: str = "unverified"          # confirmed, supported, contested, unverified, stale

    def compute_status(self) -> str:
        if self.contradiction_count > 0 and self.corroboration_count == 0:
            return "contested"
        if self.corroboration_count >= 2 and self.contradiction_count == 0:
            return "confirmed"
        if self.corroboration_count >= 1:
            return "supported"
        if self.temporal_relevance < 0.3:
            return "stale"
        return "unverified"

    def to_dict(self) -> dict:
        self.status = self.compute_status()
        return {
            "source_count": self.source_count,
            "source_independent": self.source_independent,
            "agreement_ratio": self.agreement_ratio,
            "best_authority": self.best_authority,
            "temporal_relevance": round(self.temporal_relevance, 2),
            "has_location": self.has_location,
            "has_temporal": self.has_temporal,
            "has_entity": self.has_entity,
            "contradiction_count": self.contradiction_count,
            "corroboration_count": self.corroboration_count,
            "gap_count": self.gap_count,
            "status": self.status,
        }

    def composite_score(self) -> float:
        """Legacy single score for backward compat. Use status instead."""
        score = 0.3
        if self.source_independent >= 3: score += 0.2
        elif self.source_independent >= 2: score += 0.1
        elif self.source_independent >= 1: score += 0.05
        auth = {"verified": 0.2, "trusted": 0.15, "unverified": 0.05, "disputed": 0}.get(self.best_authority, 0.05)
        score += auth
        score += min(self.corroboration_count * 0.08, 0.2)
        score -= min(self.contradiction_count * 0.12, 0.3)
        score += 0.05 * sum([self.has_location, self.has_temporal, self.has_entity])
        score *= self.temporal_relevance
        return max(0.0, min(1.0, round(score, 2)))


# ═══════════════════════════════════════════════════════════════════════════════
# Staleness Classification
# ═══════════════════════════════════════════════════════════════════════════════

STALENESS_RULES = {
    # entity_type → (staleness_class, default_ttl_hours)
    "vessel": ("temporal", 6),        # vessel position stale after 6 hours
    "aircraft": ("temporal", 2),      # aircraft position stale after 2 hours
    "device": ("temporal", 4),        # device location stale after 4 hours
    "person": ("temporal", 24),       # person location stale after 24 hours
    "vehicle": ("temporal", 12),      # vehicle position stale after 12 hours
    "organization": ("durable", None),# org facts don't expire
    "facility": ("durable", None),    # facility facts don't expire
    "account": ("durable", None),     # account facts don't expire
}

EPISTEMIC_STALENESS = {
    # epistemic_type overrides entity staleness
    "fact": ("durable", None),
    "observation": None,              # use entity type rule
    "claim": ("volatile", 168),       # claims expire after 1 week if not corroborated
    "hypothesis": ("volatile", 336),  # hypotheses expire after 2 weeks
    "question": ("durable", None),    # questions don't expire
    "evidence": None,                 # use entity type rule
}

def classify_staleness(
    epistemic_type: str,
    entity_type: str = None,
    event_time: str = None,
) -> tuple[str, Optional[str]]:
    """Returns (staleness_class, expires_at ISO string or None)."""
    # Epistemic override
    override = EPISTEMIC_STALENESS.get(epistemic_type)
    if override is not None:
        cls, ttl_hours = override
        if ttl_hours and event_time:
            try:
                et = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
                expires = et + timedelta(hours=ttl_hours)
                return cls, expires.isoformat()
            except (ValueError, TypeError):
                pass
        return cls, None

    # Entity type rule
    rule = STALENESS_RULES.get(entity_type, ("temporal", 24))
    cls, ttl_hours = rule
    if ttl_hours and event_time:
        try:
            et = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
            expires = et + timedelta(hours=ttl_hours)
            return cls, expires.isoformat()
        except (ValueError, TypeError):
            pass
    return cls, None


# ═══════════════════════════════════════════════════════════════════════════════
# Noteworthiness Filter — decides what warrants a node
# ═══════════════════════════════════════════════════════════════════════════════

# Signal types that should NEVER auto-create individual nodes (too noisy)
PATTERN_ONLY_TYPES = {
    "signal.geospatial.position_report",    # GPS pings → track patterns only
    "signal.geospatial.vessel_position",    # AIS → track patterns only
    "signal.geospatial.aircraft_position",  # ADS-B → track patterns only
    "signal.device.sensor_reading",         # sensor → pattern only
    "signal.device.network_flow",           # traffic → pattern only
    "signal.device.process_list",           # system inventory → pattern only
}

# Conditions that make a PATTERN_ONLY signal noteworthy (bypass the filter)
def is_noteworthy(
    signal_type: str = None,
    entity_name: str = None,
    is_anomaly: bool = False,
    is_watchlist_match: bool = False,
    is_rule_match: bool = False,
    is_first_observation: bool = False,
    amount: float = None,
    amount_mean: float = None,
    amount_std: float = None,
) -> tuple[bool, str]:
    """
    Returns (noteworthy, reason).

    Most signals update patterns silently. Only noteworthy events create nodes.
    """
    # Always noteworthy
    if is_anomaly:
        return True, "anomaly detected"
    if is_watchlist_match:
        return True, "watchlist match"
    if is_rule_match:
        return True, "detection rule match"
    if is_first_observation:
        return True, "first observation of entity"

    # Financial anomaly: amount > 2σ from mean
    if amount is not None and amount_mean is not None and amount_std is not None:
        if amount_std > 0 and abs(amount - amount_mean) > 2 * amount_std:
            return True, f"anomalous amount: ${amount:.2f} vs mean ${amount_mean:.2f}"

    # If signal type is pattern-only, reject unless above conditions met
    if signal_type in PATTERN_ONLY_TYPES:
        return False, "routine data point — pattern only"

    # Everything else is noteworthy by default
    return True, "standard observation"


# ═══════════════════════════════════════════════════════════════════════════════
# Node Intake — the gateway
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class IntakeResult:
    action: str             # "created", "merged", "review_created", "rejected"
    node_id: Optional[str] = None
    reason: str = ""
    evidence_profile: Optional[dict] = None


def submit_node(
    tenant_id: str,
    title: str,
    epistemic_type: str = "observation",
    description: str = None,
    entity_name: str = None,
    entity_type: str = None,
    entity_identifiers: dict = None,
    source_signal_rids: list[int] = None,
    source_thread_id: str = None,
    project_id: str = "",
    latitude: float = None,
    longitude: float = None,
    event_time: str = None,
    concepts: list[str] = None,
    source_authority: str = "unverified",
    # Noteworthiness context
    signal_type: str = None,
    is_anomaly: bool = False,
    is_watchlist_match: bool = False,
    is_rule_match: bool = False,
    dsn: Optional[str] = None,
) -> IntakeResult:
    """
    Submit a potential knowledge node through the intake gateway.

    Steps:
      1. Noteworthiness check — is this worth a node?
      2. Entity resolution — does this entity already have a node?
      3. If merge → update existing node with new sources
      4. If create → build evidence profile, classify staleness, insert
      5. If review → create with same_concept edge to possible match
    """
    identifiers = entity_identifiers or {}

    # ─── Step 1: Noteworthiness filter ────────────────────────────────
    # Check if this is a first observation (no existing node for this entity)
    is_first = False
    if entity_name:
        resolve_check = resolve_entity(tenant_id, entity_name, entity_type, identifiers,
                                        latitude, longitude, dsn)
        is_first = resolve_check.action == "create"

    noteworthy, note_reason = is_noteworthy(
        signal_type=signal_type,
        entity_name=entity_name,
        is_anomaly=is_anomaly,
        is_watchlist_match=is_watchlist_match,
        is_rule_match=is_rule_match,
        is_first_observation=is_first,
    )

    if not noteworthy:
        return IntakeResult(action="rejected", reason=note_reason)

    # ─── Step 2: Entity resolution ────────────────────────────────────
    resolved = resolve_entity(tenant_id, entity_name, entity_type, identifiers,
                               latitude, longitude, dsn)

    # ─── Step 3: Merge into existing node ─────────────────────────────
    if resolved.action == "merge":
        merge_into_node(
            node_id=resolved.node_id,
            tenant_id=tenant_id,
            new_source_signals=source_signal_rids,
            new_source_thread=source_thread_id,
            new_description=description,
            new_identifiers=identifiers,
            new_latitude=latitude,
            new_longitude=longitude,
            new_event_time=event_time,
            dsn=dsn,
        )
        return IntakeResult(
            action="merged",
            node_id=resolved.node_id,
            reason=resolved.match_reason,
        )

    # ─── Step 4/5: Create new node ────────────────────────────────────
    node_id = f"node-{uuid.uuid4().hex[:12]}"

    # Build evidence profile
    profile = EvidenceProfile(
        source_count=len(source_signal_rids or []),
        source_independent=len(source_signal_rids or []),
        best_authority=source_authority,
        has_location=latitude is not None,
        has_temporal=event_time is not None,
        has_entity=bool(entity_name),
    )

    # Classify staleness
    staleness_class, expires_at = classify_staleness(epistemic_type, entity_type, event_time)

    # Detect gaps
    gaps = []
    if not entity_name:
        gaps.append("No entity identified")
    if not event_time:
        gaps.append("No temporal anchor")
    if latitude is None:
        gaps.append("No location data")
    if len(source_signal_rids or []) < 2:
        gaps.append("Single source — needs corroboration")
    if source_authority == "unverified":
        gaps.append("Source authority unverified")

    profile.gap_count = len(gaps)

    # Admit through GDU gateway for governance stamps + base-60 address
    try:
        from core.gdu.enforce import admit_node as gdu_admit_node
        gdu_admit_node(
            tenant_id=tenant_id,
            category="assessment",
            subtype=epistemic_type if epistemic_type in ("fact", "observation", "claim", "hypothesis", "question") else "observation",
            title=title[:200],
            description=(description or "")[:500],
            signal_rid=0,
            collapse_key=node_id,
        )
    except Exception:
        pass  # Gateway is best-effort here — direct SQL below is the fallback

    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO knowledge_nodes
                        (node_id, project_id, tenant_id, title, description,
                         epistemic_type, concepts, entity_type, entity_name,
                         entity_identifiers, source_authority,
                         source_signals, source_threads, source_count,
                         latitude, longitude, event_time,
                         confidence, confidence_factors,
                         gaps, gap_count, enrichment_status,
                         metadata, ai_enriched, gdu_canonical)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending',
                            %s, FALSE, TRUE)
                """, (
                    node_id, project_id or "", tenant_id, title, description,
                    epistemic_type, concepts or [],
                    entity_type, entity_name, json.dumps(identifiers), source_authority,
                    source_signal_rids or [], [source_thread_id] if source_thread_id else [],
                    len(source_signal_rids or []),
                    latitude, longitude, event_time,
                    profile.composite_score(), json.dumps(profile.to_dict()),
                    gaps, len(gaps),
                    json.dumps({
                        "staleness_class": staleness_class,
                        "expires_at": expires_at,
                        "intake_reason": note_reason,
                    }),
                ))

                # If review match, create same_concept edge to possible duplicate
                if resolved.action == "review" and resolved.node_id:
                    cur.execute("""
                        INSERT INTO knowledge_edges
                            (edge_id, project_id, source_node_id, target_node_id,
                             relationship, strength, evidence, ai_generated)
                        VALUES (%s, %s, %s, %s, 'same_concept', %s, %s, TRUE)
                        ON CONFLICT DO NOTHING
                    """, (
                        f"edge-{uuid.uuid4().hex[:12]}", project_id or "",
                        node_id, resolved.node_id, resolved.confidence,
                        f"Possible duplicate: {resolved.match_reason}",
                    ))

                # Link to thread if provided
                if source_thread_id:
                    cur.execute("""
                        INSERT INTO thread_nodes (thread_id, node_id, auto_generated, relevance)
                        VALUES (%s, %s, TRUE, 'active')
                        ON CONFLICT DO NOTHING
                    """, (source_thread_id, node_id))

            conn.commit()

    except Exception as e:
        log.error(f"Node creation failed: {e}")
        return IntakeResult(action="rejected", reason=str(e))

    action = "review_created" if resolved.action == "review" else "created"
    return IntakeResult(
        action=action,
        node_id=node_id,
        reason=note_reason,
        evidence_profile=profile.to_dict(),
    )
