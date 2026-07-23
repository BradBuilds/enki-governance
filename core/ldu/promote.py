"""
LDU promotion — concept → instance (entities row) bridge.

Operates on ``concept_nodes`` (post-6.5 substrate). When a concept's
evidence tier + signal threshold cross, this module calls
``core.gdu.gateway.admit_entity`` (the single GDU writer) and stamps
the new entity_id onto ``concept_nodes.promoted_to_entity_id``.

Graduated evidence tiers (replaces the strict require_type_evidence
gate from the previous rev):

  A  External authoritative ID (QID, CIK, EIN, MMSI, IMO, ICAO24,
     FEC, DUNS, LEI, ticker, registration) — promote at signal_count ≥ 1
  B  Gazetteer match  — promote at signal_count ≥ 1
  C  Canonicalpack / alias_seeds — promote at signal_count ≥ 1
  D  LLM arbitration ≥ 0.85       — promote at signal_count ≥ 2
  E  Multi-source corroboration   — promote at signal_count ≥ 5
                                    from ≥ 2 distinct sources
  F  Co-occurrence pattern         — promote at signal_count ≥ 5 (future)
  G  Floor (normalize-only)        — NEVER auto-promote

After promotion, mentions + edges that were skipped at extraction time
(because the concept wasn't promoted yet) are replayed from
source_signal_rids so the dossier UI gets its connections.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.ldu.promote")


# ── Tier thresholds ────────────────────────────────────────────────────────

TIER_THRESHOLDS = {
    "A": {"min_signal": 1, "min_sources": 1},
    "B": {"min_signal": 1, "min_sources": 1},
    "C": {"min_signal": 1, "min_sources": 1},
    "D": {"min_signal": 2, "min_sources": 1},
    "E": {"min_signal": 5, "min_sources": 2},
    "F": {"min_signal": 5, "min_sources": 2},
    "G": {"min_signal": 9999, "min_sources": 9999},  # never auto-promote
}


# ── Evidence evaluation ────────────────────────────────────────────────────

def evaluate_for_promotion(
    concept_id: int,
    *,
    dsn: Optional[str] = None,
) -> dict:
    """Return ``{'ready': bool, 'reason': str, 'evidence_tier': str,
                'criteria_met': {...}}``."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT signal_count, source_signal_rids,
                          evidence_tier, promotion_status,
                          canonical_name, subtype_path, identifiers
                     FROM concept_nodes
                    WHERE concept_id = %s""",
                (concept_id,),
            )
            row = cur.fetchone()
            if not row:
                return {"ready": False, "reason": "not_found",
                        "evidence_tier": None, "criteria_met": {}}
            (sig_count, sig_rids, tier, status,
             cname, spath, idents) = row

    if status != "pending":
        return {"ready": False, "reason": f"status:{status}",
                "evidence_tier": tier, "criteria_met": {}}

    if not tier:
        # No evidence stamp at all → floor.
        tier = "G"

    distinct_sources = len(set(sig_rids or []))

    # Tier E self-stamping: corpus corroboration IS the validating signal.
    # A G-floor concept that has accumulated multi-source independent
    # mentions has satisfied tier E's definition ("multi-source
    # corroboration") by definition. Escalate so the promotion gate fires.
    # The admit_candidate garbage filter + normalize rejection rules
    # already screened obvious noise before a concept_node row ever
    # existed, so this is safe to do without an LLM round-trip.
    e_threshold = TIER_THRESHOLDS["E"]
    if (tier == "G"
            and sig_count >= e_threshold["min_signal"]
            and distinct_sources >= e_threshold["min_sources"]):
        tier = "E"

    criteria_met = {
        "signal_count": sig_count,
        "distinct_sources": distinct_sources,
        "evidence_tier": tier,
        "has_identifiers": bool(idents),
    }

    threshold = TIER_THRESHOLDS.get(tier, TIER_THRESHOLDS["G"])
    if (sig_count >= threshold["min_signal"]
            and distinct_sources >= threshold["min_sources"]):
        return {"ready": True, "reason": f"tier_{tier}_satisfied",
                "evidence_tier": tier, "criteria_met": criteria_met}

    return {
        "ready": False,
        "reason": f"tier_{tier}_below_threshold "
                  f"(sig {sig_count}<{threshold['min_signal']} "
                  f"or sources {distinct_sources}<{threshold['min_sources']})",
        "evidence_tier": tier,
        "criteria_met": criteria_met,
    }


# ── Promotion ──────────────────────────────────────────────────────────────

def promote_concept(
    concept_id: int,
    *,
    dsn: Optional[str] = None,
) -> dict:
    """Call gateway.admit_entity for a ready concept. Stamp the result
    onto concept_nodes. Replay mentions/edges from source_signal_rids.

    Returns ``{'promoted': bool, 'entity_id': int|None, 'reason': str,
                'mentions_replayed': int}``.
    """
    # Pull the concept row under a FOR UPDATE lock so concurrent
    # promoters can't double-promote.
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT tenant_id, canonical_name, canonical_subtype,
                          subtype_path, identifiers, type_evidence,
                          source_signal_rids, promotion_status,
                          concept_address, signal_count
                     FROM concept_nodes
                    WHERE concept_id = %s
                    FOR UPDATE""",
                (concept_id,),
            )
            row = cur.fetchone()
            if not row:
                return {"promoted": False, "entity_id": None,
                        "reason": "concept_not_found",
                        "mentions_replayed": 0}
            (tenant_id, cname, csub, spath, idents, evidence,
             sig_rids, status, concept_addr, sig_count) = row

            if status != "pending":
                return {"promoted": False, "entity_id": None,
                        "reason": f"status:{status}",
                        "mentions_replayed": 0}

    # External identifiers to hand to the gateway.
    identifiers = dict(idents or {})

    # Seed signal_rid: the first signal that landed on this concept,
    # or 0 if research-derived.
    seed_signal_rid = 0
    if sig_rids:
        try:
            seed_signal_rid = int(sig_rids[0])
        except (TypeError, ValueError):
            seed_signal_rid = 0

    try:
        from core.gdu.gateway import get_gateway
        result = get_gateway().admit_entity(
            tenant_id=tenant_id,
            name=cname,
            entity_type=csub or "person",
            signal_rid=seed_signal_rid,
            identifiers=identifiers,
            context="ldu.promote",
            dsn=dsn,
        )
    except Exception as e:
        log.exception("promotion gateway call failed for concept=%s", concept_id)
        return {"promoted": False, "entity_id": None,
                "reason": f"gateway_exception:{e}",
                "mentions_replayed": 0}

    if not result or result.action == "route_ldup" or not result.entity_id:
        # Gateway routed to LDU-P or rejected.
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE concept_nodes SET
                         type_evidence = COALESCE(type_evidence, '{}'::jsonb)
                                        || jsonb_build_object(
                                             'gateway_action',
                                             %s),
                         updated_at    = NOW()
                       WHERE concept_id = %s""",
                    (getattr(result, "action", "none"), concept_id),
                )
            conn.commit()
        return {"promoted": False, "entity_id": None,
                "reason": f"gateway_action:{getattr(result, 'action', 'none')}",
                "mentions_replayed": 0}

    entity_id = int(result.entity_id)

    # Transfer accumulated signal_count to the entity, mark concept
    # promoted, AND overwrite entities.base60_address with the concept's
    # structured address. The gateway assigned a sequential address
    # from the legacy gdu_address_sequences table; we want the
    # divisor-aware concept_address ("1.0.1.5" for Iran's
    # place.country.asia.slot_5) on the canonical entity row so
    # downstream queries see the structured hierarchy.
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Address inheritance — only update if concept_addr is a
            # real structured address (not an overflow placeholder).
            if concept_addr and not concept_addr.startswith("overflow:"):
                cur.execute(
                    """UPDATE entities SET base60_address = %s
                        WHERE entity_id = %s""",
                    (concept_addr, entity_id),
                )

            increment = max(int(sig_count or 0) - 1, 0)
            if increment > 0:
                cur.execute(
                    """UPDATE entities SET
                         signal_count = signal_count + %s,
                         last_seen   = NOW(),
                         updated_at  = NOW()
                       WHERE entity_id = %s""",
                    (increment, entity_id),
                )

            cur.execute(
                """UPDATE concept_nodes SET
                     promotion_status      = 'promoted',
                     promoted_to_entity_id = %s,
                     updated_at            = NOW()
                   WHERE concept_id = %s""",
                (entity_id, concept_id),
            )

            # Replay mentions/edges for the accumulated signal_rids.
            mentions_replayed = _replay_mentions(
                cur,
                tenant_id=tenant_id,
                entity_id=entity_id,
                concept_id=concept_id,
                source_signal_rids=sig_rids or [],
                canonical_name=cname,
            )
        conn.commit()

    log.info(
        "Promoted concept=%s (%r) → entity_id=%s, mentions_replayed=%d",
        concept_id, cname, entity_id, mentions_replayed,
    )
    return {"promoted": True, "entity_id": entity_id, "reason": "ok",
            "mentions_replayed": mentions_replayed}


# ── Mention/edge replay ────────────────────────────────────────────────────

def _replay_mentions(
    cur,
    *,
    tenant_id: str,
    entity_id: int,
    concept_id: int,
    source_signal_rids: list,
    canonical_name: str,
) -> int:
    """For each accumulated source_signal_rid, write an entity_mentions
    row connecting the new entity to that signal/document.

    These mentions were SKIPPED at extraction time because the concept
    wasn't promoted yet (no entity_id existed). Replay catches them up
    so dossier UI shows article connections immediately.

    Each row uses a SAVEPOINT so an individual row failure (e.g.,
    duplicate, schema drift) doesn't poison the whole promotion
    transaction.

    Returns count of mentions written.
    """
    if not source_signal_rids:
        return 0

    # Get the active_set version for lineage stamping (required column).
    try:
        from core.ldu.lineage import get_lineage_stamp
        stamp = get_lineage_stamp()
        active_set = stamp.get("active_set_version") or "default"
        pack_versions = stamp.get("pack_versions", {})
    except Exception:
        active_set = "default"
        pack_versions = {}

    written = 0
    for sig_rid in source_signal_rids:
        try:
            sig_rid = int(sig_rid)
        except (TypeError, ValueError):
            continue
        if sig_rid <= 0:
            continue

        # SAVEPOINT-wrapped insert so a per-row failure doesn't abort
        # the transaction.
        cur.execute("SAVEPOINT replay_row")
        try:
            cur.execute(
                """
                INSERT INTO entity_mentions
                  (tenant_id, entity_id, signal_rid,
                   node_id, name_raw,
                   confidence, extraction_profile_id,
                   admitted_under_active_set,
                   admitted_under_pack_versions,
                   gdu_canonical, created_at)
                VALUES (%s, %s, %s,
                        %s, %s,
                        0.8, 'ldu.promote.replay',
                        %s,
                        %s::jsonb,
                        TRUE, NOW())
                """,
                (tenant_id, entity_id, sig_rid,
                 f"entity:{entity_id}", canonical_name[:200],
                 active_set,
                 json.dumps(pack_versions)),
            )
            if cur.rowcount:
                written += 1
            cur.execute("RELEASE SAVEPOINT replay_row")
        except Exception as e:
            cur.execute("ROLLBACK TO SAVEPOINT replay_row")
            log.debug("mention replay row failed: %s", e)
            continue
    return written


# ── Batch driver ────────────────────────────────────────────────────────────

def promote_ready_concepts(
    *,
    tenant_id: str = "default",
    limit: int = 100,
    dsn: Optional[str] = None,
) -> dict:
    """Walk pending concepts ordered by evidence-strongest-first, try to
    promote each. Returns counts."""
    evaluated = 0
    promoted = 0
    errors = 0
    total_replayed = 0

    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                # Tier G is normally floor-only, but a G concept that has
                # crossed the tier-E corroboration threshold (>=5 signals
                # from >=2 distinct sources) is real evidence by the design's
                # own definition — evaluate_for_promotion self-stamps it to
                # E when that holds. Include those rows here so the gate
                # actually fires.
                e_min_sig = TIER_THRESHOLDS["E"]["min_signal"]
                e_min_src = TIER_THRESHOLDS["E"]["min_sources"]
                cur.execute(
                    """SELECT concept_id FROM concept_nodes
                        WHERE tenant_id = %s
                          AND promotion_status = 'pending'
                          AND evidence_tier IS NOT NULL
                          AND (evidence_tier <> 'G'
                               OR (signal_count >= %s
                                   AND array_length(source_signal_rids, 1) >= %s))
                        ORDER BY evidence_tier ASC,  -- A < B < C < D < ...
                                 signal_count DESC,
                                 created_at ASC NULLS LAST
                        LIMIT %s""",
                    (tenant_id, e_min_sig, e_min_src, limit),
                )
                cids = [r[0] for r in cur.fetchall()]
    except Exception:
        log.exception("promote_ready_concepts: pending-scan failed")
        return {"evaluated": 0, "promoted": 0, "errors": 1,
                "mentions_replayed": 0}

    for cid in cids:
        evaluated += 1
        try:
            verdict = evaluate_for_promotion(cid, dsn=dsn)
            if verdict.get("ready"):
                outcome = promote_concept(cid, dsn=dsn)
                if outcome.get("promoted"):
                    promoted += 1
                    total_replayed += outcome.get("mentions_replayed", 0)
        except Exception:
            log.exception("promote_ready_concepts: cid=%s failed", cid)
            errors += 1

    if promoted or errors:
        log.info(
            "promote tick: eval=%d promo=%d err=%d mentions_replayed=%d",
            evaluated, promoted, errors, total_replayed,
        )
    return {"evaluated": evaluated, "promoted": promoted,
            "errors": errors, "mentions_replayed": total_replayed}


# Legacy alias
promote_ready_candidates = promote_ready_concepts
promote_candidate = promote_concept
