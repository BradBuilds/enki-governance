"""
Universal edge writer — reads envelope.relationships, writes knowledge_edges.

The runner + auto_node handle node creation; this module handles edge
creation. Running after auto_node guarantees the source node_id exists.
Target nodes may be created by upstream stages (entity resolver stamps
entity:<id> onto envelope.entities[]); those rows exist by the time we
insert edges.

Relationships honored (from the envelopevocab.envelope_v1 shape):

    envelope.relationships.mentions[]     → relationship='mentions'
    envelope.relationships.cites[]        → relationship='cites'
    envelope.relationships.cited_by[]     → relationship='cited_by'
    envelope.relationships.follows        → relationship='follows'
    envelope.relationships.supersedes     → relationship='supersedes'
    envelope.relationships.part_of[]      → relationship='part_of'

Each edge is upserted via ON CONFLICT so re-runs are idempotent.

For research workers we also honor an extra, non-standard key
`envelope.relationships.custom[]` which lets the builder specify edge
objects directly: `{target: node_id, relationship: str, strength?, evidence?}`.
This keeps research envelopes from having to fake mentions/cites for
semantically-different relationships like `supports`, `contrasts`,
`researched_from`, `related_to`, etc.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Optional

log = logging.getLogger("enki.extraction.edges")


def _edge_id(source: str, target: str, rel: str) -> str:
    return hashlib.sha1(f"{source}|{target}|{rel}".encode()).hexdigest()[:32]


def _insert_edge(conn, *, source_node_id: str, target_node_id: str,
                 relationship: str, strength: float = 0.7,
                 evidence: str = "", project_id: str = "default") -> bool:
    if not source_node_id or not target_node_id:
        return False
    if source_node_id == target_node_id:
        return False
    eid = _edge_id(source_node_id, target_node_id, relationship)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO knowledge_edges (
                    edge_id, project_id, source_node_id, target_node_id,
                    relationship, strength, evidence, ai_generated
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE)
                ON CONFLICT (source_node_id, target_node_id, relationship) DO UPDATE SET
                    strength = EXCLUDED.strength,
                    evidence = EXCLUDED.evidence
                """,
                (eid, project_id, source_node_id, target_node_id,
                 relationship, float(strength), (evidence or "")[:2000]),
            )
        return True
    except Exception as e:
        log.debug("edge insert failed %s→%s (%s): %s",
                  source_node_id, target_node_id, relationship, e)
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def upsert_edges_from_envelope(
    envelope: dict, conn, *,
    project_id: str = "default",
) -> dict:
    """Read envelope.relationships and upsert each edge. Returns stats."""
    stats = {"created": 0, "updated": 0, "skipped": 0}
    if conn is None:
        return stats
    source = envelope.get("node_id")
    if not source:
        return stats
    rels = envelope.get("relationships") or {}

    # Scalar relationships — one target each
    for key, rel in (("follows", "follows"),
                     ("supersedes", "supersedes"),
                     ("superseded_by", "superseded_by")):
        target = rels.get(key)
        if isinstance(target, str) and target:
            if _insert_edge(conn, source_node_id=source, target_node_id=target,
                            relationship=rel, strength=0.9,
                            project_id=project_id):
                stats["created"] += 1
            else:
                stats["skipped"] += 1

    # List relationships
    list_rels = (
        ("mentions", "mentions", 0.6),
        ("cites", "cites", 0.75),
        ("cited_by", "cited_by", 0.75),
        ("part_of", "part_of", 0.85),
    )
    for key, rel, default_strength in list_rels:
        items = rels.get(key) or []
        for item in items:
            target = None
            strength = default_strength
            evidence = ""
            if isinstance(item, str):
                target = item
            elif isinstance(item, dict):
                target = item.get("node_id") or item.get("target")
                strength = float(item.get("strength", default_strength))
                evidence = item.get("evidence", "") or ""
            if not target:
                stats["skipped"] += 1
                continue
            if _insert_edge(conn, source_node_id=source, target_node_id=target,
                            relationship=rel, strength=strength,
                            evidence=evidence, project_id=project_id):
                stats["created"] += 1
            else:
                stats["skipped"] += 1

    # Custom edges — research orchestrators use these for domain-specific
    # relationships (supports, contrasts, researched_from, related_to),
    # and the relationship extractor uses them for entity-to-entity
    # semantic triples (met_with, acquired, sanctioned, etc.).
    #
    # A custom edge may specify `subject_id` to override the default
    # source (the envelope's own node_id). That's how we persist edges
    # BETWEEN two entities found in an article — the envelope is the
    # article, but the edge is (Petrov)→(Wei).
    for e in (rels.get("custom") or []):
        if not isinstance(e, dict):
            stats["skipped"] += 1
            continue
        target = e.get("target") or e.get("node_id")
        rel = e.get("relationship") or e.get("type")
        if not target or not rel:
            stats["skipped"] += 1
            continue
        edge_source = e.get("subject_id") or source
        if _insert_edge(
            conn, source_node_id=edge_source, target_node_id=target,
            relationship=rel,
            strength=float(e.get("strength", 0.7)),
            evidence=e.get("evidence", ""),
            project_id=project_id,
        ):
            stats["created"] += 1
        else:
            stats["skipped"] += 1

    # Mentioned entities get edges too — entity resolver stamps
    # entity:<id> onto envelope.entities[].node_id
    for ent in envelope.get("entities") or []:
        if not isinstance(ent, dict):
            continue
        target = ent.get("node_id")
        if not target or not target.startswith("entity:"):
            continue
        ev = ent.get("label") or ent.get("name") or ""
        if _insert_edge(
            conn, source_node_id=source, target_node_id=target,
            relationship="mentions",
            strength=float(ent.get("weight", 0.6)),
            evidence=ev, project_id=project_id,
        ):
            stats["created"] += 1
        else:
            stats["skipped"] += 1

    try:
        conn.commit()
    except Exception:
        pass
    return stats
