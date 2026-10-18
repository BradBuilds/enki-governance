"""
GDU Gateway — The Final Collapse

Single authorized write path for all node, entity, and edge operations.
Nothing touches knowledge_nodes, entities, or knowledge_edges without
going through this module.

The gateway IS the collapse — multiple signals, multiple LDU-W extractions,
one final node at one base-60 address. You query the collapse, not the expansion.

Write path:
  admit_entity()  → entity resolution (dedup/merge) + base-60 address + knowledge_node
  admit_node()    → vocabulary validation + collapse_key merge + base-60 address
  admit_edge()    → edgevocab validation + type constraint check + create/strengthen
  route_to_ldup() → items that fail GDU mapping → provisional staging

Read path:
  get_node()   → single node with governance state
  get_entity() → redirect-resolved canonical entity
  get_edges()  → edges touching a node

Security:
  - Every write logged to gdu_admission_log with lineage stamp + hash chain
  - Tenant isolation on every operation
  - Invalid types fail-closed → LDU-P
  - Hash chain detects tampering outside the gateway
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection
from core.gdu.pack_registry import (
    get_category_index,
    get_subtype_index,
    get_valid_entity_subtypes,
    get_valid_subtypes,
    get_valid_edge_names,
    get_edge_definition,
    is_valid_node_type,
    is_valid_edge,
)
from core.gdu.address import assign_address
from core.ldu.lineage import get_lineage_stamp

_log = logging.getLogger("enki.gdu.gateway")


# ── Result types ────────────────────────────────────────────────────────

@dataclass
class AdmissionResult:
    """Result of admitting an entity or node through the gateway."""
    node_id: Optional[str] = None
    entity_id: Optional[int] = None
    base60_address: Optional[str] = None
    category: Optional[str] = None
    subtype: Optional[str] = None
    action: str = "none"          # admit, collapse, reject, route_ldup
    is_new: bool = False
    source_count: int = 1


@dataclass
class EdgeResult:
    """Result of admitting an edge through the gateway."""
    edge_id: Optional[str] = None
    relationship: str = ""
    action: str = "none"          # admit, strengthen, reject
    strength: float = 0.0


# ── Singleton ───────────────────────────────────────────────────────────

_GATEWAY: Optional[GDUGateway] = None


def get_gateway() -> GDUGateway:
    """Get the singleton gateway instance."""
    global _GATEWAY
    if _GATEWAY is None:
        _GATEWAY = GDUGateway()
    return _GATEWAY


class GDUGateway:
    """The collapse point. All DB writes for nodes/entities/edges go through here."""

    # ── WRITE PATH: Entity Admission ────────────────────────────────────

    def admit_entity(
        self,
        tenant_id: str,
        name: str,
        entity_type: str,
        signal_rid: int,
        identifiers: Optional[dict] = None,
        context: str = "",
        dsn: Optional[str] = None,
        attribute_hints: Optional[dict] = None,
    ) -> AdmissionResult:
        """Admit an entity through the gateway. Resolves, deduplicates, collapses.

        This is where entity collapse happens — multiple signals about the
        same entity converge to ONE node at ONE base-60 address.

        LDU-W → GDU boundary check (`validate_llm_output`) is the FIRST step.
        Rejected items route to LDU-P with a structured audit row in
        entity_ldu_p. Accepted items use the validator's canonical
        (category, subtype) — this handles intra-aliases (ship→vessel) and
        cross-category remaps (entity.country → place.country).

        attribute_hints (optional) — caller-provided context that lets
        the resolver consult form_concept_bindings during admission.
        Recognized keys: jurisdiction ('US' | 'UK' | ...), region (state
        / admin region), coords ({lat, lon}), dob_year, founded_year,
        industry. When the same surface form already binds to multiple
        concepts, these hints route the incoming signal to the right
        concept — the READ side of the cyclical collapse loop. Without
        hints, identifier-match still works; without either, falls
        through to name-based resolution.
        """
        identifiers = identifiers or {}

        # ── LDU-W → GDU boundary check (single point of enforcement) ──
        from core.gdu.pack_aware_prompt import validate_llm_output

        v = validate_llm_output(
            name=name,
            category="entity",
            raw_subtype=entity_type or "",
            role="mention",
        )

        if not v.accepted:
            self.route_to_ldup(
                tenant_id,
                {
                    "name": name,
                    "category": "entity",
                    "subtype": entity_type,
                    "identifiers": identifiers,
                },
                entity_type or "unknown",
                f"validate_llm_output:{v.reason}",
                signal_rid, dsn,
                suggested_category=v.category,
                suggested_subtype=v.subtype,
            )
            return AdmissionResult(action="route_ldup")

        # Use the validator's canonical answer post-alias / post-remap
        category = v.category               # "entity" or "place"
        canonical_subtype = v.subtype       # validated against pack

        with get_connection(dsn) as conn:
            # Resolve through entity resolution (5-layer dedup = collapse).
            # Layer 0 of _resolve_or_create_entity consults form_concept_
            # bindings using attribute_hints so the cyclical loop closes.
            entity_id, is_new = self._resolve_or_create_entity(
                conn, tenant_id, name, canonical_subtype, identifiers, signal_rid,
                attribute_hints=attribute_hints,
            )

            if not entity_id:
                return AdmissionResult(action="reject")

            # Assign base-60 address on new entity
            base60 = None
            if is_new:
                base60 = assign_address(conn, category, canonical_subtype)
                if base60:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE entities SET base60_address = %s WHERE entity_id = %s",
                            (base60, entity_id),
                        )
            else:
                # Fetch existing address
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT base60_address FROM entities WHERE entity_id = %s",
                        (entity_id,),
                    )
                    row = cur.fetchone()
                    base60 = row[0] if row else None

            # Ensure knowledge_node exists for this entity
            node_id = f"entity:{entity_id}"
            self._ensure_knowledge_node(
                conn, node_id, category, canonical_subtype, name,
                tenant_id, signal_rid, entity_id, base60, is_new,
            )

            # Phase 2 lockdown: every entity that lands here gets a
            # base-60 substrate row triple (form_nodes → concept_nodes →
            # form_concept_bindings) per migration 093. Idempotent —
            # safe to call when admit_candidate already wrote the
            # substrate via the LDU capture-time path. Best-effort:
            # logged but never aborts admission.
            try:
                from core.ldu.admit_candidate import ensure_substrate_for_entity
                with conn.cursor() as _sub_cur:
                    ensure_substrate_for_entity(
                        cur=_sub_cur,
                        tenant_id=tenant_id,
                        entity_id=entity_id,
                        canonical_name=name,
                        canonical_subtype=canonical_subtype,
                        category=category,
                        identifiers=identifiers,
                        type_evidence={"source_context": context or "gateway"},
                        binding_source="gateway_admit",
                    )
            except Exception as _sub_err:
                import logging as _logging
                _logging.getLogger("enki.gdu.gateway").warning(
                    "substrate write failed for entity_id=%s (admission still committed): %s",
                    entity_id, _sub_err,
                )

            # Federation Phase 2 — queue this entity for publication to the
            # hub. No-op if ENKI_HUB_URL not set. Below-threshold entities
            # (signal_count < 5) get skipped at the publisher.
            try:
                from core.federation import entity_publisher
                entity_publisher.queue_for_publish(entity_id, tenant_id=tenant_id)
            except Exception:
                # Federation must never break local entity admission.
                pass

            # Log admission
            action = "admit" if is_new else "collapse"
            remap_note = " (remapped)" if v.reason in ("intra_alias", "cross_remap") else ""
            self._log_admission(
                conn, tenant_id, node_id, entity_id, None,
                action, category, canonical_subtype, base60,
                entity_type, canonical_subtype,
                f"{'new entity' if is_new else 'collapsed into existing'}{remap_note}",
            )

            conn.commit()

        # Auto-research trigger (Phase 1 of research-collapse plan, May 8 2026).
        # Pre-rebuild this fired from node_builder.py:808; after the May 4
        # gateway-only rewrite that path is no longer the live ingest, so the
        # trigger never reached entity_investigations (0 rows in production).
        # Lazy import to avoid a circular at module load (node_builder imports
        # the gateway in turn).
        try:
            from core.workers.node_builder import _maybe_queue_investigation
            # We need the post-increment signal_count for the threshold check.
            # Cheap one-row lookup keyed on the entity_id we just admitted.
            with get_connection(dsn) as _c:
                with _c.cursor() as _cur:
                    _cur.execute(
                        "SELECT signal_count FROM entities WHERE entity_id = %s",
                        (entity_id,),
                    )
                    _row = _cur.fetchone()
            sc = int(_row[0]) if _row and _row[0] is not None else 0
            _maybe_queue_investigation(entity_id, tenant_id, entity_type, name, sc)
        except Exception as _trig_err:
            # Auto-trigger is best-effort. Surface failures at warning level so
            # they don't get lost in DEBUG noise (this was the silent-failure
            # mode that hid the root cause for weeks).
            import logging as _logging
            _logging.getLogger("enki.gdu.gateway").warning(
                "auto-research trigger failed for entity_id=%s name=%r: %s",
                entity_id, name, _trig_err,
            )

        return AdmissionResult(
            node_id=node_id,
            entity_id=entity_id,
            base60_address=base60,
            category=category,
            subtype=canonical_subtype,
            action=action,
            is_new=is_new,
        )

    # ── WRITE PATH: Node Admission ──────────────────────────────────────

    def admit_node(
        self,
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
        """Admit a non-entity node (event, topic, document, assessment, etc.).

        If collapse_key is provided, checks for existing node with same key
        and merges (collapses) instead of creating a duplicate.
        """
        # Validate against nodevocab
        if not is_valid_node_type(category, subtype):
            ldup_id = self.route_to_ldup(
                tenant_id,
                {"title": title, "category": category, "subtype": subtype},
                f"{category}.{subtype}",
                f"({category}, {subtype}) not in nodevocab",
                signal_rid, dsn,
            )
            return AdmissionResult(action="route_ldup")

        with get_connection(dsn) as conn:
            # Collapse check: does a node with this collapse_key already exist?
            if collapse_key:
                existing = self._find_by_collapse_key(
                    conn, tenant_id, category, subtype, collapse_key,
                )
                if existing:
                    # COLLAPSE — merge into existing node
                    node_id, source_count = existing
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE knowledge_nodes
                            SET source_count = source_count + 1, updated_at = NOW()
                            WHERE node_id = %s
                        """, (node_id,))

                    # Fetch existing address
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT base60_address FROM knowledge_nodes WHERE node_id = %s",
                            (node_id,),
                        )
                        row = cur.fetchone()
                        base60 = row[0] if row else None

                    self._log_admission(
                        conn, tenant_id, node_id, None, None,
                        "collapse", category, subtype, base60,
                        f"{category}.{subtype}", f"{category}.{subtype}",
                        f"collapsed into {node_id} (source_count={source_count + 1})",
                    )
                    conn.commit()

                    return AdmissionResult(
                        node_id=node_id,
                        base60_address=base60,
                        category=category,
                        subtype=subtype,
                        action="collapse",
                        is_new=False,
                        source_count=source_count + 1,
                    )

            # New node — assign base-60 address
            base60 = assign_address(conn, category, subtype)
            if base60 is None:
                # nodevocab has no slot for (category, subtype) — route to
                # LDU-P instead of inserting an ungoverned canonical row that
                # the enforce_governance trigger would reject.
                ldup_id = self.route_to_ldup(
                    tenant_id,
                    {"title": title, "category": category, "subtype": subtype},
                    f"{category}.{subtype}",
                    f"no address slot for ({category}, {subtype})",
                    signal_rid, dsn,
                )
                return AdmissionResult(action="route_ldup")

            # Generate deterministic node_id
            if collapse_key:
                node_id = f"{category}:{hashlib.sha1(collapse_key.encode()).hexdigest()[:12]}"
            else:
                node_id = f"{category}:sig_{signal_rid}"

            # Create the knowledge_node
            self._ensure_knowledge_node(
                conn, node_id, category, subtype, title,
                tenant_id, signal_rid, entity_id, base60, True,
                description=description, metadata=metadata,
                collapse_key=collapse_key,
            )

            self._log_admission(
                conn, tenant_id, node_id, entity_id, None,
                "admit", category, subtype, base60,
                f"{category}.{subtype}", f"{category}.{subtype}",
                "new node",
            )
            conn.commit()

        return AdmissionResult(
            node_id=node_id,
            entity_id=entity_id,
            base60_address=base60,
            category=category,
            subtype=subtype,
            action="admit",
            is_new=True,
        )

    # ── WRITE PATH: Edge Admission ──────────────────────────────────────

    def admit_edge(
        self,
        tenant_id: str,
        source_node_id: str,
        target_node_id: str,
        relationship: str,
        evidence: str = "",
        signal_rid: Optional[int] = None,
        dsn: Optional[str] = None,
    ) -> EdgeResult:
        """Admit an edge through the gateway. Validates against edgevocab."""
        valid_edges = get_valid_edge_names()
        if relationship not in valid_edges:
            _log.debug("Edge type '%s' not in edgevocab, skipping", relationship)
            return EdgeResult(action="reject", relationship=relationship)

        # Canonicalize endpoint order for SYMMETRIC (undirected) edges so the
        # edge_id is direction-independent — A co_occurs_with B and B
        # co_occurs_with A must be the SAME edge, and two nodes that saw the
        # pair in opposite order must agree. Asymmetric edges keep their
        # direction (it carries meaning).
        from core.gdu.pack_registry import is_symmetric_edge
        if is_symmetric_edge(relationship) and source_node_id > target_node_id:
            source_node_id, target_node_id = target_node_id, source_node_id

        # Generate deterministic edge_id
        edge_id = hashlib.sha1(
            f"{source_node_id}|{target_node_id}|{relationship}".encode()
        ).hexdigest()[:16]

        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                # Try to create or strengthen
                # ON CONFLICT targets the PK (edge_id) rather than the
                # (source, target, relationship) UNIQUE. Both index the same
                # set of rows because edge_id is sha1(source|target|rel), but
                # under concurrent inserts Postgres checks the PK first; if
                # ON CONFLICT names a different constraint the PK conflict
                # escapes as an exception. Targeting the PK closes that race.
                cur.execute("""
                    INSERT INTO knowledge_edges
                        (edge_id, project_id, source_node_id, target_node_id,
                         relationship, strength, evidence, ai_generated)
                    VALUES (%s, 'default', %s, %s, %s, 0.8, %s, TRUE)
                    ON CONFLICT (edge_id) DO UPDATE SET
                        strength = LEAST(knowledge_edges.strength + 0.1, 1.0),
                        evidence = CASE
                            WHEN LENGTH(EXCLUDED.evidence) > LENGTH(COALESCE(knowledge_edges.evidence, ''))
                            THEN EXCLUDED.evidence
                            ELSE knowledge_edges.evidence
                        END
                    RETURNING strength
                """, (edge_id, source_node_id, target_node_id,
                      relationship, evidence[:500] if evidence else ""))

                row = cur.fetchone()
                strength = row[0] if row else 0.8
                action = "admit" if strength <= 0.8 else "strengthen"

            self._log_admission(
                conn, tenant_id, None, None, edge_id,
                action, None, None, None,
                relationship, relationship,
                f"{source_node_id} → {target_node_id}",
            )
            conn.commit()

        return EdgeResult(
            edge_id=edge_id,
            relationship=relationship,
            action=action,
            strength=strength,
        )

    # ── WRITE PATH: LDU-P Routing ──────────────────────────────────────

    def route_to_ldup(
        self,
        tenant_id: str,
        raw_data: dict,
        input_type: str,
        reason: str,
        signal_rid: int,
        dsn: Optional[str] = None,
        suggested_category: Optional[str] = None,
        suggested_subtype: Optional[str] = None,
    ) -> Optional[str]:
        """Route an item that fails GDU mapping to LDU-P (provisional).

        Items here are not discarded — they're preserved for human review
        and potential future vocabulary expansion. This is the SINGLE
        write path into entity_ldu_p; all rejection paths converge here so
        the audit table has one source of truth.

        On-disk schema (16 cols): ldu_p_id, tenant_id, raw_name,
        normalized_name, suspected_type, source_pipeline, source_signal_id,
        confidence, evidence, rejection_reason, status, promoted_entity_id,
        reviewed_by, reviewed_at, metadata, created_at.

        suggested_category / suggested_subtype come from
        validate_llm_output when called from admit_entity — they record
        what the boundary check thought the item *should* have been.
        """
        ldup_id = f"ldup_{uuid.uuid4().hex[:12]}"

        raw_name = raw_data.get("name") or raw_data.get("title") or ""
        normalized = (raw_name or "").lower().strip()
        raw_category = raw_data.get("category")
        raw_subtype = raw_data.get("subtype") or raw_data.get("type") or input_type

        metadata = {
            "raw_category": raw_category,
            "raw_subtype": raw_subtype,
            "suggested_category": suggested_category,
            "suggested_subtype": suggested_subtype,
            "raw_data": raw_data,
        }

        try:
            with get_connection(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO entity_ldu_p
                            (ldu_p_id, tenant_id, raw_name, normalized_name,
                             suspected_type, source_pipeline, source_signal_id,
                             rejection_reason, metadata, status)
                        VALUES (%s, %s, %s, %s, %s,
                                'gdu_gateway', %s, %s, %s::jsonb, 'pending')
                        ON CONFLICT (ldu_p_id) DO NOTHING
                    """, (
                        ldup_id, tenant_id,
                        (raw_name or "(unnamed)")[:255],
                        normalized[:255],
                        (raw_subtype or "unknown")[:64],
                        str(signal_rid) if signal_rid is not None else None,
                        reason[:500],
                        json.dumps(metadata),
                    ))

                self._log_admission(
                    conn, tenant_id, None, None, None,
                    "route_ldup", None, None, None,
                    input_type, None,
                    reason,
                )
                conn.commit()

            _log.info("Routed to LDU-P: %s (%s) — %s", ldup_id, input_type, reason)
            return ldup_id

        except Exception as e:
            _log.debug("LDU-P routing failed: %s", e)
            return None

    # ── READ PATH ───────────────────────────────────────────────────────

    def get_node(self, node_id: str, tenant_id: str = "default",
                 dsn: Optional[str] = None) -> Optional[dict]:
        """Single node retrieval with governance state."""
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT node_id, node_type, subtype, title, description,
                           base60_address, base60_category, base60_subtype,
                           confidence, source_count, completeness_score,
                           completeness_tier, enrichment_status,
                           gdu_canonical, master_entity_id,
                           created_at, updated_at
                    FROM knowledge_nodes
                    WHERE node_id = %s AND tenant_id = %s
                """, (node_id, tenant_id))
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "node_id": row[0], "node_type": row[1], "subtype": row[2],
                    "title": row[3], "description": row[4],
                    "base60_address": row[5], "base60_category": row[6],
                    "base60_subtype": row[7],
                    "confidence": float(row[8]) if row[8] else None,
                    "source_count": row[9],
                    "completeness_score": float(row[10]) if row[10] else None,
                    "completeness_tier": row[11],
                    "enrichment_status": row[12],
                    "gdu_canonical": row[13],
                    "master_entity_id": row[14],
                    "created_at": row[15].isoformat() if row[15] else None,
                    "updated_at": row[16].isoformat() if row[16] else None,
                }

    def get_entity(self, entity_id: int, tenant_id: str = "default",
                   dsn: Optional[str] = None) -> Optional[dict]:
        """Single entity retrieval with redirect resolution."""
        # Follow redirect chain
        canonical_id = self._resolve_redirect(entity_id, dsn)

        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT entity_id, canonical_type, canonical_name,
                           normalized_name, aliases, identifiers,
                           signal_count, base60_address,
                           completeness_score, completeness_tier,
                           gdu_canonical, first_seen, last_seen
                    FROM entities
                    WHERE entity_id = %s AND tenant_id = %s
                """, (canonical_id, tenant_id))
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "entity_id": row[0], "canonical_type": row[1],
                    "canonical_name": row[2], "normalized_name": row[3],
                    "aliases": row[4] or [], "identifiers": row[5] or {},
                    "signal_count": row[6], "base60_address": row[7],
                    "completeness_score": float(row[8]) if row[8] else None,
                    "completeness_tier": row[9],
                    "gdu_canonical": row[10],
                    "first_seen": row[11].isoformat() if row[11] else None,
                    "last_seen": row[12].isoformat() if row[12] else None,
                }

    def get_edges(self, node_id: str, relationship: Optional[str] = None,
                  tenant_id: str = "default",
                  dsn: Optional[str] = None) -> list[dict]:
        """Edges touching a node (as source or target)."""
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                if relationship:
                    cur.execute("""
                        SELECT edge_id, source_node_id, target_node_id,
                               relationship, strength, evidence
                        FROM knowledge_edges
                        WHERE (source_node_id = %s OR target_node_id = %s)
                          AND relationship = %s
                    """, (node_id, node_id, relationship))
                else:
                    cur.execute("""
                        SELECT edge_id, source_node_id, target_node_id,
                               relationship, strength, evidence
                        FROM knowledge_edges
                        WHERE source_node_id = %s OR target_node_id = %s
                    """, (node_id, node_id))

                return [
                    {
                        "edge_id": r[0], "source_node_id": r[1],
                        "target_node_id": r[2], "relationship": r[3],
                        "strength": float(r[4]) if r[4] else None,
                        "evidence": r[5],
                    }
                    for r in cur.fetchall()
                ]

    # ── Internal: Entity Resolution ─────────────────────────────────────

    @staticmethod
    def _name_token_superset(a: str, b: str) -> bool:
        """True if one name's word-tokens subset the other's — a base/expanded
        pair like 'Dana Vale' ⊆ 'Dana J. Vale'. The guard that lets a
        loose-form match merge real variants while refusing to fuse distinct
        people who merely share a surname ('Dana J. Vale' vs 'Dana A.
        Vale' — neither subsets the other)."""
        import re
        ta = frozenset(t for t in re.split(r"\s+", (a or "").lower().strip()) if t)
        tb = frozenset(t for t in re.split(r"\s+", (b or "").lower().strip()) if t)
        if not ta or not tb:
            return False
        return ta <= tb or tb <= ta

    def _attach_to_entity(self, cur, entity_id: int, incoming_name: str,
                          normalized: str, loose: Optional[str],
                          identifiers: Optional[dict] = None) -> None:
        """Attach an incoming signal to an existing entity: bump signal_count,
        merge in any new identifiers, AND converge canonical_name toward the
        MOST-SPECIFIC (longest) surface seen so far, demoting the less-specific
        form to an alias.

        Per project_collapse_canonical_name_principle: the canonical entity is
        the longer, fully-qualified form ("Boeing Company"); the short form
        ("Boeing") is a connection/alias. The choice is DETERMINISTIC (longer
        wins; equal length → lexicographically smaller), so two nodes that have
        seen the same surfaces converge on the same canonical_name (and thus the
        same canonical_entity_key) regardless of arrival ORDER. Promotion is
        monotonic (names only get more specific), so it never thrashes.
        """
        import psycopg2
        cur.execute(
            "SELECT canonical_name, normalized_name FROM entities WHERE entity_id = %s",
            (entity_id,))
        r = cur.fetchone()
        if not r:
            return
        # Merge in any new identifiers (existing values win on shared keys — the
        # caller has already ruled out conflicts before attaching).
        if identifiers:
            cur.execute(
                "UPDATE entities SET identifiers = %s::jsonb || COALESCE(identifiers, '{}'::jsonb) "
                "WHERE entity_id = %s", (json.dumps(identifiers), entity_id))
        cur_name, cur_norm = (r[0] or "").strip(), r[1] or ""
        inc = (incoming_name or "").strip()
        # more specific = longer; deterministic lexical tiebreak on equal length
        promote = bool(inc) and (
            len(inc) > len(cur_name)
            or (len(inc) == len(cur_name) and inc < cur_name)
        )
        # the demoted (less-specific) surface is kept as an alias = the "connection"
        demoted = (cur_name if promote else inc).lower()

        if promote:
            # canonical_name ALWAYS moves to the more-specific form. Whether
            # normalized_name also moves depends on the pack normalizer: e.g.
            # "Boeing" and "Boeing Company" BOTH normalize to "boeing", so the
            # match key is unchanged and only the display name + ckey change
            # (collision-free). When normalized_name DOES change, it can collide
            # with another entity's key — guard with a savepoint and degrade.
            if normalized and normalized != cur_norm:
                cur.execute("SAVEPOINT sp_promote")
                try:
                    cur.execute("""
                        UPDATE entities SET
                            canonical_name = %s,
                            normalized_name = %s,
                            normalized_name_loose = COALESCE(%s, normalized_name_loose),
                            aliases = (SELECT array_agg(DISTINCT a)
                                         FROM unnest(COALESCE(aliases, '{}') || ARRAY[%s]::text[]) a
                                        WHERE a IS NOT NULL AND a <> ''),
                            signal_count = signal_count + 1,
                            last_seen = NOW(), updated_at = NOW()
                        WHERE entity_id = %s
                    """, (inc, normalized, loose, demoted, entity_id))
                    cur.execute("RELEASE SAVEPOINT sp_promote")
                    return
                except psycopg2.errors.UniqueViolation:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_promote")
                    cur.execute("RELEASE SAVEPOINT sp_promote")
                    # fall through to a display-only promotion (normalized stays)
            # normalized_name unchanged → just promote the display name + ckey.
            cur.execute("""
                UPDATE entities SET
                    canonical_name = %s,
                    aliases = (SELECT array_agg(DISTINCT a)
                                 FROM unnest(COALESCE(aliases, '{}') || ARRAY[%s]::text[]) a
                                WHERE a IS NOT NULL AND a <> ''),
                    signal_count = signal_count + 1,
                    last_seen = NOW(), updated_at = NOW()
                WHERE entity_id = %s
            """, (inc, demoted, entity_id))
            return

        # not more specific → plain bump + keep the incoming surface as an alias
        cur.execute("""
            UPDATE entities SET signal_count = signal_count + 1,
                aliases = (SELECT array_agg(DISTINCT a)
                             FROM unnest(COALESCE(aliases, '{}') || ARRAY[%s]::text[]) a
                            WHERE a IS NOT NULL AND a <> ''),
                last_seen = NOW(), updated_at = NOW()
            WHERE entity_id = %s
        """, (demoted, entity_id))

    def _resolve_or_create_entity(
        self, conn, tenant_id: str, name: str, entity_type: str,
        identifiers: dict, signal_rid: int,
        attribute_hints: Optional[dict] = None,
    ) -> tuple[Optional[int], bool]:
        """Resolve entity via name-first dedup. Create if new. Returns (entity_id, is_new).

        Layered resolution (June 4 2026 — binding-consumption upgrade):
          0. **Form-concept binding consumption** — if the form has been
             admitted before and has bindings to one or more concepts,
             consult them. If incoming identifiers match an existing
             concept's identifiers (QID, CIK, candidate_id, ...), route
             directly to that concept's entity. If attribute_hints match
             a binding's evidence (jurisdiction, region, coords, ...),
             same. This closes the cyclical loop — past resolution
             decisions inform future ones, so the same surface form with
             different context routes to different concepts automatically.
          1. Strict normalized name match (pack-driven, strips Jr/Sr/titles)
          2. Alias match (incoming strict OR loose form against existing aliases)
          3. Loose-name fallback (variant collision) — auto-merges only when
             identifiers compatible, else returns the conflict candidate as
             entity_id BUT also marks the row as needing review via the
             gdu_admission_log entry. We do not block the write — investigation
             pollution is contained by the worker's admission gate.
          4. Create new entity. Seeds aliases with raw + strict + loose forms
             so future variants (Dana J. Vale → Dana Vale) hit Layer 2.

        Normalization is delegated to ``core.gdu.normalize.normalize``.
        Rejected names (Wikidata QID, ISO code, URL, hash, too short) route
        to LDU-P instead of creating a contaminated row.
        """
        from core.gdu.normalize import normalize as _norm_name
        from core.gdu.entity_resolution import normalize_entity_name_loose

        # Subtype lockdown — pass 1 of 2: accept-only normalization.
        # ('org' → 'organization' before role hint + name match runs.
        # route/drop/unknown fires later at the Layer 3 INSERT.)
        entity_type = (entity_type or "").strip()
        _ldu_verdict = None
        try:
            from core.ldu.subtype_validator import validate_for_ldu
            _ldu_verdict = validate_for_ldu(entity_type)
            if _ldu_verdict.action == "accept":
                if (_ldu_verdict.canonical_subtype
                        and _ldu_verdict.canonical_subtype != entity_type):
                    entity_type = _ldu_verdict.canonical_subtype
        except Exception as e:
            _log.warning(
                "subtype_validator failed in gateway for type=%r: %s",
                entity_type, e,
            )

        # Place gazetteer canonicalization. Map every surface variant of a place
        # to the gazetteer's canonical name AND its QID, so "United States",
        # "United States of America", "USA", "US" all converge — same canonical
        # name and same QID → same canonical_entity_key → one entity. The QID is
        # the authoritative collapse signal for places (same as CIK for orgs).
        _PLACE_SUBTYPES = {"country", "city", "admin_region", "named_area",
                           "maritime_area", "facility_location"}
        if entity_type in _PLACE_SUBTYPES:
            try:
                from core.gdu.gazetteer import lookup as _gaz_lookup
                _hit = _gaz_lookup(name)
                if _hit:
                    name = _hit.canonical
                    if _hit.qid:
                        identifiers = {**(identifiers or {}), "qid": _hit.qid}
            except Exception as _e:
                _log.debug("gazetteer lookup failed for %r: %s", name, _e)

        # Choose role hint: organization for org-ish types, person for the
        # rest. Place subtypes resolve through gazetteer / address paths.
        role = "organization" if entity_type in (
            "organization", "government_agency", "mass_media",
            "unit", "weapon_system", "product", "facility",
        ) else "person"

        nresult = _norm_name(name, role=role)
        if nresult.rejected:
            self.route_to_ldup(
                tenant_id,
                {"name": name, "category": "entity", "subtype": entity_type,
                 "identifiers": identifiers},
                entity_type or "unknown",
                f"normalize_rejected:{nresult.rejection_reason}",
                signal_rid,
            )
            return None, False
        normalized = nresult.normalized
        loose = normalize_entity_name_loose(name, entity_type) or normalized

        with conn.cursor() as cur:
            # Layer 0: form_concept_binding consumption.
            # If this surface form is already known and has bindings to
            # one or more promoted concepts, try to route the incoming
            # signal to the right concept based on identifier match or
            # attribute fingerprint. Falls through to L1 on no form, no
            # bindings, or ambiguous match.
            binding_routed = self._route_via_form_bindings(
                cur, tenant_id, normalized, entity_type,
                identifiers, attribute_hints,
            )
            if binding_routed is not None:
                self._attach_to_entity(cur, binding_routed, name, normalized, loose,
                                       identifiers)
                return binding_routed, False

            from core.gdu.identity import (
                identifier_conflict, IDENTIFIER_PRECEDENCE)
            inc_idents = {str(k).lower(): str(v)
                          for k, v in (identifiers or {}).items() if v}

            # Direct-match probe order. IDENTIFIER_PRECEDENCE is the federation
            # primary-key set (mirrored byte-for-byte by the migration-124
            # plpgsql function — do NOT change it here). But the gateway's live
            # collapse should also converge on registry ids that aren't part of
            # the cross-node key: an FEC committee_id, a SAM UEI, an
            # OpenCorporates company_number, an FEC candidate_id, a stock ticker.
            # Same value + no conflict = same entity regardless of surface. These
            # are probed AFTER the precedence ids so the strongest key still wins.
            _direct_match_keys = IDENTIFIER_PRECEDENCE + (
                "committee_id", "uei", "company_number", "candidate_id", "ticker")

            # Layer 0.5: direct strong-identifier match. A shared strong id
            # (CIK/QID/LEI/...) means the SAME real-world entity even when the
            # surface differs ("Boeing Company" ↔ "The Boeing Company" share CIK
            # 12927). The strongest probability signal — checked before names.
            for key in _direct_match_keys:
                val = inc_idents.get(key)
                if not val:
                    continue
                cur.execute("""
                    SELECT entity_id, identifiers FROM entities
                    WHERE tenant_id = %s AND identifiers @> jsonb_build_object(%s, %s::text)
                    ORDER BY signal_count DESC NULLS LAST, entity_id ASC
                    LIMIT 1
                """, (tenant_id, key, val))
                idrow = cur.fetchone()
                if idrow and not identifier_conflict(identifiers, idrow[1] or {}):
                    self._attach_to_entity(cur, idrow[0], name, normalized, loose,
                                           identifiers)
                    return idrow[0], False

            # A PERSON and an ORGANIZATION that happen to share a name are
            # DIFFERENT entities — a committee "CITIZENS FOR Dana Vale" must
            # never merge onto its treasurer, nor vice-versa. Guard the name-based
            # layers with a hard person↔org type check (identifier layers above
            # are already decisive and need no such guard).
            _PERSON_T = {"person", "individual"}
            _ORG_T = {"organization", "government_agency", "mass_media", "company",
                      "nonprofit", "committee", "unit", "weapon_system", "product",
                      "facility"}

            def _hard_type_conflict(cand_type: str) -> bool:
                a = (entity_type or "").lower()
                b = (cand_type or "").lower()
                return (a in _PERSON_T and b in _ORG_T) or \
                       (b in _PERSON_T and a in _ORG_T)

            # Layer 1: exact normalized name match (name-first). Skip any candidate
            # whose strong identifier CONFLICTS (two companies both "Acme", diff
            # CIK) or whose type is hard-incompatible (person vs organization).
            cur.execute("""
                SELECT entity_id, identifiers, canonical_type FROM entities
                WHERE tenant_id = %s AND normalized_name = %s
                ORDER BY signal_count DESC NULLS LAST, entity_id ASC
                LIMIT 5
            """, (tenant_id, normalized))
            for cand_id, cand_idents, cand_type in cur.fetchall():
                if (not identifier_conflict(identifiers, cand_idents or {})
                        and not _hard_type_conflict(cand_type)):
                    self._attach_to_entity(cur, cand_id, name, normalized, loose,
                                           identifiers)
                    return cand_id, False
            # all same-name candidates conflict on identifiers / type → fall through

            # Layer 2: alias match — try strict form, then loose form. Pull
            # up to 2 to detect ambiguity (two entities sharing this alias
            # → variant-collision territory, escalate to loose-name layer).
            cur.execute("""
                SELECT entity_id, identifiers, canonical_type FROM entities
                WHERE tenant_id = %s AND (%s = ANY(aliases) OR %s = ANY(aliases))
                ORDER BY signal_count DESC NULLS LAST, entity_id ASC
                LIMIT 5
            """, (tenant_id, normalized, loose))
            alias_compat = [eid for eid, idents, ctype in cur.fetchall()
                            if not identifier_conflict(identifiers, idents or {})
                            and not _hard_type_conflict(ctype)]

            if len(alias_compat) == 1:
                entity_id = alias_compat[0]
                self._attach_to_entity(cur, entity_id, name, normalized, loose,
                                       identifiers)
                return entity_id, False

            # Layer 2.5: loose-name fallback. If strict miss but multiple
            # entities share a loose form, OR identifiers conflict with the
            # candidate, log the ambiguity and create a new row — the data
            # is preserved, and disambiguation can run later (manual merge,
            # LLM verification, identifier arrival). Better to have two rows
            # we can merge than one wrong merge we can't undo.
            # Match pending rows too (most entities are pending) — the prior
            # gdu_canonical=TRUE filter meant variant surfaces of an unpromoted
            # entity never merged. Safety comes from the guards below, not from
            # restricting to canonical rows.
            cur.execute("""
                SELECT entity_id, identifiers, canonical_name FROM entities
                WHERE tenant_id = %s AND canonical_type = %s
                  AND normalized_name_loose = %s
                ORDER BY signal_count DESC NULLS LAST, entity_id ASC
                LIMIT 5
            """, (tenant_id, entity_type, loose))
            loose_rows = cur.fetchall()

            if len(loose_rows) == 1:
                # Exactly one candidate shares this loose form. Merge ONLY when
                # (a) no identifier conflict, AND (b) the names are token-superset
                # related — so "Dana Vale" merges into "Dana J. Vale", but
                # "Dana A. Vale" (different middle name) does NOT, and an
                # ambiguous bare form facing TWO loose-mates (len>1) never merges.
                cand_id, cand_idents, cand_name = loose_rows[0]
                if (not identifier_conflict(identifiers, cand_idents or {})
                        and self._name_token_superset(name, cand_name or "")):
                    self._attach_to_entity(cur, cand_id, name, normalized, loose,
                                           identifiers)
                    return cand_id, False
                # else: conflict or unrelated names → fall through to create new

            # Subtype lockdown — pass 2 of 2. All match layers missed; we
            # would CREATE here. If the type is route/drop/unknown, divert
            # to entity_ldu_p via the gateway's existing route_to_ldup so
            # we don't crash on migration 116's canonical_type CHECK.
            if _ldu_verdict and _ldu_verdict.action in ("route", "drop", "unknown"):
                if _ldu_verdict.action == "route" and _ldu_verdict.route_to:
                    rej = f"route:{_ldu_verdict.route_to[0]}.{_ldu_verdict.route_to[1]}"
                elif _ldu_verdict.action == "drop":
                    rej = f"ldu_drop:{_ldu_verdict.reason}"
                else:
                    rej = f"ldu_unknown:{entity_type or '<empty>'}"
                self.route_to_ldup(
                    tenant_id,
                    {"name": name, "category": "entity", "subtype": entity_type,
                     "identifiers": identifiers},
                    entity_type or "unknown",
                    rej,
                    signal_rid,
                )
                return None, False

            # Right-to-be-forgotten gate: never re-create an erased entity. If this
            # signal's content-address has been tombstoned, refuse to materialize it
            # and route the raw signal to LDU-P (the erasure sticks across re-ingest).
            from core.gdu.erasure import is_tombstoned
            from core.gdu.identity import canonical_entity_key as _ckey
            if is_tombstoned(cur, _ckey(name, entity_type, identifiers), tenant_id):
                self.route_to_ldup(
                    tenant_id,
                    {"name": name, "category": "entity", "subtype": entity_type,
                     "identifiers": identifiers},
                    entity_type or "unknown",
                    "tombstoned:right_to_be_forgotten", signal_rid)
                return None, False

            # Layer 3: create new entity.
            #
            # Aliases on create: only the raw user-input form if it differs
            # from the strict-normalized form (e.g. "Dana Vale" vs
            # "Dana Vale"). We do NOT seed the loose form into aliases —
            # the loose column owns variant-collision matching, and seeding
            # loose-as-alias would over-merge unrelated entities sharing a
            # root name (Dana J. Vale ↔ Dana A. Vale).
            #
            # The loose column is ALWAYS stored, even when equal to the
            # strict form, so the L2.5 fallback can find this entity from
            # any incoming variant.
            #
            # With migration 034 in place, (tenant_id, normalized_name)
            # WHERE gdu_canonical=TRUE is a partial unique index;
            # ON CONFLICT converges concurrent races into one row.
            stamp = get_lineage_stamp()
            seed_aliases: list[str] = []
            raw_lower = name.lower().strip()
            if raw_lower and raw_lower != normalized:
                seed_aliases.append(raw_lower)

            insert_args = (
                tenant_id, entity_type, name, normalized,
                loose,  # always store, even when == normalized
                seed_aliases,
                json.dumps(identifiers),
                stamp.get("active_set_version"),
                json.dumps(stamp.get("pack_versions", {})),
                signal_rid,
            )
            insert_sql = """
                INSERT INTO entities
                    (tenant_id, canonical_type, canonical_name, normalized_name,
                     normalized_name_loose,
                     aliases, identifiers, signal_count,
                     admitted_under_active_set, admitted_under_pack_versions,
                     gdu_canonical, ldu_t_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, 1, %s, %s::jsonb, TRUE, %s)
                ON CONFLICT (tenant_id, normalized_name)
                  WHERE gdu_canonical = TRUE
                    AND normalized_name IS NOT NULL
                    AND normalized_name <> ''
                  DO UPDATE SET
                    signal_count = entities.signal_count + 1,
                    last_seen    = NOW(),
                    updated_at   = NOW()
                RETURNING entity_id, (xmax = 0) AS inserted
            """
            # The ON CONFLICT above catches normalized_name collisions. But
            # post-wipe the canonical_entity_key trigger + its UNIQUE index can
            # ALSO be violated when a resolver miss produces a row whose ckey
            # collides with an existing entity that has a DIFFERENT
            # normalized_name (rare: pack-version skew / stale normalize). Treat
            # that as a match (bump the existing row), never a crash. No-op while
            # ckeys are NULL (the partial unique index ignores NULLs).
            import psycopg2
            cur.execute("SAVEPOINT sp_entity_create")
            try:
                cur.execute(insert_sql, insert_args)
                row = cur.fetchone()
                cur.execute("RELEASE SAVEPOINT sp_entity_create")
            except psycopg2.errors.UniqueViolation:
                cur.execute("ROLLBACK TO SAVEPOINT sp_entity_create")
                cur.execute("RELEASE SAVEPOINT sp_entity_create")
                # A ckey collision means an entity with the SAME name AND same
                # strongest identifier already exists → attach to it. Compute the
                # key WITH identifiers (the ckey is id-aware); a name-only key
                # would miss the row and wrongly reject.
                from core.gdu.identity import canonical_entity_key
                ckey = canonical_entity_key(name, entity_type, identifiers)
                cur.execute("""
                    SELECT entity_id FROM entities
                     WHERE tenant_id = %s AND canonical_entity_key = %s
                     ORDER BY signal_count DESC NULLS LAST, entity_id ASC
                     LIMIT 1
                """, (tenant_id, ckey))
                hit = cur.fetchone()
                if hit:
                    self._attach_to_entity(cur, hit[0], name, normalized, loose,
                                           identifiers)
                    return hit[0], False
                return None, False
            if row is None:
                return None, False
            return row[0], bool(row[1])

    # ── Internal: Binding Consumption (June 4 2026) ─────────────────────

    def _route_via_form_bindings(
        self, cur, tenant_id: str, normalized: str, entity_type: str,
        identifiers: Optional[dict], attribute_hints: Optional[dict],
    ) -> Optional[int]:
        """Consult form_concept_bindings to route an incoming signal to
        an existing concept when the surface form has been admitted
        before with multiple referents.

        This is the READ side of the cyclical collapse loop. Every prior
        admission of this surface form created a binding; here we use
        those bindings to route NEW signals correctly when the form is
        ambiguous.

        Routing strategy (decreasing confidence):
          1. **Identifier match** — incoming signal carries an
             identifier (QID, CIK, candidate_id, ...) that matches an
             existing concept's identifiers. Strongest signal; routes
             directly.
          2. **Single binding** — form has exactly one promoted concept
             and the type matches. No ambiguity.
          3. **Attribute fingerprint** — caller passed attribute_hints
             (jurisdiction, region, coords_bucket, dob_year, ...) and
             one binding's evidence is a clear winner. Uses hard vetoes
             on conflicting jurisdiction/coords/year same as the
             persona-clustering pairwise scorer.

        Returns the entity_id to route to, or None to fall through to
        the existing name-based resolution layers (which may create a
        new concept and thus a new binding).
        """
        import hashlib

        # 1. Look up the form. SHA-keyed deterministic, normalized.
        h = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
        cur.execute("""
            SELECT form_id FROM form_nodes
             WHERE tenant_id = %s AND surface_form_hash = %s
             LIMIT 1
        """, (tenant_id, h))
        r = cur.fetchone()
        if not r:
            return None  # First time seeing this surface form
        form_id = int(r[0])

        # 2. Pull all active bindings + the concept's identity attrs.
        cur.execute("""
            SELECT c.concept_id, c.promoted_to_entity_id,
                   c.canonical_subtype, c.identifiers, c.type_evidence
              FROM form_concept_bindings b
              JOIN concept_nodes c ON c.concept_id = b.concept_id
             WHERE b.form_id = %s
               AND b.active = TRUE
               AND c.promoted_to_entity_id IS NOT NULL
               AND c.promotion_status = 'promoted'
        """, (form_id,))
        bindings = cur.fetchall()
        if not bindings:
            return None

        # 3. Identifier match — strongest signal. Any shared identifier
        #    key with matching value is a direct route.
        if identifiers:
            for cid, ent_id, sub, c_ids, _evid in bindings:
                c_ids = c_ids or {}
                for k, v in identifiers.items():
                    if not v:
                        continue
                    if k in c_ids and str(c_ids[k]).strip() == str(v).strip():
                        return int(ent_id)

        # 4. Single binding + type compatible: route directly. This is
        #    the common case post-Layer-1 once a form's been admitted —
        #    consumption replaces the L1 lookup but with substrate
        #    awareness.
        if len(bindings) == 1:
            cid, ent_id, sub, _c_ids, _evid = bindings[0]
            # Treat NULL entity_type as wildcard; otherwise require match.
            if not entity_type or sub == entity_type:
                return int(ent_id)
            # Type mismatch — don't route, let L1 create a new concept.
            return None

        # 5. Multi-binding + attribute_hints: score each by fingerprint
        #    compatibility. Hard vetoes on jurisdiction/coords/year.
        if attribute_hints:
            best_score = 0.0
            best_ent: Optional[int] = None
            for cid, ent_id, sub, _c_ids, evid in bindings:
                if entity_type and sub != entity_type:
                    continue
                score = self._attribute_compatibility(
                    attribute_hints, evid or {}
                )
                if score > best_score:
                    best_score = score
                    best_ent = int(ent_id)
            if best_ent is not None and best_score >= 0.55:
                return best_ent

        # 6. Ambiguous: fall through. The new signal will hit L1
        #    (normalized_name match) which picks the canonical row;
        #    operators can split via the Personas panel later.
        return None

    @staticmethod
    def _attribute_compatibility(hints: dict, evidence: dict) -> float:
        """Score 0..1 compatibility between incoming attribute hints and
        an existing concept's binding evidence. Hard vetoes on known
        identity-defining mismatches (jurisdiction, coords, dob, founded)
        — same shape as the persona-clustering pairwise scorer."""
        score = 0.0
        weight = 0.0

        h_jur = hints.get("jurisdiction")
        e_jur = evidence.get("jurisdiction")
        if h_jur and e_jur:
            if h_jur != e_jur:
                return 0.0
            score += 0.4
            weight += 0.4

        h_reg = hints.get("region")
        e_reg = evidence.get("region")
        if h_reg and e_reg:
            if str(h_reg).strip().lower() == str(e_reg).strip().lower():
                score += 0.2
            weight += 0.2

        h_coords = hints.get("coords")
        e_coords = evidence.get("coords")
        if h_coords and e_coords and isinstance(h_coords, dict) and isinstance(e_coords, dict):
            dlat = abs(float(h_coords.get("lat") or 0) - float(e_coords.get("lat") or 0))
            dlon = abs(float(h_coords.get("lon") or 0) - float(e_coords.get("lon") or 0))
            if dlat > 1.0 or dlon > 1.0:
                return 0.0   # >~100km apart → different place
            score += 0.3
            weight += 0.3

        h_dob = hints.get("dob_year")
        e_dob = evidence.get("dob_year")
        if h_dob and e_dob:
            if abs(int(h_dob) - int(e_dob)) > 2:
                return 0.0
            score += 0.2
            weight += 0.2

        h_fy = hints.get("founded_year")
        e_fy = evidence.get("founded_year")
        if h_fy and e_fy:
            if abs(int(h_fy) - int(e_fy)) > 5:
                return 0.0
            score += 0.15
            weight += 0.15

        h_ind = hints.get("industry")
        e_ind = evidence.get("industry")
        if h_ind and e_ind:
            if str(h_ind).strip().lower() != str(e_ind).strip().lower():
                return 0.0
            score += 0.15
            weight += 0.15

        return score / weight if weight > 0 else 0.0


    # ── Internal: Knowledge Node ────────────────────────────────────────

    def _ensure_knowledge_node(
        self, conn, node_id: str, category: str, subtype: str,
        title: str, tenant_id: str, signal_rid: int,
        entity_id: Optional[int], base60: Optional[str],
        is_new: bool, description: str = "",
        metadata: Optional[dict] = None,
        collapse_key: Optional[str] = None,
    ):
        """Create or update a knowledge_node."""
        cat_idx = get_category_index(category)
        sub_idx = get_subtype_index(category, subtype)

        with conn.cursor() as cur:
            if not is_new:
                # Update existing
                cur.execute("""
                    UPDATE knowledge_nodes
                    SET source_count = source_count + 1, updated_at = NOW()
                    WHERE node_id = %s
                """, (node_id,))
                # Backfill base60 if missing
                if base60:
                    cur.execute("""
                        UPDATE knowledge_nodes
                        SET base60_address = %s, base60_category = %s, base60_subtype = %s
                        WHERE node_id = %s AND base60_address IS NULL
                    """, (base60, cat_idx, sub_idx, node_id))
                return

            # Create new
            cur.execute("""
                INSERT INTO knowledge_nodes
                    (node_id, project_id, tenant_id, title, description,
                     node_type, subtype, entity_type, entity_name,
                     master_entity_id, signal_rid,
                     base60_address, base60_category, base60_subtype,
                     confidence, source_count,
                     enrichment_status, gdu_canonical,
                     created_at, updated_at)
                VALUES (%s, 'default', %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s,
                        %s, %s, %s,
                        0.5, 1,
                        'pending', TRUE,
                        NOW(), NOW())
                ON CONFLICT (node_id) DO UPDATE SET
                    source_count = knowledge_nodes.source_count + 1,
                    base60_address = COALESCE(knowledge_nodes.base60_address, EXCLUDED.base60_address),
                    base60_category = COALESCE(knowledge_nodes.base60_category, EXCLUDED.base60_category),
                    base60_subtype = COALESCE(knowledge_nodes.base60_subtype, EXCLUDED.base60_subtype),
                    updated_at = NOW()
            """, (
                node_id, tenant_id, title[:500], (description or "")[:2000],
                category, subtype,
                subtype if category == "entity" else None,
                title if category == "entity" else None,
                entity_id, signal_rid,
                base60, cat_idx if cat_idx >= 0 else None,
                sub_idx if sub_idx >= 0 else None,
            ))

    # ── Internal: Collapse Key Lookup ───────────────────────────────────

    def _find_by_collapse_key(
        self, conn, tenant_id: str, category: str, subtype: str,
        collapse_key: str,
    ) -> Optional[tuple[str, int]]:
        """Find an existing node with the same collapse_key.

        Uses deterministic node_id generation from collapse_key to check.
        Returns (node_id, source_count) or None.
        """
        # The node_id is deterministic from the collapse_key
        candidate_id = f"{category}:{hashlib.sha1(collapse_key.encode()).hexdigest()[:12]}"

        with conn.cursor() as cur:
            cur.execute("""
                SELECT node_id, source_count FROM knowledge_nodes
                WHERE node_id = %s AND tenant_id = %s
            """, (candidate_id, tenant_id))
            row = cur.fetchone()
            if row:
                return (row[0], row[1])

        # Also check by title similarity for broader collapse
        normalized = collapse_key.lower().strip()[:100]
        with conn.cursor() as cur:
            cur.execute("""
                SELECT node_id, source_count FROM knowledge_nodes
                WHERE tenant_id = %s AND node_type = %s AND subtype = %s
                  AND LOWER(title) = %s
                LIMIT 1
            """, (tenant_id, category, subtype, normalized))
            row = cur.fetchone()
            if row:
                return (row[0], row[1])

        return None

    # ── Internal: Redirect Resolution ───────────────────────────────────

    def _resolve_redirect(self, entity_id: int,
                          dsn: Optional[str] = None) -> int:
        """Follow redirect chain to canonical entity_id."""
        visited = set()
        current = entity_id
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                while current not in visited:
                    visited.add(current)
                    cur.execute(
                        "SELECT new_entity_id FROM entity_redirects WHERE old_entity_id = %s",
                        (current,),
                    )
                    row = cur.fetchone()
                    if row:
                        current = row[0]
                    else:
                        break
        return current

    # ── Internal: Admission Logging ─────────────────────────────────────

    def _log_admission(
        self, conn, tenant_id: str,
        node_id: Optional[str], entity_id: Optional[int],
        edge_id: Optional[str],
        operation: str, category: Optional[str], subtype: Optional[str],
        base60: Optional[str], input_type: Optional[str],
        mapped_type: Optional[str], reason: str,
    ):
        """Write to gdu_admission_log with hash chain."""
        stamp = get_lineage_stamp()

        # Compute chain hash
        with conn.cursor() as cur:
            cur.execute("""
                SELECT mutation_hash FROM gdu_admission_log
                WHERE tenant_id = %s ORDER BY rid DESC LIMIT 1
            """, (tenant_id,))
            prev_row = cur.fetchone()
            previous_hash = prev_row[0] if prev_row else None

        payload = json.dumps({
            "node_id": node_id, "entity_id": entity_id, "edge_id": edge_id,
            "operation": operation, "category": category, "subtype": subtype,
            "base60": base60, "reason": reason,
            "ts": datetime.now(timezone.utc).isoformat(),
        }, sort_keys=True)

        hash_input = (previous_hash or "genesis") + payload
        mutation_hash = hashlib.sha256(hash_input.encode()).hexdigest()

        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO gdu_admission_log
                    (tenant_id, node_id, entity_id, edge_id,
                     operation, category, subtype, base60_address,
                     input_type, mapped_type, reason,
                     lineage_stamp, mutation_hash, previous_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                tenant_id, node_id, entity_id, edge_id,
                operation, category, subtype, base60,
                input_type, mapped_type, reason[:500],
                json.dumps(stamp), mutation_hash, previous_hash,
            ))
