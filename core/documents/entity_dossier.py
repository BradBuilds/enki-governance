"""
Entity dossier — assemble the full "node file" for one canonical entity.

For any entity_id, this returns a single JSON bundle that includes:
  • the canonical entity row (name, type, aliases, identifiers, etc.)
  • every document_mention linked to it (with full provenance:
    document, page, paragraph, char span, surface form, surrounding text)
  • every cluster the mentions belong to (with context keywords)
  • every external lookup (Wikipedia QID + label + summary + url)
  • every collapse_decision (rule fired, evidence, decided_by, reversible)
  • every promotion record (rule, score, tier, evidence)
  • aggregate counts: source document list, mention type distribution

This is the structure that will become the exportable node-file format
(Phase C). For now it's a query-time assembly — no caching, always fresh.

Public surface:
    get_entity_dossier(entity_id, *, include_surrounding_text=True) -> dict
    get_dossier_by_qid(wikidata_qid, tenant_id='default') -> dict | None
"""

from __future__ import annotations

import logging
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.documents.entity_dossier")


def _entity_row(entity_id: int) -> Optional[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT entity_id, tenant_id, canonical_type, canonical_name,
                       normalized_name, aliases, identifiers,
                       completeness_score, completeness_tier,
                       signal_count, source_kind,
                       first_seen, last_seen, last_researched_at,
                       reality_status,
                       entity_kind, concept_entity_id, base60_address
                  FROM entities WHERE entity_id = %s
                """,
                (entity_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "entity_id": row[0],
                "tenant_id": row[1],
                "canonical_type": row[2],
                "canonical_name": row[3],
                "normalized_name": row[4],
                "aliases": row[5] or [],
                "identifiers": row[6] or {},
                "completeness_score": float(row[7] or 0),
                "completeness_tier": row[8],
                "signal_count": row[9],
                "source_kind": row[10],
                "first_seen": row[11].isoformat() if row[11] else None,
                "last_seen": row[12].isoformat() if row[12] else None,
                "last_researched_at": row[13].isoformat() if row[13] else None,
                "reality_status": row[14],
                "entity_kind": row[15],
                "concept_entity_id": row[16],
                "base60_address": row[17],
            }


def _concept_parent(concept_entity_id: Optional[int]) -> Optional[dict]:
    """Resolve the concept parent — the abstract class node that this
    instance is an instance OF. Returns minimal info: name, address, QID.

    None when the entity is itself a concept or has no concept link yet.
    """
    if not concept_entity_id:
        return None
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT entity_id, canonical_name, canonical_type,
                       base60_address, identifiers ->> 'wikidata_qid'
                  FROM entities WHERE entity_id = %s
                """,
                (concept_entity_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "entity_id":      row[0],
                "canonical_name": row[1],
                "canonical_type": row[2],
                "base60_address": row[3],
                "wikidata_qid":   row[4],
            }


def _concept_instance_count(concept_entity_id: int) -> int:
    """How many instances point UP to this concept. Useful for the
    dossier card on a concept node (e.g. "Person — 81 instances")."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM entities WHERE concept_entity_id = %s",
                (concept_entity_id,),
            )
            return int(cur.fetchone()[0] or 0)


def _timeline_for_entity(entity_id: int, limit: int = 100) -> list[dict]:
    """Every event where this entity participated as actor or target,
    ordered by event_time. Empty list if no events yet."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.event_id, e.event_type, e.title, e.event_time,
                       e.temporal_precision, e.confidence,
                       e.location_entity_id, l.canonical_name AS location_name,
                       e.latitude, e.longitude,
                       e.actor_entity_ids, e.target_entity_ids,
                       e.base60_address, e.originating_statement_ids
                  FROM events e
                  LEFT JOIN entities l ON l.entity_id = e.location_entity_id
                 WHERE %s = ANY(COALESCE(e.actor_entity_ids,  ARRAY[]::bigint[]))
                    OR %s = ANY(COALESCE(e.target_entity_ids, ARRAY[]::bigint[]))
                 ORDER BY e.event_time NULLS LAST, e.event_id
                 LIMIT %s
                """,
                (entity_id, entity_id, limit),
            )
            rows = cur.fetchall()
    out: list[dict] = []
    for r in rows:
        out.append({
            "event_id":             r[0],
            "event_type":           r[1],
            "title":                r[2],
            "event_time":           r[3].isoformat() if r[3] else None,
            "temporal_precision":   r[4],
            "confidence":           float(r[5]) if r[5] is not None else None,
            "location_entity_id":   r[6],
            "location_name":        r[7],
            "latitude":             r[8],
            "longitude":            r[9],
            "actor_entity_ids":     list(r[10] or []),
            "target_entity_ids":    list(r[11] or []),
            "event_address":        r[12],
            "originating_statement_ids": list(r[13] or []),
        })
    return out


def _assessments_for_entity(entity_id: int, limit: int = 100) -> dict:
    """Assessments (facts/claims/observations) where this entity is
    subject OR object. Returns dict with 'by_this_entity' and
    'about_this_entity'."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.assessment_id, a.base60_address, a.assessment_tier,
                       a.subj_entity_id, ses.canonical_name AS subj_name,
                       a.predicate_edge_type,
                       a.obj_entity_id, oes.canonical_name AS obj_name,
                       a.obj_value, a.polarity,
                       a.corroborated_by_count, a.contradicted_by_count,
                       a.earliest_event_time, a.latest_event_time,
                       a.confidence,
                       a.source_statement_ids
                  FROM assessments a
                  LEFT JOIN entities ses ON ses.entity_id = a.subj_entity_id
                  LEFT JOIN entities oes ON oes.entity_id = a.obj_entity_id
                 WHERE a.subj_entity_id = %s OR a.obj_entity_id = %s
                 ORDER BY a.assessment_tier DESC NULLS LAST,
                          a.corroborated_by_count DESC NULLS LAST,
                          a.assessment_id
                 LIMIT %s
                """,
                (entity_id, entity_id, limit),
            )
            rows = cur.fetchall()
    by_this: list[dict] = []
    about_this: list[dict] = []
    for r in rows:
        entry = {
            "assessment_id":      r[0],
            "base60_address":     r[1],
            "tier":               r[2],
            "subj_entity_id":     r[3],
            "subj_name":          r[4],
            "predicate_edge_type": r[5],
            "obj_entity_id":      r[6],
            "obj_name":           r[7],
            "obj_value":          r[8],
            "polarity":           r[9],
            "corroborated_by_count": r[10],
            "contradicted_by_count": r[11],
            "earliest_event_time": r[12].isoformat() if r[12] else None,
            "latest_event_time":   r[13].isoformat() if r[13] else None,
            "confidence":         float(r[14]) if r[14] is not None else None,
            "source_statement_ids": list(r[15] or []),
        }
        if r[3] == entity_id:
            by_this.append(entry)
        else:
            about_this.append(entry)
    return {"by_this_entity": by_this, "about_this_entity": about_this}


def _mentions_for_entity(entity_id: int,
                          include_surrounding_text: bool = True) -> list[dict]:
    """Every mention linked to this entity, with full provenance."""
    sql = """
        SELECT m.mention_id,
               m.document_id, d.title AS doc_title, d.on_disk_path,
               d.dataset_id, d.trust_class,
               m.page_id, p.page_number,
               m.paragraph_id, dp.order_index AS paragraph_idx,
               m.char_start, m.char_end,
               m.surface_form, m.surface_form_norm,
               m.suggested_type, m.confidence,
               m.extraction_method, m.cluster_id,
               m.resolution_status, m.created_at
    """
    if include_surrounding_text:
        sql += """,
               SUBSTRING(dp.raw_text FROM GREATEST(1, m.char_start - 60)
                                       FOR LEAST(LENGTH(dp.raw_text),
                                                 m.char_end - m.char_start + 120))
                 AS surrounding_text,
               LEFT(dp.raw_text, 400) AS paragraph_excerpt
        """
    sql += """
          FROM document_mentions m
          JOIN documents d ON d.document_id = m.document_id
          LEFT JOIN document_pages p ON p.page_id = m.page_id
          LEFT JOIN document_paragraphs dp ON dp.paragraph_id = m.paragraph_id
         WHERE m.entity_id = %s
         ORDER BY d.document_id, p.page_number NULLS LAST,
                  dp.order_index NULLS LAST, m.char_start
    """
    out: list[dict] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (entity_id,))
            for r in cur.fetchall():
                rec = {
                    "mention_id": r[0],
                    "source": {
                        "document_id": r[1],
                        "document_title": r[2],
                        "on_disk_path": r[3],
                        "dataset_id": r[4],
                        "trust_class": r[5],
                        "page_id": r[6],
                        "page_number": r[7],
                        "paragraph_id": r[8],
                        "paragraph_idx": r[9],
                        "char_start": r[10],
                        "char_end": r[11],
                    },
                    "extracted": {
                        "surface_form": r[12],
                        "surface_form_norm": r[13],
                        "suggested_type": r[14],
                        "confidence": float(r[15] or 0),
                        "extraction_method": r[16],
                        "cluster_id": r[17],
                        "resolution_status": r[18],
                        "extracted_at": r[19].isoformat() if r[19] else None,
                    },
                }
                if include_surrounding_text:
                    rec["surrounding_text"] = r[20]
                    rec["paragraph_excerpt"] = r[21]
                out.append(rec)
    return out


def _clusters_for_entity(entity_id: int) -> list[dict]:
    """Every cluster whose mentions belong to this entity."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT c.cluster_id, c.corpus_id,
                       c.surface_form_norm, c.representative_form,
                       c.mention_count, c.document_count,
                       c.context_keywords, c.suggested_type, c.created_at
                  FROM document_mention_clusters c
                  JOIN document_mentions m ON m.cluster_id = c.cluster_id
                 WHERE m.entity_id = %s
                """,
                (entity_id,),
            )
            return [
                {
                    "cluster_id": r[0],
                    "corpus_id": r[1],
                    "surface_form_norm": r[2],
                    "representative_form": r[3],
                    "mention_count": r[4],
                    "document_count": r[5],
                    "context_keywords": r[6] or [],
                    "suggested_type": r[7],
                    "created_at": r[8].isoformat() if r[8] else None,
                }
                for r in cur.fetchall()
            ]


def _external_lookups_for_entity(entity_id: int) -> list[dict]:
    """Every external validation (Wikipedia, internal_node, etc.) for the
    clusters that promoted into this entity."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT e.lookup_id, e.cluster_id, e.corpus_id,
                       e.source, e.external_id, e.label,
                       LEFT(e.summary, 800) AS summary, e.url,
                       e.matched, e.confidence, e.looked_up_at
                  FROM entity_external_lookups e
                  JOIN document_mentions m ON m.cluster_id = e.cluster_id
                 WHERE m.entity_id = %s AND e.matched
                """,
                (entity_id,),
            )
            return [
                {
                    "lookup_id": r[0], "cluster_id": r[1], "corpus_id": r[2],
                    "source": r[3], "external_id": r[4],
                    "label": r[5], "summary": r[6], "url": r[7],
                    "matched": r[8], "confidence": float(r[9] or 0),
                    "looked_up_at": r[10].isoformat() if r[10] else None,
                }
                for r in cur.fetchall()
            ]


def _promotions_for_entity(entity_id: int) -> list[dict]:
    """The cluster→entity promotion records that built this entity."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT promotion_id, cluster_id, rule_fired,
                       promotion_score, confidence_tier, evidence,
                       wikidata_qid, internal_node_id,
                       promoted_at, promoted_by,
                       reverted_at, reverted_by, notes
                  FROM document_cluster_promotions
                 WHERE entity_id = %s
                 ORDER BY promoted_at
                """,
                (entity_id,),
            )
            return [
                {
                    "promotion_id": r[0], "cluster_id": r[1],
                    "rule_fired": r[2],
                    "promotion_score": float(r[3] or 0),
                    "confidence_tier": r[4],
                    "evidence": r[5] or {},
                    "wikidata_qid": r[6], "internal_node_id": r[7],
                    "promoted_at": r[8].isoformat() if r[8] else None,
                    "promoted_by": r[9],
                    "reverted_at": r[10].isoformat() if r[10] else None,
                    "reverted_by": r[11], "notes": r[12],
                }
                for r in cur.fetchall()
            ]


def _collapse_decisions_for_entity(entity_id: int) -> list[dict]:
    """Every collapse_decisions row that mentions this entity OR the
    clusters that promoted to it."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT cd.decision_id, cd.corpus_id,
                       cd.surface_form_norm, cd.action, cd.rule_fired,
                       cd.score, cd.confidence_tier,
                       cd.evidence_for, cd.evidence_against,
                       cd.decided_by, cd.decided_at,
                       cd.reverted_at, cd.notes
                  FROM collapse_decisions cd
                  JOIN document_mentions m
                    ON m.surface_form_norm = cd.surface_form_norm
                   AND (cd.corpus_id IS NULL OR cd.corpus_id IN (
                          SELECT dataset_id FROM documents
                           WHERE document_id = m.document_id))
                 WHERE m.entity_id = %s
                 ORDER BY cd.decided_at
                """,
                (entity_id,),
            )
            return [
                {
                    "decision_id": r[0], "corpus_id": r[1],
                    "surface_form_norm": r[2], "action": r[3],
                    "rule_fired": r[4], "score": float(r[5] or 0),
                    "confidence_tier": r[6],
                    "evidence_for": r[7] or [],
                    "evidence_against": r[8] or [],
                    "decided_by": r[9],
                    "decided_at": r[10].isoformat() if r[10] else None,
                    "reverted_at": r[11].isoformat() if r[11] else None,
                    "notes": r[12],
                }
                for r in cur.fetchall()
            ]


def _images_for_entity(entity_id: int) -> list[dict]:
    """Every image attached to this entity, two ways:
      1. Images on pages where the entity has paragraph mentions
      2. Images that directly produced an image_ocr / image_caption mention
         linked to this entity
    Returns one row per image with its analyses rolled in."""
    sql = """
        WITH entity_images AS (
          -- Images on pages where the entity is mentioned
          SELECT DISTINCT i.image_id, 'page_evidence' AS link_kind
            FROM document_images i
            JOIN document_mentions m
              ON m.page_id = i.page_id
             AND m.entity_id = %s
          UNION
          -- Images that directly produced a mention of this entity
          SELECT DISTINCT i.image_id, 'direct_mention' AS link_kind
            FROM document_images i
            JOIN document_mentions m ON m.image_id = i.image_id
           WHERE m.entity_id = %s
        )
        SELECT i.image_id, ei.link_kind,
               i.document_id, d.title, p.page_number,
               i.image_index, i.on_disk_path,
               i.sha256, i.phash, i.width, i.height,
               i.bytes, i.mime, i.is_redacted_block,
               i.extracted_at,
               -- Inline the analyses as JSON aggregate
               (SELECT jsonb_agg(jsonb_build_object(
                   'engine', a.engine,
                   'classification', a.classification,
                   'caption', a.caption,
                   'is_intelligence', a.is_intelligence,
                   'confidence', a.confidence,
                   'ocr_excerpt', LEFT(a.ocr_text, 500),
                   'objects', a.objects,
                   'tags', a.tags,
                   'faces_count', a.faces_count,
                   'error', a.error
                 ))
                  FROM document_image_analyses a
                 WHERE a.image_id = i.image_id) AS analyses
          FROM entity_images ei
          JOIN document_images i ON i.image_id = ei.image_id
          JOIN documents d ON d.document_id = i.document_id
          LEFT JOIN document_pages p ON p.page_id = i.page_id
         ORDER BY i.document_id, p.page_number, i.image_index
    """
    out: list[dict] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (entity_id, entity_id))
            for r in cur.fetchall():
                out.append({
                    "image_id": r[0],
                    "link_kind": r[1],
                    "source": {
                        "document_id": r[2],
                        "document_title": r[3],
                        "page_number": r[4],
                        "image_index": r[5],
                        "on_disk_path": r[6],
                    },
                    "metadata": {
                        "sha256": r[7], "phash": r[8],
                        "width": r[9], "height": r[10],
                        "bytes": r[11], "mime": r[12],
                        "is_redacted_block": r[13],
                        "extracted_at": r[14].isoformat() if r[14] else None,
                    },
                    "analyses": r[15] or [],
                })
    return out


def _aggregate_summary(mentions: list[dict]) -> dict:
    """Rollup stats over the mentions list."""
    from collections import Counter
    types: Counter = Counter()
    docs: set = set()
    methods: Counter = Counter()
    pages_per_doc: dict = {}
    for m in mentions:
        types[m["extracted"]["suggested_type"] or "unknown"] += 1
        doc_id = m["source"]["document_id"]
        docs.add(doc_id)
        methods[m["extracted"]["extraction_method"] or "unknown"] += 1
        if m["source"]["page_number"]:
            pages_per_doc.setdefault(doc_id, set()).add(m["source"]["page_number"])
    return {
        "mention_total":         len(mentions),
        "source_document_count": len(docs),
        "by_suggested_type":     dict(types),
        "by_extraction_method":  dict(methods),
        "pages_per_doc":         {str(d): sorted(p) for d, p in pages_per_doc.items()},
    }


# ─── Public API ─────────────────────────────────────────────────────────

def get_entity_dossier(
    entity_id: int,
    *,
    include_surrounding_text: bool = True,
) -> Optional[dict]:
    """Assemble the full provenance bundle for one canonical entity."""
    entity = _entity_row(entity_id)
    if not entity:
        return None

    mentions = _mentions_for_entity(entity_id, include_surrounding_text)
    clusters = _clusters_for_entity(entity_id)
    lookups = _external_lookups_for_entity(entity_id)
    promotions = _promotions_for_entity(entity_id)
    decisions = _collapse_decisions_for_entity(entity_id)
    images = _images_for_entity(entity_id)
    summary = _aggregate_summary(mentions)
    summary["image_total"] = len(images)
    summary["images_intelligence_grade"] = sum(
        1 for img in images for a in (img.get("analyses") or [])
        if a.get("is_intelligence")
    )

    concept_parent = _concept_parent(entity.get("concept_entity_id"))
    instance_count = (
        _concept_instance_count(entity_id)
        if entity.get("entity_kind") == "concept" else 0
    )
    timeline = _timeline_for_entity(entity_id, limit=100)
    assessments_block = _assessments_for_entity(entity_id, limit=100)
    summary["assessment_count"] = (len(assessments_block["by_this_entity"]) +
                                    len(assessments_block["about_this_entity"]))
    summary["event_count"] = len(timeline)

    return {
        "entity":                entity,
        "summary":               summary,
        "concept_parent":        concept_parent,
        "instance_count":        instance_count,
        "mentions":              mentions,
        "clusters":              clusters,
        "external_lookups":      lookups,
        "promotions":            promotions,
        "collapse_decisions":    decisions,
        "images":                images,
        "assessments":           assessments_block,
        "timeline":              timeline,
        "schema_version":        "dossier_v4",
    }


def get_dossier_by_qid(qid: str, tenant_id: str = "default") -> Optional[dict]:
    """Convenience wrapper: find entity by Wikidata QID, then return dossier."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT entity_id FROM entities
                 WHERE tenant_id = %s
                   AND identifiers ->> 'wikidata_qid' = %s
                 LIMIT 1
                """,
                (tenant_id, qid),
            )
            row = cur.fetchone()
            if not row:
                return None
            return get_entity_dossier(row[0])
