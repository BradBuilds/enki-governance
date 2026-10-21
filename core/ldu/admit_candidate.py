"""
LDU admit_candidate — the capture-time front door.

The full algorithm (per the internal ldu gdu and public network plan
phase 6.5 rewrite):

  1. Normalize (via entityvocab.normalize_rules).
     If rejected → write rejected ldu_candidates row, return.

  2. Look up the normalized form in the unified thesaurus
     (core.ldu.dictionaries). Layers consulted in order:
       canonicalpack → gazetteer → alias_seeds.
     If hit: replace canonical fields, stamp tier B/C evidence.

  3. Ensure a form_nodes row exists for this surface_form
     (deterministic hash-based form_address).

  4. Identifier-based concept lookup (strategies 1+2): if the resolved
     identifiers (QID, CIK, etc.) match an existing concept, bind form →
     concept, return.

  5. Name-based concept lookup (strategy 3+4): match by canonical_name
     + subtype_path. If found and no identifier conflict, bind, return.

  6. Mint a new concept. Allocate a base-60 address using the partition
     hint (region/role/sector) and the subtype's divisor. Bind form →
     new concept. Return.

  7. If the concept is already promoted (promoted_to_entity_id set),
     also call gateway.admit_entity to bump the canonical signal_count
     and write the GDU admission log.

This function NEVER writes to ``entities`` directly. The only entity
table mutation is via gateway.admit_entity, and only when we've
already confirmed a promoted concept exists at the target.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from storage.postgres.db import get_connection
from core.gdu.normalize import normalize as _normalize
from core.ldu.lineage import get_lineage_stamp
from core.ldu.dictionaries import resolve as _resolve, ResolveResult
from core.gdu.pack_registry import get_category_index, get_subtype_index

log = logging.getLogger("enki.ldu.admit_candidate")


# Decisive external identifiers — a registry/authority key that uniquely names a
# real-world entity (Tier A per the internal LDU/GDU plan). A concept carrying one
# of these has decisive evidence of a distinct entity: it should be minted at
# evidence_tier "A" (promote at signal_count ≥ 1), not stranded at the G floor.
#
# Phase 2.1/2.2: this was a 20-key frozenset written here in Python. It is now
# pack content, declared per subtype as a PROPERTY OF A SLOT in the governed
# decomposition (`completenessvocab.slot_specs` -> `decisive_slots`), because an
# IMO is decisive not by appearing on a list but because that slot alone settles
# which hull this is. See core/ldu/decisive_identifiers.py.
#
# The name is kept as a module-level accessor so the two existing consumers keep
# working unchanged. It is a FUNCTION now, not a constant: a constant would be
# evaluated at import time and freeze whichever pack happened to be loaded then,
# which is the same "current on its face, false in fact" failure the completeness
# regime stamp exists to catch.
def DECISIVE_IDENTIFIER_KEYS() -> frozenset:  # noqa: N802  (kept for callers)
    """Every decisive identifier key across all subtypes, from the pack.

    Deliberately the UNION and not the per-subtype set: both consumers ask only
    "does this carry any decisive identifier", so the union preserves today's
    behaviour exactly and this phase moves knowledge into the pack without moving
    any collapse decision. Callers that know the subtype should prefer
    `decisive_identifiers.decisive_keys_for(subtype)` — that is what stops an
    organisation's EIN lifting a person to Tier A — but switching them is a
    separate change with its own measurement.
    """
    from core.ldu.decisive_identifiers import all_decisive_keys
    return all_decisive_keys()


# ── Result type ─────────────────────────────────────────────────────────────

@dataclass
class CandidateAdmissionResult:
    """Outcome of admit_candidate. Fields populated depend on the action.

    actions:
      'rejected'              — normalize rejected; ldu_candidates audit row written.
      'attached_to_gdu_entity' — concept already promoted; entity_id is set.
      'bound_to_concept'      — form bound to an existing pending concept.
      'created_concept'       — new concept minted; form bound to it.
    """
    action: str
    form_id: Optional[int] = None
    concept_id: Optional[int] = None
    entity_id: Optional[int] = None
    concept_address: Optional[str] = None
    canonical_name: Optional[str] = None
    canonical_subtype: Optional[str] = None
    normalized_name: Optional[str] = None
    rejection_reason: Optional[str] = None
    evidence_tier: Optional[str] = None
    thesaurus_source: Optional[str] = None
    # Legacy fields preserved so existing callers don't break:
    node_id: Optional[str] = None
    base60_address: Optional[str] = None
    category: Optional[str] = None
    subtype: Optional[str] = None
    is_new: bool = False
    candidate_id: Optional[int] = None  # legacy alias for concept_id


# ── Subtype role hinting (unchanged from previous rev) ─────────────────────

_PLACE_LIKE = {
    "country", "city", "region", "admin_region", "location",
    "island", "river", "ocean", "sea", "strait", "maritime_area",
    "peninsula", "archipelago", "poi", "facility_location",
    "coordinate_cluster", "named_area", "airspace",
}

_ORG_LIKE = {
    "organization", "government_agency", "mass_media",
    "unit", "weapon_system", "product", "facility",
    "company", "ngo", "political_party", "armed_group",
}


def _role_for_type(proposed_type: str) -> str:
    pt = (proposed_type or "").lower()
    if pt in _ORG_LIKE:
        return "organization"
    if pt in _PLACE_LIKE:
        return "place"
    return "person"


def _category_for_subtype(subtype: str) -> str:
    """Map a bare subtype to its nodevocab category."""
    pt = (subtype or "").lower()
    if pt in _PLACE_LIKE:
        return "place"
    return "entity"


# ── Public API ──────────────────────────────────────────────────────────────

def admit_candidate(
    *,
    surface_form: str,
    proposed_type: str,
    signal_rid: int,
    statement_id: Optional[int] = None,
    tenant_id: str = "default",
    proposed_subtype: Optional[str] = None,
    type_evidence: Optional[dict] = None,
    dsn: Optional[str] = None,
) -> CandidateAdmissionResult:
    """Admit a surface form into the LDU substrate. See module docstring."""

    if not surface_form:
        return CandidateAdmissionResult(action="rejected",
                                         rejection_reason="empty")

    try:
        from core.gdu.entity_resolution import is_garbage_name
        garbage, reason = is_garbage_name(surface_form)
        if garbage:
            return CandidateAdmissionResult(action="rejected",
                                             rejection_reason=f"garbage: {reason}")
    except Exception:
        pass

    role = _role_for_type(proposed_type)
    nres = _normalize(surface_form, role=role)
    normalized = nres.normalized or ""

    # ── Step 2 (BEFORE rejection): thesaurus lookup ────────────────────────
    # Thesaurus hits OVERRIDE reject_patterns. "USA" matches the iso_code
    # reject pattern, but the gazetteer recognises it as United States —
    # the gazetteer is the stronger signal.
    thesaurus_hit = _resolve(normalized, role=role)

    # ── Step 1: rejected path (only if thesaurus missed too) ───────────────
    if nres.rejected and not thesaurus_hit:
        _write_rejected_audit(
            tenant_id=tenant_id,
            surface_form=surface_form,
            normalized=normalized,
            proposed_type=proposed_type,
            proposed_subtype=proposed_subtype,
            signal_rid=signal_rid,
            statement_id=statement_id,
            reason=nres.rejection_reason or "rejected",
            type_evidence=type_evidence,
            dsn=dsn,
        )
        return CandidateAdmissionResult(
            action="rejected",
            rejection_reason=nres.rejection_reason,
            normalized_name=normalized,
        )

    canonical_name = surface_form
    canonical_category = _category_for_subtype(proposed_subtype or proposed_type)
    canonical_subtype = proposed_subtype or proposed_type
    identifiers = dict(type_evidence or {})  # caller-provided ids carry through
    evidence_tier = "G"  # floor — bare extractor capture
    region_hint: Optional[str] = None
    role_hint: Optional[str] = None
    sector_hint: Optional[str] = None
    thesaurus_source: Optional[str] = None
    extra: dict = {}

    if thesaurus_hit:
        canonical_name = thesaurus_hit.canonical_name
        canonical_category = thesaurus_hit.canonical_type or canonical_category
        canonical_subtype = thesaurus_hit.canonical_subtype or canonical_subtype
        identifiers.update(thesaurus_hit.identifiers or {})
        evidence_tier = thesaurus_hit.evidence_tier
        region_hint = thesaurus_hit.region_hint
        role_hint = thesaurus_hit.role_hint
        sector_hint = thesaurus_hit.sector_hint
        thesaurus_source = thesaurus_hit.source
        extra = dict(thesaurus_hit.extra or {})

    # Identifier-decisive tier stamp. A caller-provided decisive identifier
    # (FEC committee_id, EIN, CIK, UEI, QID, …) IS Tier-A evidence — it names a
    # distinct registered entity on its own.
    #
    # The rule now lives in ONE place. It used to be inline here, which is why
    # `ensure_substrate_for_entity` — handed the same identifiers on the gateway
    # path — stamped its hardcoded default instead and left 520 `fec_id` person
    # concepts at tier D beside 537 identical ones at A.
    from core.ldu.decisive_identifiers import tier_with_decisive
    evidence_tier = tier_with_decisive(evidence_tier, identifiers)

    # ── Step 2.5: LDU subtype lock-down (entityvocab.entity_subtypes) ─────
    # The thesaurus may have left canonical_subtype as the raw LLM value
    # (gpe, org, abstract, etc.). Validate against the closed entity-pack
    # vocabulary BEFORE building the subtype_path or minting any concept.
    try:
        from core.ldu.subtype_validator import validate_for_ldu
        _val = validate_for_ldu(canonical_subtype)
        if _val.action == "accept":
            # Normalize to canonical (e.g. 'org' → 'organization')
            if _val.canonical_subtype and _val.canonical_subtype != canonical_subtype:
                canonical_subtype = _val.canonical_subtype
            canonical_category = "entity"
        elif _val.action == "route":
            # Caller proposed an entity but the type belongs in a different
            # category (e.g. 'gpe' belongs in places). Re-target.
            target_cat, target_sub = _val.route_to
            canonical_category = target_cat
            canonical_subtype = target_sub
        elif _val.action == "drop":
            # Pure LLM/NER garbage. Audit-log + reject.
            _write_rejected_audit(
                tenant_id=tenant_id,
                surface_form=surface_form,
                normalized=normalized,
                proposed_type=proposed_type,
                proposed_subtype=proposed_subtype,
                signal_rid=signal_rid,
                statement_id=statement_id,
                reason=f"ldu_subtype_dropped: {_val.reason}",
                type_evidence=type_evidence,
                dsn=dsn,
            )
            return CandidateAdmissionResult(
                action="rejected",
                rejection_reason=f"ldu_subtype_dropped: {_val.reason}",
                normalized_name=normalized,
            )
        # action == "unknown" → fall through to existing logic; the row
        # lands as gdu_canonical=FALSE per the entities trigger, and the
        # auto_validator + ldup_promoter pick it up later.
    except Exception as e:
        log.warning("ldu subtype validator failed for %r: %s — passing through",
                    canonical_subtype, e)

    subtype_path = f"{canonical_category}.{canonical_subtype}"


    # ── Step 3: ensure form_nodes row ──────────────────────────────────────
    form_id, form_address = _ensure_form_node(
        tenant_id=tenant_id,
        surface_form=surface_form,
        normalized=normalized,
        signal_rid=signal_rid,
        dsn=dsn,
    )

    # ── Step 4: identifier-based concept lookup ────────────────────────────
    concept_row = None
    if identifiers:
        concept_row = _find_concept_by_identifier(
            tenant_id=tenant_id, identifiers=identifiers, dsn=dsn,
        )

    # ── Step 5: name-based concept lookup ──────────────────────────────────
    if not concept_row:
        concept_row = _find_concept_by_name(
            tenant_id=tenant_id,
            canonical_name=canonical_name,
            subtype_path=subtype_path,
            dsn=dsn,
        )

    if concept_row:
        # Bind form to existing concept; accumulate signal; maybe attach.
        return _attach_to_concept(
            form_id=form_id,
            concept_row=concept_row,
            surface_form=surface_form,
            signal_rid=signal_rid,
            statement_id=statement_id,
            identifiers=identifiers,
            evidence_tier=evidence_tier,
            thesaurus_source=thesaurus_source,
            tenant_id=tenant_id,
            dsn=dsn,
        )

    # ── Step 6: mint new concept ───────────────────────────────────────────
    return _mint_new_concept(
        form_id=form_id,
        tenant_id=tenant_id,
        canonical_name=canonical_name,
        canonical_category=canonical_category,
        canonical_subtype=canonical_subtype,
        subtype_path=subtype_path,
        identifiers=identifiers,
        evidence_tier=evidence_tier,
        type_evidence=dict(type_evidence or {}),
        thesaurus_source=thesaurus_source,
        thesaurus_extra=extra,
        region_hint=region_hint,
        role_hint=role_hint,
        sector_hint=sector_hint,
        signal_rid=signal_rid,
        statement_id=statement_id,
        normalized=normalized,
        surface_form=surface_form,
        dsn=dsn,
    )


# ── Form node helper ────────────────────────────────────────────────────────

def _surface_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _ensure_form_node(
    *,
    tenant_id: str,
    surface_form: str,
    normalized: str,
    signal_rid: int,
    dsn: Optional[str],
) -> tuple[int, str]:
    """Upsert a form_nodes row. Returns (form_id, form_address).

    form_address format: "11.0.<sha16>" — form is category 11, single
    bucket for now (subtype 0). Address is deterministic across nodes
    because it's derived from the normalized surface form hash.

    `normalized` IS RE-FOLDED HERE AND THE PARAMETER IS ADVISORY. A keyword named
    `normalized` looks like a precondition and is really an honour system: nothing
    stops a caller passing a raw name, and two of this function's three call sites
    did exactly that — `ensure_substrate_for_entity` passed `canonical_name` and
    `write_persona_split_substrate` passed `form_surface`, both unfolded.

    Measured on node-A 2026-08-10: 7,929 form rows carry a non-lowercase
    `surface_form_normalized`, in 5,699 case clusters covering 11,440 forms and
    reaching 11,480 concepts. `johnson & johnson`, `Johnson & Johnson` and
    `JOHNSON & JOHNSON` are three separate forms with three different hashes and
    therefore three separate identities. None before June; 6,734 in July and 1,196
    in August, the newest minted minutes before this was written — a LIVE defect,
    not legacy residue.

    Fixing the two callers would have left the trap armed for the third. The
    function that WRITES `surface_form_normalized` is the only place that can
    guarantee what it means, so it computes the fold instead of trusting it.
    `fold_surface` is idempotent and role-independent, so the one call site that
    was already correct is unaffected — its value folds to itself.

    NO BACKFILL: the 11,440 existing case-variant forms stay. This stops the
    source; merging what it already produced is a separate, identity-moving
    decision that must not ride along on a normaliser fix.
    """
    from core.gdu.normalize import fold_surface

    normalized = fold_surface(normalized or surface_form)
    h = _surface_hash(normalized)
    form_address = f"11.0.{h}"

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO form_nodes
                  (tenant_id, form_address, surface_form,
                   surface_form_normalized, surface_form_hash,
                   signal_count, source_signal_rids,
                   first_seen_at, last_seen_at)
                VALUES (%s, %s, %s, %s, %s, 1, ARRAY[%s::bigint],
                        NOW(), NOW())
                ON CONFLICT (tenant_id, surface_form_hash) DO UPDATE SET
                  signal_count = form_nodes.signal_count + 1,
                  last_seen_at = NOW(),
                  source_signal_rids = CASE
                    WHEN %s::bigint = ANY(form_nodes.source_signal_rids)
                      THEN form_nodes.source_signal_rids
                    ELSE array_append(form_nodes.source_signal_rids, %s::bigint)
                  END
                RETURNING form_id
                """,
                (tenant_id, form_address, surface_form, normalized, h,
                 signal_rid, signal_rid, signal_rid),
            )
            form_id = cur.fetchone()[0]
        conn.commit()
    return int(form_id), form_address


# ── Concept lookup helpers ──────────────────────────────────────────────────

def _find_concept_by_identifier(
    *,
    tenant_id: str,
    identifiers: dict,
    dsn: Optional[str],
) -> Optional[dict]:
    """Look up a concept by any matching identifier (QID, CIK, EIN, ...).

    Uses the JSONB GIN index on concept_nodes.identifiers for O(1)-ish
    containment checks. Returns the concept row as a dict, or None.
    """
    if not identifiers:
        return None

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Sort identifier keys so the probe order is deterministic across
            # nodes (dict iteration order must not decide which concept binds).
            for k, v in sorted(identifiers.items()):
                if not v:
                    continue
                cur.execute(
                    """
                    SELECT concept_id, concept_address, subtype_path,
                           canonical_name, canonical_subtype,
                           promotion_status, promoted_to_entity_id,
                           identifiers, type_evidence, evidence_tier,
                           signal_count
                    FROM concept_nodes
                    WHERE tenant_id = %s
                      AND identifiers @> jsonb_build_object(%s, %s::text)
                      AND promotion_status IN ('pending', 'promoted')
                    ORDER BY signal_count DESC NULLS LAST, concept_id ASC
                    LIMIT 1
                    """,
                    (tenant_id, k, str(v)),
                )
                row = cur.fetchone()
                if row:
                    return _row_to_concept_dict(row)
    return None


def _find_concept_by_name(
    *,
    tenant_id: str,
    canonical_name: str,
    subtype_path: str,
    dsn: Optional[str],
) -> Optional[dict]:
    """Look up a concept by canonical_name + subtype_path. The
    subtype_path scope avoids merging different categories that
    happen to share a name."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT concept_id, concept_address, subtype_path,
                       canonical_name, canonical_subtype,
                       promotion_status, promoted_to_entity_id,
                       identifiers, type_evidence, evidence_tier,
                       signal_count
                FROM concept_nodes
                WHERE tenant_id = %s
                  AND canonical_name = %s
                  AND subtype_path = %s
                  AND promotion_status IN ('pending', 'promoted')
                ORDER BY signal_count DESC NULLS LAST, concept_id ASC
                LIMIT 1
                """,
                (tenant_id, canonical_name, subtype_path),
            )
            row = cur.fetchone()
            if row:
                return _row_to_concept_dict(row)
    return None


def _row_to_concept_dict(row) -> dict:
    return {
        "concept_id": int(row[0]),
        "concept_address": row[1],
        "subtype_path": row[2],
        "canonical_name": row[3],
        "canonical_subtype": row[4],
        "promotion_status": row[5],
        "promoted_to_entity_id": row[6],
        "identifiers": row[7] or {},
        "type_evidence": row[8] or {},
        "evidence_tier": row[9],
        "signal_count": int(row[10] or 0),
    }


# ── Address allocation ─────────────────────────────────────────────────────

def _allocate_concept_address(
    *,
    tenant_id: str,
    subtype_path: str,
    canonical_category: str,
    canonical_subtype: str,
    region_hint: Optional[str],
    role_hint: Optional[str],
    sector_hint: Optional[str],
    canonical_name: str,
    cur,
) -> tuple[str, int, str]:
    """Allocate the next concept_address for a subtype_path, honoring
    the subtype's divisor + partition hint when available.

    Returns (concept_address, depth, effective_subtree_path).

    Strategy:
      - If a divisor exists for subtype_path:
          - If a hint maps to a partition_label, use it.
          - Else hash-bucket canonical_name into the divisor space so
            unhinted admissions still distribute across depth-2
            partitions instead of hammering the depth-1 parent.
          - Allocate at depth 2 in that partition's subtree.
      - Else allocate at depth 1 (effective_subtree = subtype_path).
      - The slot counter lives in concept_address_sequences keyed on
        the effective subtree path.

    Concurrency: an advisory transaction lock keyed on the effective
    subtree path serialises concurrent allocators for the same bucket,
    so the read-allocate-insert window for (slot, concept_nodes) can
    never collide with another worker's allocation.
    """
    cat_idx = get_category_index(canonical_category)
    sub_idx = get_subtype_index(canonical_category, canonical_subtype)
    if cat_idx < 0 or sub_idx < 0:
        # Fallback for unknown vocab — keep going but mark as depth 1
        # at the next slot of an unrooted path.
        cat_idx = 12  # concept category
        sub_idx = 0

    # See if we have a divisor + partition match.
    partition_index = None
    partition_label = None

    cur.execute(
        """SELECT divisor, partition_labels FROM subtype_divisors
            WHERE subtype_path = %s""",
        (subtype_path,),
    )
    row = cur.fetchone()
    if row:
        divisor, labels = row
        # Prefer an explicit hint when it matches a known label.
        hint = region_hint or role_hint or sector_hint
        if hint and labels:
            for entry in labels:
                if not isinstance(entry, dict):
                    continue
                if entry.get("label") == hint:
                    partition_index = int(entry.get("index"))
                    partition_label = hint
                    break
        # No matching hint but a divisor exists — hash-bucket the
        # canonical_name across the divisor space. Deterministic per
        # name, so retries land in the same partition.
        if partition_index is None and labels and divisor:
            seed = hashlib.sha1(
                (canonical_name or "").encode("utf-8")
            ).digest()
            partition_index = seed[0] % int(divisor)
            for entry in labels:
                if isinstance(entry, dict) and int(entry.get("index", -1)) == partition_index:
                    partition_label = entry.get("label")
                    break

    if partition_index is not None and partition_label is not None:
        # Depth 2 allocation: cat_idx.sub_idx.partition.slot
        effective_path = f"{subtype_path}.{partition_label}"
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (effective_path,))
        slot = _next_slot(cur, effective_path)
        return (
            f"{cat_idx}.{sub_idx}.{partition_index}.{slot}",
            2,
            effective_path,
        )

    # Depth 1 fallback — only reached when no divisor row exists for
    # this subtype. Still serialised per-bucket via advisory lock.
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (subtype_path,))
    slot = _next_slot(cur, subtype_path)
    return (f"{cat_idx}.{sub_idx}.{slot}", 1, subtype_path)


def _next_slot(cur, subtree_path: str) -> int:
    """Atomically return the next free slot (0-59) for a subtree and
    flag overflow when we cross 59. Returns the allocated slot.

    NB: overflow handling (cascade) is not implemented in this
    function — slot 60+ would land out of range. The slot field has a
    CHECK constraint <=60, so an overflow attempt becomes a constraint
    violation that the caller can catch and route to cascade.
    """
    cur.execute(
        """
        INSERT INTO concept_address_sequences (subtype_path, next_slot)
        VALUES (%s, 1)
        ON CONFLICT (subtype_path) DO UPDATE SET
          next_slot  = concept_address_sequences.next_slot + 1,
          updated_at = NOW(),
          overflowed = (concept_address_sequences.next_slot + 1) >= 60
        RETURNING next_slot - 1
        """,
        (subtree_path,),
    )
    row = cur.fetchone()
    return int(row[0]) if row else 0


# ── Bind + accumulate helpers ──────────────────────────────────────────────

def _ensure_binding(
    *,
    form_id: int,
    concept_id: int,
    source: Optional[str],
    confidence: float,
    evidence: Optional[dict],
    cur,
) -> None:
    """Upsert a form_concept_binding."""
    cur.execute(
        """
        INSERT INTO form_concept_bindings
          (form_id, concept_id, binding_confidence, binding_source,
           binding_evidence)
        VALUES (%s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (form_id, concept_id) DO UPDATE SET
          binding_confidence = GREATEST(form_concept_bindings.binding_confidence,
                                        EXCLUDED.binding_confidence),
          active = TRUE
        """,
        (form_id, concept_id, float(confidence), source or "unknown",
         json.dumps(evidence) if evidence else None),
    )


def _accumulate_concept_signal(
    *,
    concept_id: int,
    signal_rid: int,
    identifiers: dict,
    evidence_tier: Optional[str],
    thesaurus_source: Optional[str],
    cur,
) -> None:
    """Bump signal_count + source_signal_rids + merge identifiers/evidence."""
    cur.execute(
        """
        UPDATE concept_nodes SET
          signal_count = signal_count + 1,
          source_signal_rids = CASE
            WHEN %s::bigint = ANY(source_signal_rids) THEN source_signal_rids
            ELSE array_append(source_signal_rids, %s::bigint)
          END,
          identifiers   = COALESCE(identifiers, '{}'::jsonb)
                          || COALESCE(%s::jsonb, '{}'::jsonb),
          type_evidence = COALESCE(type_evidence, '{}'::jsonb)
                          || jsonb_build_object('last_thesaurus_source', %s),
          evidence_tier = CASE
            WHEN evidence_tier IS NULL THEN %s
            WHEN %s IS NULL THEN evidence_tier
            WHEN %s < evidence_tier THEN %s  -- A beats B beats C ...
            ELSE evidence_tier
          END,
          updated_at = NOW()
        WHERE concept_id = %s
        """,
        (signal_rid, signal_rid,
         json.dumps(identifiers) if identifiers else None,
         thesaurus_source,
         evidence_tier, evidence_tier, evidence_tier, evidence_tier,
         concept_id),
    )


# ── Attach to existing concept ─────────────────────────────────────────────

def _attach_to_concept(
    *,
    form_id: int,
    concept_row: dict,
    surface_form: str,
    signal_rid: int,
    statement_id: Optional[int],
    identifiers: dict,
    evidence_tier: str,
    thesaurus_source: Optional[str],
    tenant_id: str,
    dsn: Optional[str],
) -> CandidateAdmissionResult:
    concept_id = concept_row["concept_id"]

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            _ensure_binding(
                form_id=form_id,
                concept_id=concept_id,
                source=thesaurus_source or "name_match",
                confidence=0.95 if thesaurus_source else 0.85,
                evidence={"surface_form": surface_form,
                          "signal_rid": signal_rid,
                          "statement_id": statement_id} if statement_id else
                         {"surface_form": surface_form, "signal_rid": signal_rid},
                cur=cur,
            )
            _accumulate_concept_signal(
                concept_id=concept_id,
                signal_rid=signal_rid,
                identifiers=identifiers,
                evidence_tier=evidence_tier,
                thesaurus_source=thesaurus_source,
                cur=cur,
            )
        conn.commit()

    # If concept is already promoted, also bump the entity via gateway.
    if concept_row["promoted_to_entity_id"]:
        entity_id = int(concept_row["promoted_to_entity_id"])
        try:
            from core.gdu.gateway import get_gateway
            get_gateway().admit_entity(
                tenant_id=tenant_id,
                name=surface_form,
                entity_type=concept_row["canonical_subtype"] or "person",
                signal_rid=signal_rid,
                identifiers={},
                context="ldu.admit_candidate.attach_promoted",
                dsn=dsn,
            )
        except Exception as e:
            log.warning("gateway re-admit failed for entity_id=%s: %s",
                        entity_id, e)
        return CandidateAdmissionResult(
            action="attached_to_gdu_entity",
            form_id=form_id,
            concept_id=concept_id,
            entity_id=entity_id,
            concept_address=concept_row["concept_address"],
            canonical_name=concept_row["canonical_name"],
            canonical_subtype=concept_row["canonical_subtype"],
            normalized_name=surface_form,
            evidence_tier=concept_row["evidence_tier"],
            thesaurus_source=thesaurus_source,
            node_id=f"entity:{entity_id}",
            category=concept_row["subtype_path"].split(".")[0] if "." in concept_row["subtype_path"] else None,
            subtype=concept_row["canonical_subtype"],
            is_new=False,
        )

    # Concept exists, not yet promoted.
    return CandidateAdmissionResult(
        action="bound_to_concept",
        form_id=form_id,
        concept_id=concept_id,
        concept_address=concept_row["concept_address"],
        canonical_name=concept_row["canonical_name"],
        canonical_subtype=concept_row["canonical_subtype"],
        normalized_name=surface_form,
        evidence_tier=concept_row["evidence_tier"],
        thesaurus_source=thesaurus_source,
        candidate_id=concept_id,
    )


# ── Mint new concept ───────────────────────────────────────────────────────

def _mint_new_concept(
    *,
    form_id: int,
    tenant_id: str,
    canonical_name: str,
    canonical_category: str,
    canonical_subtype: str,
    subtype_path: str,
    identifiers: dict,
    evidence_tier: str,
    type_evidence: dict,
    thesaurus_source: Optional[str],
    thesaurus_extra: dict,
    region_hint: Optional[str],
    role_hint: Optional[str],
    sector_hint: Optional[str],
    signal_rid: int,
    statement_id: Optional[int],
    normalized: str,
    surface_form: str,
    dsn: Optional[str],
) -> CandidateAdmissionResult:
    stamp = get_lineage_stamp()
    pack_versions = stamp.get("pack_versions", {}) if isinstance(stamp, dict) else {}

    # Roll thesaurus hints into type_evidence for audit.
    full_evidence = dict(type_evidence)
    if thesaurus_source:
        full_evidence[f"{thesaurus_source}_match"] = canonical_name
    if region_hint:
        full_evidence["region_hint"] = region_hint
    if role_hint:
        full_evidence["role_hint"] = role_hint
    if sector_hint:
        full_evidence["sector_hint"] = sector_hint
    if thesaurus_extra:
        full_evidence["thesaurus_extra"] = thesaurus_extra

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # SAVEPOINT-wrapped allocation. A CHECK constraint violation
            # (slot >= 60 — subtype subtree is full without cascade)
            # would otherwise poison the outer transaction. Rolling
            # back to the savepoint lets us drop in a placeholder
            # address and continue the concept INSERT.
            cur.execute("SAVEPOINT alloc_address")
            try:
                concept_address, depth, _ = _allocate_concept_address(
                    tenant_id=tenant_id,
                    subtype_path=subtype_path,
                    canonical_category=canonical_category,
                    canonical_subtype=canonical_subtype,
                    region_hint=region_hint,
                    role_hint=role_hint,
                    sector_hint=sector_hint,
                    canonical_name=canonical_name,
                    cur=cur,
                )
                cur.execute("RELEASE SAVEPOINT alloc_address")
            except Exception as e:
                cur.execute("ROLLBACK TO SAVEPOINT alloc_address")
                log.warning("address allocation failed for %s: %s — using overflow placeholder",
                            subtype_path, e)
                # Use a content-derived deterministic placeholder so
                # the same surface form hits the same slot on retry,
                # avoiding duplicates while cascade is implemented.
                placeholder_suffix = hashlib.sha256(
                    normalized.encode("utf-8")
                ).hexdigest()[:8]
                concept_address = f"overflow:{subtype_path}:{placeholder_suffix}"
                depth = 1

            cur.execute(
                """
                INSERT INTO concept_nodes
                  (tenant_id, concept_address, subtype_path,
                   canonical_name, canonical_subtype, depth,
                   identifiers, type_evidence, evidence_tier,
                   signal_count, source_signal_rids,
                   promotion_status, pack_versions)
                VALUES (%s, %s, %s, %s, %s, %s,
                        %s::jsonb, %s::jsonb, %s,
                        1, ARRAY[%s::bigint],
                        'pending', %s::jsonb)
                RETURNING concept_id
                """,
                (
                    tenant_id, concept_address, subtype_path,
                    canonical_name, canonical_subtype, depth,
                    json.dumps(identifiers) if identifiers else "{}",
                    json.dumps(full_evidence) if full_evidence else "{}",
                    evidence_tier,
                    signal_rid,
                    json.dumps(pack_versions),
                ),
            )
            concept_id = int(cur.fetchone()[0])

            _ensure_binding(
                form_id=form_id,
                concept_id=concept_id,
                source=thesaurus_source or "minted_from_capture",
                confidence=0.99 if thesaurus_source else 0.6,
                evidence={"surface_form": surface_form,
                          "signal_rid": signal_rid},
                cur=cur,
            )
        conn.commit()

    return CandidateAdmissionResult(
        action="created_concept",
        form_id=form_id,
        concept_id=concept_id,
        concept_address=concept_address,
        canonical_name=canonical_name,
        canonical_subtype=canonical_subtype,
        normalized_name=normalized,
        evidence_tier=evidence_tier,
        thesaurus_source=thesaurus_source,
        candidate_id=concept_id,
        is_new=True,
        category=canonical_category,
        subtype=canonical_subtype,
    )


# ── Universal substrate writer (Phase 2 lockdown) ───────────────────────────

def ensure_substrate_for_entity(
    *,
    cur,
    tenant_id: str,
    entity_id: int,
    canonical_name: str,
    canonical_subtype: str,
    category: str = "entity",
    identifiers: Optional[dict] = None,
    type_evidence: Optional[dict] = None,
    evidence_tier: str = "D",
    binding_source: str = "gateway_admit",
    binding_confidence: float = 0.85,
) -> dict:
    """Idempotent base-60 substrate writer — ensures form_nodes,
    concept_nodes, and form_concept_bindings exist for an entity_id.

    Call this from ANY path that creates entities so every entity in
    the system has substrate from day one. Safe to call multiple times
    on the same entity — uses UPSERT semantics throughout.

    Designed for the gateway-level chokepoint: gateway.admit_entity
    calls this after creating the entities row, and substrate is then
    guaranteed for every entity regardless of which UI / worker / API
    path triggered the admission.

    Works for ALL profile types (person, organization, place, event,
    document, vessel, ...) — the category/subtype_path is computed
    from the canonical_subtype argument.

    Returns: {form_id, concept_id, concept_address, concept_is_new,
              binding_id (always upserted)}
    """
    identifiers = identifiers or {}
    type_evidence = type_evidence or {}

    # `evidence_tier` defaults to "D" here, and this path is handed the caller's
    # identifiers — so a person arriving through the gateway with an FEC id was
    # stamped D while the same person arriving through `admit_candidate` was
    # stamped A. The lift is applied here for the same reason `_ensure_form_node`
    # folds its own input: the function that WRITES the value is the only place
    # that can guarantee what it means.
    from core.ldu.decisive_identifiers import tier_with_decisive
    evidence_tier = tier_with_decisive(evidence_tier, identifiers)

    subtype_path = f"{category}.{canonical_subtype}"

    # 1. Form node — idempotent upsert via SHA-keyed UNIQUE constraint.
    form_id, _form_address = _ensure_form_node(
        tenant_id=tenant_id,
        surface_form=canonical_name,
        normalized=canonical_name,
        signal_rid=0,
        dsn=None,
    )

    # 2. Is there already a promoted concept for this entity_id?
    cur.execute(
        """
        SELECT concept_id, concept_address
          FROM concept_nodes
         WHERE tenant_id = %s
           AND promoted_to_entity_id = %s
         LIMIT 1
        """,
        (tenant_id, entity_id),
    )
    row = cur.fetchone()
    concept_is_new = False

    if row:
        concept_id = int(row[0])
        concept_address = row[1]
    else:
        # Mint a concept_node for this entity. Use savepoint so the
        # base-60 address allocation can fall back to a placeholder
        # without poisoning the outer txn.
        cur.execute("SAVEPOINT alloc_substrate_addr")
        try:
            concept_address, depth, _ = _allocate_concept_address(
                tenant_id=tenant_id,
                subtype_path=subtype_path,
                canonical_category=category,
                canonical_subtype=canonical_subtype,
                region_hint=type_evidence.get("region"),
                role_hint=None,
                sector_hint=None,
                canonical_name=canonical_name,
                cur=cur,
            )
            cur.execute("RELEASE SAVEPOINT alloc_substrate_addr")
        except Exception as e:
            cur.execute("ROLLBACK TO SAVEPOINT alloc_substrate_addr")
            log.warning("substrate: address allocation failed for %s: %s — overflow placeholder",
                        subtype_path, e)
            placeholder = hashlib.sha256(canonical_name.encode("utf-8")).hexdigest()[:8]
            concept_address = f"overflow:{subtype_path}:{placeholder}"
            depth = 1

        cur.execute(
            """
            INSERT INTO concept_nodes
              (tenant_id, concept_address, subtype_path,
               canonical_name, canonical_subtype, depth,
               identifiers, type_evidence, evidence_tier,
               signal_count, source_signal_rids,
               promotion_status, promoted_to_entity_id)
            VALUES (%s, %s, %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s,
                    0, ARRAY[]::bigint[],
                    'promoted', %s)
            ON CONFLICT (tenant_id, concept_address) DO UPDATE
              SET promoted_to_entity_id = EXCLUDED.promoted_to_entity_id,
                  promotion_status = 'promoted',
                  updated_at = NOW()
            RETURNING concept_id, (xmax = 0) AS inserted
            """,
            (
                tenant_id, concept_address, subtype_path,
                canonical_name, canonical_subtype, depth,
                json.dumps(identifiers),
                json.dumps(type_evidence),
                evidence_tier,
                entity_id,
            ),
        )
        cr = cur.fetchone()
        concept_id = int(cr[0])
        concept_is_new = bool(cr[1])

    # 3. Form ↔ concept binding — upsert via UNIQUE(form_id, concept_id).
    _ensure_binding(
        form_id=form_id,
        concept_id=concept_id,
        source=binding_source,
        confidence=binding_confidence,
        evidence={**type_evidence, "canonical_name": canonical_name},
        cur=cur,
    )

    return {
        "form_id": form_id,
        "concept_id": concept_id,
        "concept_address": concept_address,
        "concept_is_new": concept_is_new,
    }


# ── Merge consolidation (Phase 2 piece 4) ──────────────────────────────────

def consolidate_bindings_on_merge(
    *,
    cur,
    tenant_id: str,
    canonical_entity_id: int,
    duplicate_entity_id: int,
    merge_reason: str = "entity_merge",
) -> dict:
    """When entity B (duplicate) merges into entity A (canonical), update
    the base-60 substrate so the form's resolution distribution stays
    consistent.

    Per migration 093 — concept_nodes has merged_to_concept_id and
    promotion_status='merged' for the lineage chain. form_concept_
    bindings has an `active` flag for soft-deletion of stale bindings.

    Operations:
      1. Ensure both entities have concept rows (idempotent — minted if
         missing, no-op if present).
      2. Mark every B-concept as merged into the A-concept:
           promotion_status   = 'merged'
           merged_to_concept_id = A_concept_id
           promoted_to_entity_id = canonical_entity_id (one-hop lookup)
           merge_reason       = (provided)
      3. For every active form → B-concept binding, ensure a parallel
         form → A-concept binding exists (upsert; preserves audit).
      4. Mark the form → B-concept bindings inactive (active=FALSE).
         They stay as audit but no longer participate in resolution.

    Returns: {canonical_concept_id, merged_concept_ids[], rebound_form_ids[]}
    """
    # Step 1: ensure canonical has a concept (mint if missing — covers
    # the case where canonical was admitted pre-093 or by a path that
    # bypassed substrate).
    cur.execute(
        """
        SELECT concept_id FROM concept_nodes
         WHERE tenant_id = %s AND promoted_to_entity_id = %s
         ORDER BY signal_count DESC LIMIT 1
        """,
        (tenant_id, canonical_entity_id),
    )
    r = cur.fetchone()
    if r:
        canonical_concept_id = int(r[0])
    else:
        cur.execute(
            "SELECT canonical_name, canonical_type FROM entities WHERE entity_id = %s",
            (canonical_entity_id,),
        )
        e = cur.fetchone()
        if not e:
            return {
                "canonical_concept_id": None,
                "merged_concept_ids": [],
                "rebound_form_ids": [],
                "skipped": "canonical_entity_missing",
            }
        cname, ctype = e[0] or "", e[1] or "person"
        sub_result = ensure_substrate_for_entity(
            cur=cur,
            tenant_id=tenant_id,
            entity_id=canonical_entity_id,
            canonical_name=cname,
            canonical_subtype=ctype,
            category="entity",
            binding_source="merge_substrate_backfill",  # <=32 chars (varchar cap)
        )
        canonical_concept_id = sub_result["concept_id"]

    # Step 2: find all B-concepts (could be >1 if B had been previously
    # split or otherwise has multiple concept rows).
    cur.execute(
        """
        SELECT concept_id FROM concept_nodes
         WHERE tenant_id = %s AND promoted_to_entity_id = %s
        """,
        (tenant_id, duplicate_entity_id),
    )
    merged_concept_ids = [int(row[0]) for row in cur.fetchall()]

    if not merged_concept_ids:
        # Duplicate had no concept rows — substrate is clean for this
        # merge; nothing to consolidate.
        return {
            "canonical_concept_id": canonical_concept_id,
            "merged_concept_ids": [],
            "rebound_form_ids": [],
        }

    # Mark them merged.
    cur.execute(
        """
        UPDATE concept_nodes
           SET promotion_status       = 'merged',
               merged_to_concept_id   = %s,
               promoted_to_entity_id  = %s,
               merge_reason           = %s,
               updated_at             = NOW()
         WHERE concept_id = ANY(%s)
        """,
        (canonical_concept_id, canonical_entity_id, merge_reason,
         merged_concept_ids),
    )

    # Step 3: every form bound to a B-concept also gets bound to the
    # A-concept (so resolution distribution reflects the merge).
    cur.execute(
        """
        SELECT DISTINCT form_id FROM form_concept_bindings
         WHERE concept_id = ANY(%s) AND active = TRUE
        """,
        (merged_concept_ids,),
    )
    form_ids = [int(row[0]) for row in cur.fetchall()]
    for fid in form_ids:
        _ensure_binding(
            form_id=fid,
            concept_id=canonical_concept_id,
            source="merge_consolidation",
            confidence=0.85,
            evidence={"merged_from_entity_id": duplicate_entity_id,
                      "merge_reason": merge_reason},
            cur=cur,
        )

    # Step 4: deactivate B-concept bindings (audit-preserving soft-delete).
    cur.execute(
        """
        UPDATE form_concept_bindings
           SET active = FALSE
         WHERE concept_id = ANY(%s) AND active = TRUE
        """,
        (merged_concept_ids,),
    )

    return {
        "canonical_concept_id": canonical_concept_id,
        "merged_concept_ids": merged_concept_ids,
        "rebound_form_ids": form_ids,
    }


# ── Persona-split substrate writer (Phase 1 of persona clustering) ─────────

def write_persona_split_substrate(
    *,
    cur,
    tenant_id: str,
    form_surface: str,
    new_entity_id: int,
    new_canonical_name: str,
    canonical_subtype: str,
    subtype_path: str,
    identifiers: Optional[dict] = None,
    binding_evidence: Optional[dict] = None,
    binding_source: str = "persona_clustering_v1",
    evidence_tier: str = "D",
    binding_confidence: float = 0.85,
) -> dict:
    """Persona-split substrate writer — three substrate writes per split:

      1. New entity gets its own substrate (form for its canonical name
         + new concept + binding) via ensure_substrate_for_entity.
      2. The SHARED parent form (e.g. "Sam Rowan") gets a NEW
         binding to the new concept — this is the disambiguation
         expression: the form binds to multiple concepts.
      3. Best-effort anchor backfill — parent form → parent concept
         binding, in case the parent predates the substrate.

    After this runs, form("Sam Rowan") binds to BOTH the original
    concept (anchor) AND the new concept (split-off namesake). Future
    admissions of "Sam Rowan" with attribute context can route to
    either via gateway Layer 0 binding consumption.
    """
    category = subtype_path.split(".", 1)[0] if "." in subtype_path else "entity"

    # 1. Mint new substrate for the new entity (new form + new concept + binding).
    result = ensure_substrate_for_entity(
        cur=cur,
        tenant_id=tenant_id,
        entity_id=new_entity_id,
        canonical_name=new_canonical_name,
        canonical_subtype=canonical_subtype,
        category=category,
        identifiers=identifiers or {},
        type_evidence=dict(binding_evidence or {}),
        evidence_tier=evidence_tier,
        binding_source=binding_source,
        binding_confidence=binding_confidence,
    )
    new_concept_id = result["concept_id"]
    new_form_id = result["form_id"]

    # 2. Ensure the SHARED PARENT form exists and bind it to the NEW
    #    concept. This is the persona-split semantic: the form gains an
    #    additional binding so future ambiguous mentions route via the
    #    full distribution (gateway Layer 0).
    parent_form_id, _ = _ensure_form_node(
        tenant_id=tenant_id,
        surface_form=form_surface,
        normalized=form_surface,
        signal_rid=0,
        dsn=None,
    )
    if parent_form_id != new_form_id:
        _ensure_binding(
            form_id=parent_form_id,
            concept_id=new_concept_id,
            source=binding_source,
            confidence=binding_confidence,
            evidence={**(binding_evidence or {}),
                      "split_off_from_form": form_surface},
            cur=cur,
        )

    # 3. Anchor backfill — parent form → parent's existing concept.
    #    Belt-and-braces for entities admitted before the Phase 2
    #    substrate lockdown (where every gateway admission now writes
    #    bindings). If the parent already has a binding to its concept,
    #    _ensure_binding is a no-op upsert.
    anchor_concept_id: Optional[int] = None
    cur.execute(
        """
        SELECT c.concept_id
          FROM concept_nodes c
          JOIN entities e ON e.canonical_name = %s
                          AND e.entity_id = c.promoted_to_entity_id
         WHERE c.tenant_id = %s
           AND c.canonical_name = %s
           AND c.promoted_to_entity_id IS NOT NULL
           AND c.promoted_to_entity_id != %s
         LIMIT 1
        """,
        (form_surface, tenant_id, form_surface, new_entity_id),
    )
    r = cur.fetchone()
    if r:
        anchor_concept_id = int(r[0])
        _ensure_binding(
            form_id=parent_form_id,
            concept_id=anchor_concept_id,
            source="anchor_backfill_from_split",
            confidence=0.85,
            evidence={"backfilled_during_split_of": new_entity_id},
            cur=cur,
        )

    result["anchor_concept_id"] = anchor_concept_id
    result["parent_form_id"] = parent_form_id
    return result


# ── Rejected audit row ─────────────────────────────────────────────────────

def _write_rejected_audit(
    *,
    tenant_id: str,
    surface_form: str,
    normalized: str,
    proposed_type: Optional[str],
    proposed_subtype: Optional[str],
    signal_rid: int,
    statement_id: Optional[int],
    reason: str,
    type_evidence: Optional[dict],
    dsn: Optional[str],
) -> None:
    """Write to ldu_candidates as a rejected audit row. We don't write
    to form_nodes for rejected captures — they shouldn't pollute the
    form space."""
    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ldu_candidates
                      (tenant_id, surface_form, normalized_name,
                       proposed_type, proposed_subtype,
                       signal_count, source_signal_rids,
                       promotion_status, rejection_reason,
                       type_evidence)
                    VALUES (%s, %s, %s, %s, %s,
                            1, ARRAY[%s::bigint],
                            'rejected', %s, %s::jsonb)
                    """,
                    (tenant_id, surface_form, normalized or "",
                     proposed_type, proposed_subtype, signal_rid,
                     reason[:500],
                     json.dumps(type_evidence) if type_evidence else None),
                )
            conn.commit()
    except Exception as e:
        log.warning("rejected audit write failed: %s", e)
