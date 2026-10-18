"""
Canonical entity promotion — converts validated clusters into rows in
the `entities` table and backlinks every document_mention to its
canonical entity.

Promotion rules (in priority order):
  1. Wikipedia-matched cluster (entity_external_lookups.source='wikipedia',
     matched=true, has external_id Q-number):
       → look up existing entity by Wikidata QID in identifiers JSON
       → if found, link cluster mentions to it
       → if not, create new entity with the QID + canonical name from
         the Wikipedia label + canonical_type from the cluster's
         suggested_type
       rule: 'wikipedia_qid_match'

  2. Internal-node-matched cluster (entity_external_lookups.source=
     'internal_node', matched=true):
       → link mentions to the existing entity (no new entity created)
       rule: 'internal_name_match'

  3. Multi-mention cluster with no external match:
       → create a candidate entity (low confidence, source_kind='document')
       → review_required corpora get suggested_tier; others get soft_tier
       rule: 'multi_mention_new_entity'

  4. Singleton cluster (mention_count == 1) with no external match:
       → DO NOT auto-promote. Wait for corroboration from another
         mention before creating an entity. Recorded as a collapse
         decision with action='collapse_candidate'.

Every promotion writes a document_cluster_promotions row with the rule
that fired, the evidence, and a reversible flag.

Public surface:
    promote_cluster(cluster_id) -> dict  — one cluster
    promote_corpus(corpus_id)  -> dict   — every eligible cluster
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Optional

from storage.postgres.db import get_connection
from core.extraction.name_shape import classify_name

log = logging.getLogger("enki.documents.entity_promotion")


def _identifier_hash(tenant_id: str, identifiers: dict) -> Optional[str]:
    """Compute a hash matching the entities.identifier_hash convention so
    we can find an existing entity by its Wikidata QID without duplicating."""
    if not identifiers:
        return None
    # Stable key for the QID — same convention used by the signal pipeline.
    canon = json.dumps(identifiers, sort_keys=True)
    return hashlib.sha256(f"{tenant_id}::{canon}".encode()).hexdigest()


def _find_entity_by_qid(tenant_id: str, qid: str) -> Optional[dict]:
    """Look up an existing entity whose identifiers JSON contains this QID.
    Returns (entity_id, canonical_name, canonical_type) or None."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT entity_id, canonical_name, canonical_type
                  FROM entities
                 WHERE tenant_id = %s
                   AND identifiers ->> 'wikidata_qid' = %s
                 LIMIT 1
                """,
                (tenant_id, qid),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {"entity_id": row[0], "canonical_name": row[1],
                    "canonical_type": row[2]}


def _find_entity_by_normalized_name(tenant_id: str, normalized: str) -> Optional[int]:
    """Find existing entity by normalized_name. The entities table has a
    unique constraint on (tenant_id, normalized_name) so this is exact."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT entity_id FROM entities
                 WHERE tenant_id = %s AND normalized_name = %s
                 LIMIT 1
                """,
                (tenant_id, normalized),
            )
            row = cur.fetchone()
            return row[0] if row else None


def _is_qid_concept(qid: Optional[str]) -> bool:
    """Ask wikidata_types whether this QID is a class/metaclass (concept)
    vs an instance. Cached in wikidata_type_cache."""
    if not qid:
        return False
    try:
        from core.documents.wikidata_types import get_qid_types
        return bool(get_qid_types(qid).get("is_concept"))
    except Exception:
        return False


# Map our LLM extractor's `suggested_type` (which becomes canonical_type) to
# the GDU (category, subtype) pair used by core.gdu.address.assign_address.
# Categories + subtypes are defined in nodevocab; we have to use what's
# registered there, not invent new ones.
_TYPE_TO_CATEGORY_SUBTYPE: dict[str, tuple[str, str]] = {
    "person":       ("entity", "person"),
    "organization": ("entity", "organization"),
    "aircraft":     ("entity", "aircraft"),
    "vehicle":      ("entity", "vehicle"),
    "vessel":       ("entity", "vessel"),
    "facility":     ("entity", "facility"),
    "device":       ("entity", "device"),
    "product":      ("entity", "product"),
    "unit":         ("entity", "unit"),
    # Places live under their own category, not under entity
    "place":        ("place",  "admin_region"),
    "city":         ("place",  "city"),
    "country":      ("place",  "country"),
    "region":       ("place",  "admin_region"),
}


def _resolve_category_subtype(canonical_type: Optional[str]) -> Optional[tuple[str, str]]:
    if not canonical_type:
        return None
    return _TYPE_TO_CATEGORY_SUBTYPE.get(canonical_type.lower())


def _find_or_create_concept_for_type(
    tenant_id: str, canonical_type: str,
) -> Optional[int]:
    """For an instance with canonical_type like 'person' or 'organization',
    find (or create) the matching concept-level entity. Returns the
    concept's entity_id.

    Smart-merge: if an existing INSTANCE entity already has the same
    normalized name as the would-be concept (e.g. entity 46678 named
    "aircraft" promoted as instance with Wikidata Q11436), UPGRADE it
    to entity_kind='concept' rather than creating a duplicate.
    """
    if not canonical_type or canonical_type == "unknown":
        return None
    # Subtype lockdown — refuse to mint a concept entity for a type that
    # entityvocab routes elsewhere or rejects. Concept rows have
    # canonical_type=canonical_type, so a bad type would crash migration
    # 116's CHECK constraint on INSERT. Returns None for route/drop/unknown;
    # callers already handle a None concept link gracefully.
    try:
        from core.ldu.subtype_validator import validate_for_ldu
        _v = validate_for_ldu(canonical_type)
        if _v.action == "accept":
            if _v.canonical_subtype and _v.canonical_subtype != canonical_type:
                canonical_type = _v.canonical_subtype
        else:
            log.info("skipping concept for non-accept type %r (action=%s)",
                     canonical_type, _v.action)
            return None
    except Exception:
        log.debug("subtype_validator failed in _find_or_create_concept_for_type",
                  exc_info=True)
    normalized = canonical_type.upper()
    with get_connection() as conn:
        with conn.cursor() as cur:
            # First look for an existing concept
            cur.execute(
                """
                SELECT entity_id, entity_kind FROM entities
                 WHERE tenant_id = %s AND normalized_name = %s
                 LIMIT 1
                """,
                (tenant_id, normalized),
            )
            row = cur.fetchone()
            if row:
                eid, kind = row
                if kind != "concept":
                    # Upgrade in place
                    cur.execute(
                        "UPDATE entities SET entity_kind='concept' WHERE entity_id=%s",
                        (eid,),
                    )
                    conn.commit()
                return eid

            # No existing entity with this name → create a fresh concept.
            cur.execute(
                """
                INSERT INTO entities
                    (tenant_id, canonical_type, canonical_name, normalized_name,
                     aliases, identifiers, identifier_hash,
                     completeness_score, completeness_tier, signal_count,
                     admitted_under_active_set, admitted_under_pack_versions,
                     gdu_canonical, source_kind, entity_kind)
                VALUES (%s, %s, %s, %s, ARRAY[]::text[], '{}'::jsonb, NULL,
                        0.50, 'partial', 0, 'default', '{}'::jsonb,
                        TRUE, 'document', 'concept')
                RETURNING entity_id
                """,
                (
                    tenant_id, canonical_type,
                    canonical_type.capitalize(),
                    normalized,
                ),
            )
            cid = cur.fetchone()[0]
        conn.commit()
    return cid


def _mint_base60_address(
    canonical_type: str,
) -> Optional[str]:
    """Mint a base-60 address from core.gdu.address. Returns None if the
    type doesn't map to nodevocab."""
    pair = _resolve_category_subtype(canonical_type)
    if not pair:
        return None
    category, subtype = pair
    try:
        from core.gdu.address import assign_address
    except Exception:
        return None
    try:
        with get_connection() as conn:
            addr = assign_address(conn, category, subtype)
            conn.commit()
            return addr
    except Exception:
        log.exception("base60 mint failed for type=%s", canonical_type)
        return None


def _create_entity(
    tenant_id: str, canonical_name: str, canonical_type: str,
    aliases: list[str], qid: Optional[str], active_set: str = "default",
) -> Optional[int]:
    """Insert a new canonical entity row. Returns entity_id.

    Two-layer logic:
      - If the QID is a Wikidata concept/class (e.g. Q11436 'aircraft',
        Q5 'human'), the new row is entity_kind='concept'.
      - Otherwise it's entity_kind='instance'. The instance is linked
        UP to its concept via concept_entity_id (find-or-create on the
        canonical_type label, e.g., 'person' → concept entity 'Person').
      - Every new entity gets a base-60 address via core.gdu.address,
        joining the same address space as knowledge_nodes.

    If an entity with the same normalized_name already exists (from the
    signal pipeline or a prior doc promotion), reuse it instead of
    raising — duplicates would violate entities_pending_unique.

    Returns None when entityvocab routes/drops/rejects the canonical_type
    (e.g. 'place', 'concept') — caller treats as a failed promotion and
    records it as routed_to_ldup rather than crashing migration 116's CHECK.
    """
    # Topic guard: a topic-shaped label ("Life of Alex Acme",
    # "Relationship of A and B") is NOT a physical entity — it belongs to the
    # topic library, not the person/org/place space. Refuse promotion (caller
    # records routed_to_ldup); the source stays staged so the emergent topic
    # flow can admit it as a topic. This is the live counterpart to the
    # migration that reclassified the historical article-title "persons".
    _shape = classify_name(canonical_name)
    if _shape.is_topic:
        try:
            from core.gdu.entity_governance import stage_provisional
            stage_provisional(
                tenant_id=tenant_id, raw_name=canonical_name,
                suspected_type=canonical_type or "unknown",
                source_pipeline="entity_promotion",
                rejection_reason=f"topic_shaped:{_shape.topic_kind or 'theme'}",
            )
        except Exception:
            log.debug("stage_provisional failed for topic-shaped name",
                      exc_info=True)
        log.info("entity_promotion: refused topic-shaped %r (kind=%s)",
                 canonical_name, _shape.topic_kind)
        return None

    # Subtype lockdown.
    try:
        from core.ldu.subtype_validator import validate_for_ldu
        _v = validate_for_ldu(canonical_type)
        if _v.action == "accept":
            if _v.canonical_subtype and _v.canonical_subtype != canonical_type:
                canonical_type = _v.canonical_subtype
        else:
            rej = (f"route:{_v.route_to[0]}.{_v.route_to[1]}"
                   if _v.action == "route" and _v.route_to
                   else f"ldu_{_v.action}:{canonical_type or '<empty>'}")
            try:
                from core.gdu.entity_governance import stage_provisional
                stage_provisional(
                    tenant_id=tenant_id, raw_name=canonical_name,
                    suspected_type=canonical_type or "unknown",
                    source_pipeline="entity_promotion",
                    rejection_reason=rej,
                )
            except Exception:
                log.debug("stage_provisional failed in entity_promotion._create_entity",
                          exc_info=True)
            log.info("entity_promotion: routed %r to LDU-P (%s)", canonical_name, rej)
            return None
    except Exception:
        log.debug("subtype_validator failed in entity_promotion._create_entity",
                  exc_info=True)

    identifiers = {"wikidata_qid": qid} if qid else {}
    normalized = (canonical_name or "").strip().upper()

    is_concept = _is_qid_concept(qid)
    entity_kind = "concept" if is_concept else "instance"

    # Pre-check: reuse if already there.
    existing = _find_entity_by_normalized_name(tenant_id, normalized)
    if existing:
        with get_connection() as conn:
            with conn.cursor() as cur:
                updates: list[str] = ["last_seen = NOW()"]
                params: list = []
                if qid:
                    updates.append(
                        "identifiers = identifiers || "
                        "jsonb_build_object('wikidata_qid', %s::text)"
                    )
                    params.append(qid)
                # If the existing row has no base60_address, mint one now.
                cur.execute(
                    "SELECT base60_address, entity_kind, concept_entity_id "
                    "FROM entities WHERE entity_id = %s",
                    (existing,),
                )
                cur_addr, cur_kind, cur_concept = cur.fetchone()
                if not cur_addr:
                    addr = _mint_base60_address(canonical_type)
                    if addr:
                        updates.append("base60_address = %s")
                        params.append(addr)
                # Upgrade entity_kind if our determination is stronger
                # (existing=instance + new=concept is a real upgrade).
                if cur_kind != "concept" and entity_kind == "concept":
                    updates.append("entity_kind = 'concept'")
                # If existing is an instance and has no concept link, add it
                if entity_kind == "instance" and not cur_concept:
                    concept_id = _find_or_create_concept_for_type(
                        tenant_id, canonical_type,
                    )
                    if concept_id:
                        updates.append("concept_entity_id = %s")
                        params.append(concept_id)
                params.append(existing)
                cur.execute(
                    f"UPDATE entities SET {', '.join(updates)} WHERE entity_id = %s",
                    params,
                )
            conn.commit()
        return existing

    # Brand new entity — compute concept_entity_id (if this is an
    # instance) and base60 address.
    concept_id: Optional[int] = None
    if entity_kind == "instance":
        concept_id = _find_or_create_concept_for_type(tenant_id, canonical_type)
    base60 = _mint_base60_address(canonical_type)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO entities
                    (tenant_id, canonical_type, canonical_name, normalized_name,
                     aliases, identifiers, identifier_hash,
                     completeness_score, completeness_tier,
                     signal_count, admitted_under_active_set,
                     admitted_under_pack_versions, gdu_canonical,
                     source_kind, entity_kind, concept_entity_id, base60_address)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s,
                        %s, %s, 0, %s, '{}'::jsonb, TRUE,
                        'document', %s, %s, %s)
                RETURNING entity_id
                """,
                (
                    tenant_id, canonical_type, canonical_name, normalized,
                    aliases or [], json.dumps(identifiers),
                    _identifier_hash(tenant_id, identifiers),
                    0.50, "partial", active_set,
                    entity_kind, concept_id, base60,
                ),
            )
            entity_id = cur.fetchone()[0]
        conn.commit()
    return entity_id


def _link_mentions_to_entity(cluster_id: int, entity_id: int) -> int:
    """Backlink every mention in this cluster to the canonical entity.
    Returns number of mentions linked."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE document_mentions
                   SET entity_id = %s,
                       candidate_entity_id = %s,
                       resolution_status = 'bound'
                 WHERE cluster_id = %s
                """,
                (entity_id, entity_id, cluster_id),
            )
            rows = cur.rowcount
            # Update signal_count on the entity to reflect the new evidence
            cur.execute(
                """
                UPDATE entities
                   SET signal_count = signal_count + %s,
                       last_seen = NOW()
                 WHERE entity_id = %s
                """,
                (rows, entity_id),
            )
        conn.commit()
    return rows


def _record_promotion(
    cluster_id: int, entity_id: int, rule: str,
    score: float, tier: str, evidence: dict,
    qid: Optional[str] = None, internal_node_id: Optional[int] = None,
    notes: Optional[str] = None,
) -> int:
    """Insert one row to document_cluster_promotions. Idempotent on cluster_id
    (UPSERT-via-DELETE-then-INSERT pattern)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM document_cluster_promotions WHERE cluster_id = %s",
                (cluster_id,),
            )
            cur.execute(
                """
                INSERT INTO document_cluster_promotions
                    (cluster_id, entity_id, rule_fired, promotion_score,
                     confidence_tier, evidence, wikidata_qid,
                     internal_node_id, notes)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                RETURNING promotion_id
                """,
                (
                    cluster_id, entity_id, rule, score, tier,
                    json.dumps(evidence), qid, internal_node_id, notes,
                ),
            )
            pid = cur.fetchone()[0]
        conn.commit()
    return pid


def _fetch_cluster_with_lookups(cluster_id: int) -> Optional[dict]:
    """Pull a cluster + its external_lookups in one query."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.cluster_id, c.corpus_id, c.surface_form_norm,
                       c.representative_form, c.mention_count, c.document_count,
                       c.context_keywords, c.suggested_type
                  FROM document_mention_clusters c
                 WHERE c.cluster_id = %s
                """,
                (cluster_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            cluster = {
                "cluster_id": row[0], "corpus_id": row[1],
                "surface_form_norm": row[2],
                "representative_form": row[3],
                "mention_count": row[4], "document_count": row[5],
                "context_keywords": row[6] or [],
                "suggested_type": row[7],
            }
            cur.execute(
                """
                SELECT source, external_id, label, matched, confidence
                  FROM entity_external_lookups
                 WHERE cluster_id = %s AND matched = TRUE
                """,
                (cluster_id,),
            )
            cluster["lookups"] = [
                {"source": r[0], "external_id": r[1], "label": r[2],
                 "matched": r[3], "confidence": float(r[4] or 0)}
                for r in cur.fetchall()
            ]
    return cluster


# ─── Public API ─────────────────────────────────────────────────────────

def promote_cluster(cluster_id: int, tenant_id: str = "default",
                     active_set: str = "6.2.0") -> dict:
    """Promote one cluster to a canonical entity. Returns the promotion result."""
    cluster = _fetch_cluster_with_lookups(cluster_id)
    if not cluster:
        return {"ok": False, "reason": "cluster_not_found", "cluster_id": cluster_id}

    # Priority 1: Wikipedia match
    wp_lookups = [l for l in cluster["lookups"]
                  if l["source"] == "wikipedia" and l["matched"]
                  and l.get("external_id")]
    if wp_lookups:
        wp = wp_lookups[0]
        qid = wp["external_id"]
        # Check if entity already exists by QID
        existing = _find_entity_by_qid(tenant_id, qid)
        if existing:
            entity_id = existing["entity_id"]
            rule = "wikipedia_qid_match_existing"
            notes = (f"reused entity #{entity_id} ({existing['canonical_name']}) "
                     f"matching QID {qid}")
        else:
            entity_id = _create_entity(
                tenant_id=tenant_id,
                canonical_name=wp["label"] or cluster["representative_form"],
                canonical_type=cluster["suggested_type"] or "unknown",
                aliases=[cluster["representative_form"]],
                qid=qid, active_set=active_set,
            )
            if entity_id is None:
                return {
                    "ok": False, "cluster_id": cluster_id, "entity_id": None,
                    "rule": "routed_to_ldup",
                    "tier": None, "qid": qid,
                    "reason": (f"suggested_type {cluster['suggested_type']!r} "
                               f"is not an admittable entity subtype"),
                }
            rule = "wikipedia_qid_match_new"
            notes = (f"created entity #{entity_id} from Wikipedia hit "
                     f"'{wp['label']}' QID {qid}")
        mentions_linked = _link_mentions_to_entity(cluster_id, entity_id)
        pid = _record_promotion(
            cluster_id=cluster_id, entity_id=entity_id, rule=rule,
            score=float(wp["confidence"] or 0.85), tier="hard",
            evidence={"axis": "wikidata_qid", "qid": qid, "label": wp["label"]},
            qid=qid, notes=notes,
        )
        return {
            "ok": True, "cluster_id": cluster_id, "entity_id": entity_id,
            "rule": rule, "tier": "hard", "qid": qid,
            "mentions_linked": mentions_linked, "promotion_id": pid,
        }

    # Priority 2: Internal-node match
    int_lookups = [l for l in cluster["lookups"]
                   if l["source"] == "internal_node" and l["matched"]
                   and l.get("external_id")]
    if int_lookups:
        int_match = int_lookups[0]
        entity_id = int(int_match["external_id"])
        mentions_linked = _link_mentions_to_entity(cluster_id, entity_id)
        pid = _record_promotion(
            cluster_id=cluster_id, entity_id=entity_id,
            rule="internal_name_match",
            score=float(int_match["confidence"] or 0.85), tier="soft",
            evidence={"axis": "internal_node",
                      "matched_entity_id": entity_id,
                      "canonical_name": int_match["label"]},
            internal_node_id=entity_id,
            notes=f"linked to existing entity #{entity_id} ({int_match['label']})",
        )
        return {
            "ok": True, "cluster_id": cluster_id, "entity_id": entity_id,
            "rule": "internal_name_match", "tier": "soft",
            "mentions_linked": mentions_linked, "promotion_id": pid,
        }

    # Priority 3: Multi-mention with no external match — new candidate entity
    if cluster["mention_count"] >= 2:
        entity_id = _create_entity(
            tenant_id=tenant_id,
            canonical_name=cluster["representative_form"]
                          or cluster["surface_form_norm"],
            canonical_type=cluster["suggested_type"] or "unknown",
            aliases=[cluster["representative_form"]],
            qid=None, active_set=active_set,
        )
        if entity_id is None:
            return {
                "ok": False, "cluster_id": cluster_id, "entity_id": None,
                "rule": "routed_to_ldup",
                "tier": None,
                "reason": (f"suggested_type {cluster['suggested_type']!r} "
                           f"is not an admittable entity subtype"),
            }
        mentions_linked = _link_mentions_to_entity(cluster_id, entity_id)
        pid = _record_promotion(
            cluster_id=cluster_id, entity_id=entity_id,
            rule="multi_mention_new_entity",
            score=0.55, tier="suggested",
            evidence={"mention_count": cluster["mention_count"],
                      "context_keywords": cluster["context_keywords"][:5]},
            notes=f"created candidate entity #{entity_id} from "
                  f"{cluster['mention_count']}-mention cluster, "
                  f"no external match found",
        )
        return {
            "ok": True, "cluster_id": cluster_id, "entity_id": entity_id,
            "rule": "multi_mention_new_entity", "tier": "suggested",
            "mentions_linked": mentions_linked, "promotion_id": pid,
        }

    # Priority 4: Singleton with no match — DON'T auto-promote
    return {
        "ok": True, "cluster_id": cluster_id, "entity_id": None,
        "rule": "singleton_unpromoted", "tier": None,
        "mentions_linked": 0,
        "reason": "singleton mention with no internal or external match — "
                  "waiting for corroboration",
    }


def promote_corpus(corpus_id: str, tenant_id: str = "default") -> dict:
    """Promote every eligible cluster in a corpus."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT cluster_id FROM document_mention_clusters WHERE corpus_id = %s",
                (corpus_id,),
            )
            cluster_ids = [r[0] for r in cur.fetchall()]

    counts = {
        "clusters_total": len(cluster_ids),
        "wikipedia_qid_match_new": 0,
        "wikipedia_qid_match_existing": 0,
        "internal_name_match": 0,
        "multi_mention_new_entity": 0,
        "singleton_unpromoted": 0,
        "errors": 0,
        "entities_created": 0,
        "mentions_linked": 0,
    }
    sample_promoted: list[dict] = []

    for cid in cluster_ids:
        try:
            r = promote_cluster(cid, tenant_id=tenant_id)
        except Exception:
            log.exception("promote_cluster failed on %s", cid)
            counts["errors"] += 1
            continue
        if not r.get("ok"):
            counts["errors"] += 1
            continue
        rule = r.get("rule", "unknown")
        counts[rule] = counts.get(rule, 0) + 1
        counts["mentions_linked"] += r.get("mentions_linked", 0)
        if rule in ("wikipedia_qid_match_new", "multi_mention_new_entity"):
            counts["entities_created"] += 1
        if r.get("entity_id") and len(sample_promoted) < 8:
            sample_promoted.append({
                "cluster_id": r["cluster_id"],
                "entity_id": r["entity_id"],
                "rule": r["rule"],
                "tier": r.get("tier"),
                "qid": r.get("qid"),
            })

    return {
        "ok": True,
        "corpus_id": corpus_id,
        "counts": counts,
        "sample_promotions": sample_promoted,
    }
