"""The collapse operator ⊕.

`admit_assertion(node_id, property_key, value_label, precision_tier,
                  source_statement_id, confidence)` is the single entry
point. It:

  1. Validates the property exists + accepts this precision tier
  2. Resolves or admits the value into the property's lattice
  3. Looks up existing typed assessments for the same (node, property)
  4. Runs the 5-case collapse algebra (see valuevocab.mechanics.json):
       refinement / subsumption / alternatives /
       incompatible_property / enumerated_conflict
  5. Persists the new assessment + records contradicted_by edges when conflicts

Returns an AdmissionResult describing what happened so the caller can
react (e.g. UI shows "this corroborates an existing claim" vs "this
contradicts an existing claim").

Orthogonal ⊗ is implicit — assertions about DIFFERENT properties of the
same node don't interact; they accumulate.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from storage.postgres.db import get_connection
from core.substrate.properties import (
    Property, get_by_key, validate_precision, depth_for_tier,
    PropertyTypeError,
)
from core.substrate.values import (
    Value, find_or_create, in_same_chain, is_ancestor,
)

log = logging.getLogger("enki.substrate.collapse")


@dataclass
class AdmissionResult:
    action: str   # 'created' | 'refinement' | 'subsumption' | 'alternatives' | 'enumerated_conflict' | 'duplicate'
    assessment_id: int
    value_id: int
    canonical_assessment_id: Optional[int] = None    # which assessment is canonical for the (node, property)
    contradicts_assessment_ids: list[int] = None     # other assessments now marked contradicted_by this one
    reason: str = ""


def admit_assertion(
    *,
    subj_entity_id: int,
    property_key: str,
    value_label: str,
    precision_tier: str,
    source_statement_id: Optional[int] = None,
    signal_rid: Optional[int] = None,
    confidence: float = 0.8,
    polarity: str = "affirmative",
    tenant_id: str = "default",
    parent_value_label: Optional[str] = None,
    parent_value_tier: Optional[str] = None,
) -> AdmissionResult:
    """Admit a typed property assertion. Returns the action taken.

    If parent_value_label is provided, the new value is registered as a
    refinement of that parent (admitting the parent first if needed). This
    is how the substrate grows lattices: extractors that know the
    refinement chain pass it explicitly.
    """
    prop = get_by_key(property_key)
    if not prop:
        raise PropertyTypeError(f"unknown property: {property_key!r}")
    validate_precision(prop, precision_tier)

    parent_id: Optional[int] = None
    if parent_value_label and parent_value_tier:
        parent_val = find_or_create(
            prop, parent_value_label, parent_value_tier,
            tenant_id=tenant_id,
        )
        parent_id = parent_val.value_id

    # Resolve or admit the value
    value = find_or_create(
        prop, value_label, precision_tier,
        parent_value_id=parent_id, tenant_id=tenant_id,
    )

    depth = depth_for_tier(precision_tier)

    # Look up existing typed assessments for the same (node, property)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT assessment_id, value_id, precision_tier, precision_depth,
                          corroborated_by_count, contradicted_by_count,
                          source_statement_ids
                     FROM assessments
                    WHERE subj_entity_id = %s
                      AND property_id = %s
                      AND polarity = %s
                    ORDER BY assessment_id""",
                (subj_entity_id, prop.property_id, polarity),
            )
            existing = cur.fetchall()

    # Empty case — first typed assertion for this (node, property)
    if not existing:
        return _insert_new(
            conn_factory=get_connection,
            tenant_id=tenant_id, subj_entity_id=subj_entity_id,
            prop=prop, value=value, precision_tier=precision_tier,
            depth=depth, source_statement_id=source_statement_id,
            signal_rid=signal_rid, confidence=confidence, polarity=polarity,
            action="created",
        )

    # Run the algebra against each existing assessment
    refinement_target: Optional[tuple] = None       # existing row we refine
    subsumption_target: Optional[tuple] = None      # existing row we subsume
    alternatives: list[tuple] = []                  # rows that are alternatives to us
    duplicate_target: Optional[tuple] = None

    for row in existing:
        ex_id, ex_value_id, ex_tier, ex_depth, ex_corr, ex_contra, ex_stmt_ids = row
        if ex_value_id is None:
            # Legacy untyped assertion — can't compare lattice-wise. Skip.
            continue
        if ex_value_id == value.value_id:
            duplicate_target = row
            break

        same_chain = in_same_chain(value.value_id, ex_value_id)
        if same_chain:
            ex_is_ancestor = is_ancestor(ex_value_id, value.value_id)
            if ex_is_ancestor:
                # New value refines existing → REFINEMENT (we are deeper)
                refinement_target = row
            else:
                # Existing refines new value → SUBSUMPTION (existing is deeper)
                subsumption_target = row
        else:
            # Different chain in same property → alternative
            if precision_tier == "enumerated" and ex_tier == "enumerated":
                alternatives.append((row, "enumerated_conflict"))
            else:
                alternatives.append((row, "alternatives"))

    # Decide action by precedence: duplicate > refinement > subsumption > alternatives
    if duplicate_target:
        # Add this source as another attestation of the same value
        ex_id, ex_value_id, ex_tier, ex_depth, ex_corr, ex_contra, ex_stmt_ids = duplicate_target
        new_ids = list(ex_stmt_ids or [])
        if source_statement_id and source_statement_id not in new_ids:
            new_ids.append(source_statement_id)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE assessments
                          SET corroborated_by_count = corroborated_by_count + 1,
                              source_statement_ids = %s,
                              last_seen = NOW(),
                              confidence = GREATEST(COALESCE(confidence,0), %s)
                        WHERE assessment_id = %s""",
                    (new_ids, confidence, ex_id),
                )
            conn.commit()
        return AdmissionResult(
            action="duplicate", assessment_id=ex_id, value_id=value.value_id,
            canonical_assessment_id=ex_id,
            reason="same (node, property, value) already present; corroboration++",
        )

    if refinement_target:
        # New assertion is DEEPER — becomes canonical; existing becomes attestation
        result = _insert_new(
            conn_factory=get_connection,
            tenant_id=tenant_id, subj_entity_id=subj_entity_id,
            prop=prop, value=value, precision_tier=precision_tier,
            depth=depth, source_statement_id=source_statement_id,
            signal_rid=signal_rid, confidence=confidence, polarity=polarity,
            action="refinement",
        )
        ex_id = refinement_target[0]
        result.canonical_assessment_id = result.assessment_id
        result.reason = (
            f"new value refines existing (assessment {ex_id}); "
            f"new assessment {result.assessment_id} is canonical"
        )
        return result

    if subsumption_target:
        # Existing is DEEPER — it remains canonical; new is shallower attestation
        result = _insert_new(
            conn_factory=get_connection,
            tenant_id=tenant_id, subj_entity_id=subj_entity_id,
            prop=prop, value=value, precision_tier=precision_tier,
            depth=depth, source_statement_id=source_statement_id,
            signal_rid=signal_rid, confidence=confidence, polarity=polarity,
            action="subsumption",
        )
        ex_id = subsumption_target[0]
        result.canonical_assessment_id = ex_id
        result.reason = (
            f"existing assessment {ex_id} subsumes new (existing is deeper)"
        )
        return result

    if alternatives:
        # Different chains in same property → conflict (or hard conflict if enumerated)
        result = _insert_new(
            conn_factory=get_connection,
            tenant_id=tenant_id, subj_entity_id=subj_entity_id,
            prop=prop, value=value, precision_tier=precision_tier,
            depth=depth, source_statement_id=source_statement_id,
            signal_rid=signal_rid, confidence=confidence, polarity=polarity,
            action="alternatives",
        )
        contra_ids = [row[0] for row, kind in alternatives]
        result.contradicts_assessment_ids = contra_ids

        # Federation Phase 5 — broadcast the contradiction so the network
        # learns this entity has contested values at this property.
        # No-op when ENKI_HUB_URL is unset.
        try:
            from core.federation import phase4 as fed_phase4
            from core.federation.entity_publisher import canonical_key as _ekey
            from core.substrate.values import get_by_id as _value_by_id
            # Determine the contested counterpart value's label
            for row, _kind in alternatives[:1]:
                ex_id, ex_value_id, ex_tier, ex_depth, _, _, _ = row
                ex_value = _value_by_id(ex_value_id) if ex_value_id else None
                if not ex_value:
                    continue
                # Compute canonical_entity_key
                with get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT canonical_name, canonical_type FROM entities WHERE entity_id = %s",
                            (subj_entity_id,),
                        )
                        en = cur.fetchone()
                if en:
                    ent_name, ent_type = en
                    ekey = _ekey(ent_name or "", ent_type or "")
                    fed_phase4.publish_conflict(
                        canonical_entity_key=ekey,
                        property_key=property_key,
                        value_a=ex_value.label,
                        value_b=value.label,
                    )
        except Exception:
            log.debug("federation conflict broadcast failed", exc_info=True)
        # Determine hardness
        has_enum_conflict = any(kind == "enumerated_conflict" for _, kind in alternatives)
        if has_enum_conflict:
            result.action = "enumerated_conflict"
            result.reason = (
                f"hard conflict: enumerated values disagree with assessment(s) "
                f"{contra_ids}"
            )
        else:
            result.reason = (
                f"soft conflict: different chains in property {property_key!r} "
                f"vs assessment(s) {contra_ids}"
            )
        # Bump contradicted_by_count on the existing conflicting rows + on the new row
        with get_connection() as conn:
            with conn.cursor() as cur:
                for ex_id in contra_ids:
                    cur.execute(
                        "UPDATE assessments SET contradicted_by_count = contradicted_by_count + 1 "
                        " WHERE assessment_id = %s",
                        (ex_id,),
                    )
                cur.execute(
                    "UPDATE assessments SET contradicted_by_count = %s WHERE assessment_id = %s",
                    (len(contra_ids), result.assessment_id),
                )
            conn.commit()
        return result

    # Fallback: no relationship with any existing row — just create
    return _insert_new(
        conn_factory=get_connection,
        tenant_id=tenant_id, subj_entity_id=subj_entity_id,
        prop=prop, value=value, precision_tier=precision_tier,
        depth=depth, source_statement_id=source_statement_id,
        signal_rid=signal_rid, confidence=confidence, polarity=polarity,
        action="created",
    )


def _insert_new(
    *, conn_factory, tenant_id, subj_entity_id, prop: Property,
    value: Value, precision_tier: str, depth: int,
    source_statement_id: Optional[int], signal_rid: Optional[int],
    confidence: float, polarity: str, action: str,
) -> AdmissionResult:
    """Insert a fresh assessment row representing a typed property assertion."""
    from core.gdu.address import assign_address

    # Collapse key — same (subj, property, value) collapses (handled above by
    # duplicate_target detection, but keep collapse_key consistent for any
    # legacy paths that index on it).
    collapse_key = f"prop:{prop.property_id}:{value.value_id}:{subj_entity_id}"

    # Base60 address comes from the substrate's atomic sequence counter
    # (gdu_address_sequences). The address is structural: category.subtype.seq.
    # For property assertions we use category=assessment, subtype=observation.
    # The sequence counter is per-(category, subtype) so two entities
    # asserting the same value don't collide — each gets the next sequence
    # number under (assessment, observation). The address still MEANS
    # something — it tells you this is the Nth observation in the substrate
    # — instead of being an opaque hash.
    src_ids = [source_statement_id] if source_statement_id else []

    with conn_factory() as conn:
        # Atomic sequence counter advances + insert in the same transaction
        # so a rollback never leaks a sequence number.
        addr = assign_address(conn, "assessment", "observation")
        if not addr:
            # Fallback if the address sequence is misconfigured — should never
            # happen on a properly bootstrapped substrate but keeps the system
            # writing rather than blocking on a missing config row.
            import hashlib
            fp = hashlib.sha1(
                f"{subj_entity_id}|{prop.property_id}|{value.value_id}".encode()
            ).hexdigest()[:6]
            addr = f"5.{depth}.{fp}"
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO assessments (
                     tenant_id, base60_address, assessment_tier,
                     subj_entity_id, predicate_edge_type, obj_entity_id, obj_value,
                     polarity, collapse_key,
                     property_id, value_id, precision_tier, precision_depth,
                     corroborated_by_count, contradicted_by_count,
                     source_statement_ids, confidence
                   ) VALUES (%s, %s, 'observation',
                             %s, %s, NULL, %s,
                             %s, %s,
                             %s, %s, %s, %s,
                             1, 0,
                             %s, %s)
                   RETURNING assessment_id""",
                (tenant_id, addr,
                 subj_entity_id, f"has_{prop.property_key}", value.label[:200],
                 polarity, collapse_key,
                 prop.property_id, value.value_id, precision_tier, depth,
                 src_ids, confidence),
            )
            assessment_id = cur.fetchone()[0]
        conn.commit()

    return AdmissionResult(
        action=action, assessment_id=assessment_id, value_id=value.value_id,
        canonical_assessment_id=assessment_id,
        contradicts_assessment_ids=[],
    )


def get_property_assertions(
    subj_entity_id: int, property_key: Optional[str] = None,
) -> list[dict]:
    """Read-side: get all typed property assertions for an entity.

    Returns list of dicts grouped by property, with the canonical (deepest)
    value first and the attestation chain following. Used by the
    property-grouped Relations panel.
    """
    prop = None
    if property_key:
        prop = get_by_key(property_key)
        if not prop:
            return []

    with get_connection() as conn:
        with conn.cursor() as cur:
            sql = """
                SELECT a.assessment_id, a.property_id, p.property_key, p.label,
                       a.value_id, v.label, v.normalized_label,
                       a.precision_tier, a.precision_depth,
                       a.corroborated_by_count, a.contradicted_by_count,
                       a.source_statement_ids, a.confidence
                  FROM assessments a
                  JOIN properties p ON p.property_id = a.property_id
                  LEFT JOIN property_values v ON v.value_id = a.value_id
                 WHERE a.subj_entity_id = %s
                   AND a.property_id IS NOT NULL
            """
            params: list = [subj_entity_id]
            if prop:
                sql += " AND a.property_id = %s"
                params.append(prop.property_id)
            sql += " ORDER BY p.cycle, a.precision_depth DESC, a.assessment_id"
            cur.execute(sql, params)
            rows = cur.fetchall()

    return [
        {
            "assessment_id": r[0],
            "property_id": r[1],
            "property_key": r[2],
            "property_label": r[3],
            "value_id": r[4],
            "value_label": r[5],
            "value_normalized": r[6],
            "precision_tier": r[7],
            "precision_depth": r[8],
            "corroborated_by_count": r[9],
            "contradicted_by_count": r[10],
            "source_statement_ids": list(r[11] or []),
            "confidence": float(r[12]) if r[12] is not None else None,
        }
        for r in rows
    ]
