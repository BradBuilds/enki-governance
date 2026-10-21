"""
Knowledge node CRUD API — Phase D.

Users create nodes (dossiers, questions, theories, FAQs) that the
research worker will extend automatically as new signal_extractions
arrive. This route handles the human-initiated creation + editing path.

Routes:
  POST   /nodes                    — create a new node
  PATCH  /nodes/{node_id}          — update title/narrative/research_enabled
  POST   /nodes/{node_id}/cite     — attach a signal_extraction as evidence
  POST   /nodes/{node_id}/research — manually trigger a research pass
  GET    /nodes                    — list nodes (for the Intelligence tab)
  GET    /nodes/{node_id}          — node detail with cited evidence

A node written by this API follows the canonical envelope schema:
  - identity.title_display  = user input
  - identity.canonical_name = same
  - summary.narrative       = user description
  - node_type               = user choice (topic/entity/place/question/
                              theory/dossier/faq)
  - master_*_id             = optional link to an existing master row
  - relationships.cites[]   = starts empty, grows as research runs
  - evidence.source_count   = 0 initially
  - governance              = LDU lineage stamped at creation
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Header, Query
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

log = logging.getLogger("enki.nodes")
router = APIRouter(prefix="/nodes", tags=["nodes"])

ALLOWED_NODE_TYPES = {
    "topic", "entity", "place", "question", "theory", "dossier",
    "faq", "assessment", "case", "pattern", "chronicle",
}


def _new_node_id(node_type: str, title: str) -> str:
    """Deterministic id for human-created nodes. Uses a short uuid
    prefix so two nodes with the same title never collide."""
    slug = hashlib.sha1(f"{node_type}|{title}|{uuid.uuid4().hex[:8]}"
                        .encode("utf-8")).hexdigest()[:12]
    return f"{node_type}:{slug}"


def _build_envelope(
    *,
    node_id: str,
    node_type: str,
    subtype: Optional[str],
    title: str,
    narrative: str,
    master_entity_id: Optional[int],
    master_topic_id: Optional[int],
) -> dict:
    """Build a canonical envelope for a newly-created node."""
    from core.ldu.lineage import get_lineage_stamp
    stamp = get_lineage_stamp()
    now = datetime.now(timezone.utc).isoformat()

    return {
        "envelope_version": "v1",
        "node_id": node_id,
        "node_type": node_type,
        "subtype": subtype,
        "governance": {
            "gdu_canonical": bool(stamp.get("gdu_canonical")),
            "active_set_version": stamp.get("active_set_version") or "",
            "pack_versions": stamp.get("pack_versions") or {},
            "ldu_t_id": None,
            "ldu_p_id": None,
            "admitted_at": now,
        },
        "identity": {
            "title_raw": title,
            "title_display": title,
            "title_source": "human_edit",
            "subtitle": None,
            "canonical_name": title,
            "aliases": [],
            "byline_raw": None,
            "byline_resolved": None,
        },
        "summary": {
            "headline": title,
            "narrative": narrative,
            "key_facts": [],
            "quotes": [],
            "feed_summary": None,
            "feed_summary_clean": None,
            "feed_summary_translated": None,
            "body_original": None,
            "body_translated": None,
            "structured_fields": None,
            "domain_classification": "other",
        },
        "temporal": {
            "event_time": now,
            "ingested_at": now,
            "precision": "second",
        },
        "spatial": {
            "coordinates": None, "bbox": None,
            "place_text": [], "resolved_places": [],
            "extraction_method": "none",
        },
        "entities": [],
        "topics": [],
        "events_referenced": [],
        "source": {
            "node_id": "source:human",
            "url": "",
            "outlet_name": "Human",
            "language_declared": "en",
            "language_detected": "en",
            "language_displayed": "en",
            "editorial_stance": "unknown",
            "fetched_at": None,
        },
        "media": {
            "thumbnail": None, "thumbnail_source": "none",
            "images": [], "videos": [], "audio": [], "embeds": [],
        },
        "evidence": {
            "epistemic_type": "observation",
            "confidence": 0.5,
            "source_count": 0,
            "corroborations": 0,
            "contradictions": 0,
            "background_relevance": "background",
            "relevance_to_active_cases": [],
            "assessment_nodes": [],
        },
        "relationships": {
            "mentions": [],
            "cites": [],
            "cited_by": [],
            "follows": None,
            "supersedes": None,
            "superseded_by": None,
            "part_of": [],
        },
        "completeness": {
            "score": 0.0, "tier": "stub",
            "filled_slots": 0, "total_slots": 0, "gaps": [],
        },
        "provenance": {
            "extraction_passes": [{
                "stage": "human_create",
                "ts": now,
                "model": None,
                "model_version": None,
                "prompt_version": None,
                "fields_filled": [
                    "identity.title_display",
                    "summary.narrative",
                    "node_type",
                ],
                "duration_ms": 0,
                "success": True,
                "error": None,
            }],
            "human_edits": [],
        },
        "card_actions": [],
    }


@router.post("")
async def create_node(
    body: dict = Body(...),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Create a new knowledge node.

    Body fields:
        node_type          — required (topic/entity/place/question/
                              theory/dossier/faq/assessment)
        subtype            — optional
        title              — required, used for display + canonical_name
        narrative          — optional, free-text description
        master_topic_id    — optional, link to an existing topic master row
        master_entity_id   — optional (post-unification covers places too)
        research_enabled   — optional, default True
    """
    from storage.postgres.db import get_connection

    node_type = (body.get("node_type") or "").strip().lower()
    if node_type not in ALLOWED_NODE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"node_type must be one of: {sorted(ALLOWED_NODE_TYPES)}",
        )

    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")

    narrative = (body.get("narrative") or "").strip()
    subtype = body.get("subtype")
    master_topic_id = body.get("master_topic_id")
    master_entity_id = body.get("master_entity_id")
    research_enabled = bool(body.get("research_enabled", True))

    node_id = _new_node_id(node_type, title)
    envelope = _build_envelope(
        node_id=node_id,
        node_type=node_type,
        subtype=subtype,
        title=title,
        narrative=narrative,
        master_entity_id=master_entity_id,
        master_topic_id=master_topic_id,
    )

    try:
        from core.extraction.completeness import score_envelope
        score_envelope(envelope)
    except Exception:
        pass

    try:
        from core.extraction.auto_node import upsert_knowledge_node
        result = upsert_knowledge_node(
            envelope,
            _get_conn_raw(),
            tenant_id=auth.tenant_id,
            project_id="default",
        )
        if result.get("status") != "ok":
            raise HTTPException(status_code=500, detail=result.get("reason"))

        # Flip research_enabled + attach master FK pointers that aren't in
        # the envelope but need to be on the row
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE knowledge_nodes
                       SET research_enabled = %s,
                           master_topic_id = COALESCE(%s, master_topic_id),
                           master_entity_id = COALESCE(%s, master_entity_id)
                     WHERE tenant_id = %s AND node_id = %s
                    """,
                    (research_enabled, master_topic_id,
                     master_entity_id, auth.tenant_id, node_id),
                )
            conn.commit()

    except HTTPException:
        raise
    except Exception as e:
        log.exception("create_node failed")
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "status": "created",
        "node_id": node_id,
        "node_type": node_type,
        "title": title,
        "research_enabled": research_enabled,
    }


def _get_conn_raw():
    """Return a raw psycopg2 connection (auto_node needs one that's
    not inside a `with` block so the function can commit on its own)."""
    from storage.postgres.db import get_connection
    return get_connection().__enter__()


@router.get("")
async def list_nodes(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    node_type: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
):
    """List knowledge nodes (for the Intelligence tab)."""
    from storage.postgres.db import get_connection

    clauses = ["tenant_id = %s", "envelope IS NOT NULL", "envelope <> '{}'::jsonb"]
    params: list = [auth.tenant_id]
    if node_type:
        clauses.append("node_type = %s")
        params.append(node_type)
    params.extend([limit, offset])

    nodes: list[dict] = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Pull nodes + per-node badge counts (source_count, open_conflicts)
                # in one query so list views can render the full card contract
                # without N+1 follow-up fetches.
                cur.execute(
                    f"""
                    SELECT kn.node_id, kn.node_type, kn.subtype,
                           kn.envelope->'identity'->>'title_display' AS title,
                           kn.envelope->'summary'->>'narrative' AS narrative,
                           kn.envelope->'evidence'->>'source_count' AS source_count,
                           kn.completeness_score, kn.completeness_tier,
                           kn.master_topic_id, kn.master_entity_id,
                           kn.research_enabled, kn.last_research_at, kn.research_pass_count,
                           kn.updated_at,
                           COALESCE(src.article_count, 0)    AS source_article_count,
                           COALESCE(src.outlet_count,  0)    AS source_outlet_count,
                           COALESCE(conf.open_conflicts, 0)  AS open_conflicts_count,
                           kn.envelope                        AS envelope
                      FROM knowledge_nodes kn
                      LEFT JOIN LATERAL (
                        SELECT COUNT(*) AS article_count,
                               COUNT(DISTINCT se.source_outlet) AS outlet_count
                          FROM knowledge_edges ke
                          JOIN signal_extractions se ON se.node_id = ke.source_node_id
                         WHERE ke.target_node_id = kn.node_id
                           AND ke.relationship = 'mentions'
                           AND se.tenant_id = kn.tenant_id
                      ) src ON TRUE
                      LEFT JOIN LATERAL (
                        SELECT COUNT(*) AS open_conflicts
                          FROM node_conflicts nc
                         WHERE nc.tenant_id = kn.tenant_id
                           AND nc.node_id   = kn.node_id
                           AND nc.resolution = 'unresolved'
                      ) conf ON TRUE
                     WHERE {' AND '.join('kn.' + c if not c.startswith(('kn.', 'src.', 'conf.')) else c for c in clauses)}
                     ORDER BY kn.updated_at DESC
                     LIMIT %s OFFSET %s
                    """,
                    params,
                )
                from core.extraction.envelope import build_envelope_summary
                cols = [d[0] for d in cur.description]
                for r in cur.fetchall():
                    row = dict(zip(cols, r))
                    raw_env = row.pop("envelope", None)
                    if isinstance(raw_env, memoryview):
                        raw_env = json.loads(bytes(raw_env))
                    elif isinstance(raw_env, str):
                        try: raw_env = json.loads(raw_env)
                        except Exception: raw_env = None
                    # Phase 3.4 — envelope-first contract for chip navigation.
                    row["envelope_summary"] = build_envelope_summary(raw_env)
                    nodes.append(row)
    except Exception as e:
        log.exception("list_nodes failed")
        raise HTTPException(status_code=500, detail=str(e))

    return {"nodes": nodes, "count": len(nodes)}


@router.get("/{node_id}")
async def get_node(
    node_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
    show_low_relevance: bool = Query(False),
):
    """Return one node with everything needed to render a full card:
    envelope, cited evidence, connected neighbors grouped by type, conflicts,
    and a source-stack summary. The front-end uses this as the single load
    for /nodes/[id] pages."""
    from storage.postgres.db import get_connection

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT node_id, node_type, subtype, envelope,
                           completeness_score, completeness_tier,
                           master_topic_id, master_entity_id,
                           research_enabled, last_research_at, research_pass_count,
                           cited_extraction_ids, created_at, updated_at,
                           title, description
                      FROM knowledge_nodes
                     WHERE tenant_id = %s AND node_id = %s
                    """,
                    (auth.tenant_id, node_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="not found")
                cols = [d[0] for d in cur.description]
                node = dict(zip(cols, row))

                # Cited evidence (manual citations, legacy path)
                cited_ids = node.get("cited_extraction_ids") or []
                if cited_ids:
                    cur.execute(
                        """
                        SELECT extraction_id, node_id,
                               envelope->'identity'->>'title_display' AS title,
                               envelope->'source'->>'outlet_name' AS outlet,
                               event_time
                          FROM signal_extractions
                         WHERE extraction_id = ANY(%s)
                         ORDER BY event_time DESC
                         LIMIT 50
                        """,
                        (cited_ids[:50],),
                    )
                    cited_cols = [d[0] for d in cur.description]
                    node["cited_evidence"] = [
                        dict(zip(cited_cols, r)) for r in cur.fetchall()
                    ]
                else:
                    node["cited_evidence"] = []

                # Connected neighbors, grouped by node_type.
                # Union of outbound + inbound edges; we fold direction into
                # each row so the client can render arrows sanely.
                cur.execute(
                    """
                    WITH edges AS (
                        SELECT target_node_id AS other, relationship, strength, 'out' AS direction
                          FROM knowledge_edges
                         WHERE source_node_id = %(nid)s
                        UNION ALL
                        SELECT source_node_id AS other, relationship, strength, 'in'  AS direction
                          FROM knowledge_edges
                         WHERE target_node_id = %(nid)s
                    )
                    SELECT kn.node_id, kn.node_type, kn.subtype,
                           -- Phase 8a: prefer envelope title_display, then
                           -- canonical_name, then the title COLUMN (assessment
                           -- nodes populate this at admit time but never the
                           -- envelope), then entity_name, finally node_id.
                           COALESCE(
                               kn.envelope->'identity'->>'title_display',
                               kn.envelope->'identity'->>'canonical_name',
                               NULLIF(kn.title, ''),
                               NULLIF(kn.entity_name, ''),
                               kn.node_id
                           ) AS title,
                           e.relationship, e.strength, e.direction
                      FROM edges e
                      JOIN knowledge_nodes kn ON kn.node_id = e.other
                     WHERE kn.tenant_id = %(tenant)s
                     ORDER BY e.strength DESC NULLS LAST
                     LIMIT 500
                    """,
                    {"nid": node_id, "tenant": auth.tenant_id},
                )
                conn_cols = [d[0] for d in cur.description]
                raw_neighbors = [dict(zip(conn_cols, r)) for r in cur.fetchall()]

                # Dedup neighbors by node_id — the SQL UNION ALL preserves
                # one row per (edge direction × relationship), so an entity
                # connected by multiple relationship types (e.g. 'investigated'
                # AND 'filed_with' from a Phase-4 investigation) shows up N
                # times. Pre-Phase-4 this was rare and the UI's keyed-each
                # block tolerated it; post-Phase-4 it's common and Svelte
                # raises each_key_duplicate. Collapse to one chip per
                # neighbor with all relationships joined.
                merged: dict = {}
                for n in raw_neighbors:
                    nid = n.get("node_id")
                    if not nid:
                        continue
                    if nid not in merged:
                        n["relationships"] = [n.get("relationship")] if n.get("relationship") else []
                        merged[nid] = n
                    else:
                        rel = n.get("relationship")
                        if rel and rel not in merged[nid]["relationships"]:
                            merged[nid]["relationships"].append(rel)
                        # Keep the strongest strength
                        if (n.get("strength") or 0) > (merged[nid].get("strength") or 0):
                            merged[nid]["strength"] = n.get("strength")
                # Reduce to a single representative relationship label for
                # legacy UI templates; full list available on .relationships.
                for n in merged.values():
                    rels = n.get("relationships") or []
                    n["relationship"] = ", ".join(rels[:3]) + (f" +{len(rels)-3}" if len(rels) > 3 else "")
                neighbors = list(merged.values())

                # Group by node_type bucket. UI renders each bucket as chip row.
                BUCKETS = {
                    "entity":    "entities",
                    "place":     "places",
                    "event":     "events",
                    "topic":     "topics",
                    "document":  "documents",
                    "source":    "documents",   # legacy node_type for articles
                    "chronicle": "chronicles",
                    "assessment": "assessments",
                    "pattern":   "patterns",
                }
                connected: dict[str, list] = {}
                for n in neighbors:
                    bucket = BUCKETS.get(n.get("node_type") or "", "other")
                    connected.setdefault(bucket, []).append(n)
                # Re-sort by strength after dedup
                for bucket_list in connected.values():
                    bucket_list.sort(key=lambda x: (x.get("strength") or 0), reverse=True)
                node["connected"] = connected

                # Source-stack summary — count + distinct outlets from every
                # document that `mentions` this node.
                cur.execute(
                    """
                    SELECT COUNT(*)                                  AS article_count,
                           COUNT(DISTINCT se.source_outlet)          AS outlet_count,
                           MAX(se.event_time)                        AS latest_at,
                           MIN(se.event_time)                        AS earliest_at
                      FROM knowledge_edges ke
                      JOIN signal_extractions se ON se.node_id = ke.source_node_id
                     WHERE ke.target_node_id = %s
                       AND ke.relationship  = 'mentions'
                       AND se.tenant_id     = %s
                    """,
                    (node_id, auth.tenant_id),
                )
                src_row = cur.fetchone()
                node["sources_summary"] = {
                    "article_count": int(src_row[0] or 0),
                    "outlet_count":  int(src_row[1] or 0),
                    "latest_at":     src_row[2].isoformat() if src_row[2] else None,
                    "earliest_at":   src_row[3].isoformat() if src_row[3] else None,
                }

                # Conflicts recorded for this node — warning+ first, info last.
                cur.execute(
                    """
                    SELECT conflict_id, field_path, field_kind,
                           value_a, sources_a, value_b, sources_b,
                           severity, resolution, detected_by, detected_at
                      FROM node_conflicts
                     WHERE tenant_id = %s AND node_id = %s
                     ORDER BY
                        CASE severity
                          WHEN 'critical' THEN 0
                          WHEN 'warning'  THEN 1
                          WHEN 'info'     THEN 2
                          ELSE 3
                        END,
                        detected_at DESC
                     LIMIT 50
                    """,
                    (auth.tenant_id, node_id),
                )
                conf_cols = [d[0] for d in cur.description]
                node["conflicts"] = [
                    dict(zip(conf_cols, r)) for r in cur.fetchall()
                ]

                # ── Research dossier (Phase 5 of research-collapse plan) ──
                # Joins investigations + findings + discovered + enrichment
                # for this node's master_entity_id, so the front-end can
                # render an "Intelligence" / "Connections" / "Profile" view
                # without a separate fetch round-trip.
                #
                # show_low_relevance toggle (Phase 7d): default False hides
                # findings the relevance scorer marked < 0.5. Toggle to True
                # to see everything including low-relevance/wrong-entity
                # noise (useful for the entity SPLIT workflow: looking at
                # the rejects to decide which ones belong to a sibling entity).
                node["research"] = _build_research_dossier(
                    cur, auth.tenant_id, node.get("master_entity_id"),
                    show_low_relevance=show_low_relevance,
                )
    except HTTPException:
        raise
    except Exception as e:
        log.exception("get_node failed")
        raise HTTPException(status_code=500, detail=str(e))

    return node


def _derive_source_url(source: str, data: dict) -> str | None:
    """Construct a clickable URL to the original source for this finding.

    Each source has its own URL convention. We compute on read instead of
    storing in the data blob so the same logic applies to historical
    findings ingested before the source handlers captured URLs explicitly.

    Used by the dossier endpoint so the UI can render every finding as a
    clickable verification link — the "click to verify or reject"
    workflow for entity disambiguation.
    """
    if not isinstance(data, dict):
        return None
    # If the source handler already captured a URL, prefer that
    for key in ("source_url", "document_url", "url", "absolute_url", "permalink"):
        if data.get(key):
            v = str(data[key])
            return v if v.startswith("http") else f"https://www.courtlistener.com{v}"

    if source == "sec_edgar":
        cik = data.get("cik")
        if cik:
            return f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=&dateb=&owner=include&count=40"
    elif source == "fec":
        cid = data.get("committee_id") or data.get("candidate_id")
        if cid:
            kind = "committee" if data.get("committee_id") else "candidate"
            return f"https://www.fec.gov/data/{kind}/{cid}/"
    elif source == "propublica_nonprofits":
        ein = data.get("ein") or data.get("EIN")
        if ein:
            return f"https://projects.propublica.org/nonprofits/organizations/{str(ein).replace('-', '')}"
    elif source == "courtlistener":
        # Build a search URL from the case_name + docket so user can find it
        case = data.get("case_name") or data.get("title", "")
        if case:
            from urllib.parse import quote_plus
            return f"https://www.courtlistener.com/?type=r&q={quote_plus(case)}"
    elif source == "usaspending":
        award_id = data.get("award_id") or data.get("generated_unique_award_id")
        if award_id:
            return f"https://www.usaspending.gov/award/{award_id}"
    elif source == "loc":
        loc_id = data.get("id") or data.get("loc_id")
        if loc_id:
            return f"https://id.loc.gov/authorities/names/{loc_id}"
    elif source == "fdic":
        cert = data.get("cert") or data.get("certificate")
        if cert:
            return f"https://banks.data.fdic.gov/api/institutions?filters=CERT:{cert}"
    elif source == "ofac":
        return "https://sanctionssearch.ofac.treas.gov/"
    return None


def _derive_map_url(data: dict) -> str | None:
    """If the finding has an address, return a Google Maps search URL so the
    UI can render a 'View on map' chip. The 'addresses should be mapped'
    requirement — collapsing data to a verifiable geographic source."""
    if not isinstance(data, dict):
        return None
    addr = data.get("address") or data.get("location") or data.get("addr")
    if isinstance(addr, str) and addr.strip():
        from urllib.parse import quote_plus
        return f"https://www.google.com/maps/search/?api=1&query={quote_plus(addr)}"
    return None


def _build_research_dossier(cur, tenant_id: str, entity_id, show_low_relevance: bool = False):
    """Pack everything we know about a canonical entity into a single dict.

    Empty (zero-everything) result if entity_id is None or no investigations
    have run yet — the UI will hide the section in that case.
    """
    dossier = {
        "investigations": [],
        "findings": [],
        "discovered": [],
        "enrichment": {},
        "totals": {
            "investigation_count": 0,
            "finding_count": 0,
            "discovered_count": 0,
            "enrichment_complete": 0,
        },
        # Phase 8b — additional data classes surfaced on the unified dossier
        # so the Overview tab shows everything we know, not just research.
        "identifiers": [],
        "documents": [],
        "images": [],
        "llm_relationships": [],
        "governance_conflicts": [],
        "watch_topics": [],
        "review_status": None,
        "splits": [],
        # Phase 8b.6 — physical-asset profile + locations from subsidiary chain
        "addresses": [],
        "aircraft": [],
        "vessels": [],
        "subsidiaries": [],
        "extended_addresses": [],   # rolled up from subsidiaries
        "extended_assets": [],
    }
    if not entity_id:
        return dossier

    try:
        # Investigations targeting this entity (depth=0 = direct, >0 = cascade-discovered)
        cur.execute(
            """
            SELECT investigation_id, status, depth, sources_completed,
                   findings_count, entities_discovered,
                   started_at, completed_at, discovered_from
              FROM entity_investigations
             WHERE tenant_id = %s AND entity_id = %s
             ORDER BY started_at DESC NULLS LAST
             LIMIT 20
            """,
            (tenant_id, entity_id),
        )
        cols = [d[0] for d in cur.description]
        invs = []
        for r in cur.fetchall():
            row = dict(zip(cols, r))
            for tk in ("started_at", "completed_at"):
                if row.get(tk):
                    row[tk] = row[tk].isoformat()
            invs.append(row)
        dossier["investigations"] = invs
        dossier["totals"]["investigation_count"] = len(invs)

        # Findings + discovered come from the COLLAPSE VIEWS (migration 047).
        # Raw investigation_findings/discovered are append-only audit trail;
        # the views aggregate by natural dedup key so the UI sees one row
        # per unique fact with occurrence_count + source_investigation_ids
        # + cross-source attribution. This is the same architectural pattern
        # as signal_aliases for cross-source news dedup.
        # Phase 7d/e: per-finding relevance + human attribution overlay.
        # Each collapsed cluster gets:
        #   - max_rel.score / reason : best LLM interpretation
        #   - human.decision         : user's override if any (confirmed
        #                              wins over score; rejected hides
        #                              regardless of score)
        # Filter < 0.5 by default; show_low_relevance toggle reveals all.
        # Rejected findings always hidden unless show_low_relevance=True.
        if show_low_relevance:
            visibility_clause = ""
        else:
            visibility_clause = """
              AND (
                  -- Show if human confirmed it OR no human decision + score >= 0.5
                  human.decision = 'confirmed'
                  OR (human.decision IS NULL
                      AND (max_rel.score >= 0.5 OR max_rel.score IS NULL))
              )
            """
        cur.execute(
            f"""
            SELECT v.source, v.finding_type, v.title, v.data, v.confidence,
                   v.occurrence_count, v.source_investigation_ids,
                   v.first_seen_at, v.last_seen_at,
                   max_rel.score AS relevance_score,
                   max_rel.reason AS relevance_reason,
                   max_rel.finding_id AS finding_id,
                   human.decision AS human_decision,
                   human.reviewer AS human_reviewer,
                   human.reviewer_note AS human_note
              FROM investigation_finding_collapsed v
              LEFT JOIN LATERAL (
                  SELECT MAX(fi.relevance_score) AS score,
                         (ARRAY_AGG(fi.relevance_reason
                            ORDER BY fi.relevance_score DESC NULLS LAST))[1] AS reason,
                         (ARRAY_AGG(fi.finding_id
                            ORDER BY fi.relevance_score DESC NULLS LAST,
                                     fi.created_at DESC))[1] AS finding_id
                    FROM investigation_findings fi
                   WHERE fi.investigation_id = ANY(v.source_investigation_ids)
                     AND LOWER(REGEXP_REPLACE(fi.title, '\\s+', ' ', 'g'))
                          = v.normalized_title
              ) max_rel ON TRUE
              LEFT JOIN finding_attributions human
                ON human.tenant_id = v.tenant_id
               AND human.finding_id = max_rel.finding_id
             WHERE v.tenant_id = %s AND v.entity_id = %s
               {visibility_clause}
             ORDER BY
               CASE WHEN human.decision = 'confirmed' THEN 0 ELSE 1 END,
               max_rel.score DESC NULLS LAST,
               v.confidence DESC NULLS LAST,
               v.occurrence_count DESC
             LIMIT 200
            """,
            (tenant_id, entity_id),
        )
        cols = [d[0] for d in cur.description]
        findings = []
        for r in cur.fetchall():
            row = dict(zip(cols, r))
            for tk in ("first_seen_at", "last_seen_at"):
                if row.get(tk):
                    row[tk] = row[tk].isoformat()
            # Compute clickable links so the UI can render verify/expand
            # affordances on every finding (Phase 5b — disambiguation UX).
            row["source_url"] = _derive_source_url(row.get("source") or "", row.get("data") or {})
            row["map_url"] = _derive_map_url(row.get("data") or {})
            findings.append(row)
        dossier["findings"] = findings
        dossier["totals"]["finding_count"] = len(findings)

        cur.execute(
            """
            SELECT name, entity_type, relationship,
                   admitted_entity_id, sources,
                   occurrence_count, source_investigation_ids,
                   first_seen_at, last_seen_at
              FROM investigation_discovered_collapsed
             WHERE tenant_id = %s AND parent_entity_id = %s
             ORDER BY relationship, name
             LIMIT 200
            """,
            (tenant_id, entity_id),
        )
        cols = [d[0] for d in cur.description]
        disc = []
        for r in cur.fetchall():
            row = dict(zip(cols, r))
            for tk in ("first_seen_at", "last_seen_at"):
                if row.get(tk):
                    row[tk] = row[tk].isoformat()
            disc.append(row)
        dossier["discovered"] = disc
        dossier["totals"]["discovered_count"] = len(disc)

        # Pipeline B enrichment results (Wikipedia + Wikidata + sanctions)
        # Grouped by enrichment_type so the UI can render each section
        # under its own header (Biography / Identity / Sanctions / etc.).
        cur.execute(
            """
            SELECT enrichment_type, status, result, completed_at
              FROM entity_enrichment_queue
             WHERE tenant_id = %s AND entity_id = %s
               AND status IN ('complete', 'failed', 'skipped')
             ORDER BY completed_at DESC NULLS LAST
            """,
            (tenant_id, entity_id),
        )
        enrichment_by_type: dict = {}
        complete_count = 0
        for et, status, result, completed_at in cur.fetchall():
            if et in enrichment_by_type:
                continue  # keep most-recent only
            if status == "complete":
                complete_count += 1
            enrichment_by_type[et] = {
                "status": status,
                "result": result,
                "completed_at": completed_at.isoformat() if completed_at else None,
            }
        dossier["enrichment"] = enrichment_by_type
        dossier["totals"]["enrichment_complete"] = complete_count
    except Exception as e:
        # Best-effort — don't fail the whole node load if research join breaks.
        # Surface at warning so we notice; cards remain usable without research.
        log.warning("research dossier build failed for entity_id=%s: %s",
                    entity_id, e)

    # ── Phase 8b: surface every other data class for this entity ─────────
    # All best-effort. A failure in any single section logs at warning and
    # leaves that section as the empty default — the card stays usable.
    _add_dossier_section(dossier, "identifiers", cur, """
        SELECT id_type, id_number, country, state, issuer,
               issue_date, expiry_date, verified, scan_url
        FROM entity_identifiers
        WHERE node_id = %s OR contact_id IN (
            SELECT contact_id FROM contacts WHERE entity_id = %s
        )
        ORDER BY verified DESC NULLS LAST, id_type
        LIMIT 50
    """, (f"entity:{entity_id}", entity_id))

    _add_dossier_section(dossier, "documents", cur, """
        SELECT document_id, doc_type, title, classification,
               file_path, scan_url, created_at
        FROM entity_documents
        WHERE node_id = %s
        ORDER BY created_at DESC
        LIMIT 50
    """, (f"entity:{entity_id}",))

    _add_dossier_section(dossier, "images", cur, """
        SELECT image_id, image_type, file_path, thumbnail_url,
               is_primary, latitude, longitude, captured_at
        FROM entity_images
        WHERE node_id = %s
        ORDER BY is_primary DESC, captured_at DESC NULLS LAST
        LIMIT 30
    """, (f"entity:{entity_id}",))

    # LLM-extracted entity_relationships — these are noisy free-form edges
    # (different from the research-grade discovered entities). Surface them
    # marked as such so the user can spot bad ones.
    _add_dossier_section(dossier, "llm_relationships", cur, """
        SELECT er.relationship, er.confidence, er.source,
               er.evidence, er.article_count, er.is_current,
               e2.entity_id AS target_entity_id,
               e2.canonical_name AS target_name,
               e2.canonical_type AS target_type
          FROM entity_relationships er
          JOIN entities e2 ON e2.entity_id = er.target_entity_id
         WHERE er.source_entity_id = %s
         ORDER BY er.confidence DESC NULLS LAST, er.article_count DESC NULLS LAST
         LIMIT 50
    """, (entity_id,))

    _add_dossier_section(dossier, "governance_conflicts", cur, """
        SELECT conflict_id, conflict_type, severity, competing_values,
               auto_resolved, auto_resolution, detected_by, detected_at
          FROM governance_conflicts
         WHERE entity_id = %s OR %s = ANY(related_entity_ids)
         ORDER BY detected_at DESC
         LIMIT 20
    """, (entity_id, entity_id))

    _add_dossier_section(dossier, "watch_topics", cur, """
        SELECT wt.topic_id, wt.title, wt.deep_research_status,
               wte.added_at
          FROM watch_topic_entities wte
          JOIN watch_topics wt ON wt.topic_id = wte.topic_id
         WHERE wte.canonical_entity_id = %s
         ORDER BY wte.added_at DESC
         LIMIT 20
    """, (entity_id,))

    # Review queue — is THIS entity flagged?
    try:
        cur.execute("""
            SELECT review_id, flagged_reason, flagged_at, status,
                   findings_count, entities_discovered, signal_count,
                   reviewed_at, reviewed_by, resolution_note
              FROM entity_review_queue
             WHERE entity_id = %s AND status = 'pending'
             ORDER BY flagged_at DESC LIMIT 1
        """, (entity_id,))
        row = cur.fetchone()
        if row:
            cols = [d[0] for d in cur.description]
            r = dict(zip(cols, row))
            for k in ("flagged_at", "reviewed_at"):
                if r.get(k):
                    r[k] = r[k].isoformat()
            dossier["review_status"] = r
    except Exception as e:
        log.warning("review_status fetch failed for entity_id=%s: %s", entity_id, e)

    _add_dossier_section(dossier, "splits", cur, """
        SELECT split_id, parent_entity_id, new_entity_id,
               split_at, split_by, reason, reassigned_findings
          FROM entity_splits
         WHERE parent_entity_id = %s OR new_entity_id = %s
         ORDER BY split_at DESC LIMIT 10
    """, (entity_id, entity_id))

    # ── Phase 8b.6 — physical-asset profile ───────────────────────────────
    # Direct addresses for this entity (from entity_addresses, populated by
    # the geocoder worker + investigation-finding hook).
    _add_dossier_section(dossier, "addresses", cur, """
        SELECT address_id, address_type, raw_address,
               street, city, region, postal_code, country,
               lat, lon, geocoded_at, source, confidence
          FROM entity_addresses
         WHERE entity_id = %s
         ORDER BY confidence DESC NULLS LAST, address_type
         LIMIT 50
    """, (entity_id,))

    # Aircraft owned/registered to this entity, via entity_relationships
    # filtered to ownership-class edges + target type=aircraft.
    _add_dossier_section(dossier, "aircraft", cur, """
        SELECT er.relationship, er.confidence, er.source, er.evidence,
               e2.entity_id AS asset_entity_id,
               e2.canonical_name AS asset_name,
               e2.identifiers AS asset_identifiers
          FROM entity_relationships er
          JOIN entities e2 ON e2.entity_id = er.target_entity_id
         WHERE er.source_entity_id = %s
           AND er.relationship IN ('owns','registered_to','controls','held_by')
           AND e2.canonical_type = 'aircraft'
         ORDER BY er.confidence DESC NULLS LAST
         LIMIT 30
    """, (entity_id,))

    # Vessels — same pattern, target type=vessel.
    _add_dossier_section(dossier, "vessels", cur, """
        SELECT er.relationship, er.confidence, er.source, er.evidence,
               e2.entity_id AS asset_entity_id,
               e2.canonical_name AS asset_name,
               e2.identifiers AS asset_identifiers
          FROM entity_relationships er
          JOIN entities e2 ON e2.entity_id = er.target_entity_id
         WHERE er.source_entity_id = %s
           AND er.relationship IN ('owns','registered_to','controls','held_by')
           AND e2.canonical_type = 'vessel'
         ORDER BY er.confidence DESC NULLS LAST
         LIMIT 30
    """, (entity_id,))

    # Direct subsidiaries / controlled entities (depth=1).
    _add_dossier_section(dossier, "subsidiaries", cur, """
        SELECT er.relationship, er.confidence, er.source, er.evidence,
               e2.entity_id AS subsidiary_entity_id,
               e2.canonical_name AS subsidiary_name,
               e2.canonical_type AS subsidiary_type,
               e2.signal_count
          FROM entity_relationships er
          JOIN entities e2 ON e2.entity_id = er.target_entity_id
         WHERE er.source_entity_id = %s
           AND er.relationship IN ('owns','subsidiary_of','controls',
                                    'beneficial_owner_of','parent_of')
         ORDER BY er.confidence DESC NULLS LAST, e2.signal_count DESC NULLS LAST
         LIMIT 50
    """, (entity_id,))

    # ── Subsidiary chain walker (depth=3, ownership-edge whitelist) ──────
    # Roll up addresses + assets from controlled entities so Vale's dossier
    # surfaces Harbor House even though it's owned via Harbor House Club LLC.
    try:
        _walk_ownership_chain_into_dossier(dossier, cur, tenant_id, entity_id, max_depth=3)
    except Exception as e:
        log.warning("ownership chain walk failed for entity_id=%s: %s", entity_id, e)

    return dossier


def _walk_ownership_chain_into_dossier(
    dossier: dict, cur, tenant_id: str, root_entity_id: int, max_depth: int = 3,
) -> None:
    """Walk owns/subsidiary_of/controls/beneficial_owner_of edges from
    root_entity_id up to max_depth, collecting addresses + assets along the
    way. Populates dossier['extended_addresses'] and ['extended_assets'].

    Phase 8b.6.4. Edge-type whitelist prevents the walker from grabbing
    unrelated entities through co_occurred / mentioned_with edges.
    """
    OWNERSHIP_EDGES = (
        'owns', 'subsidiary_of', 'controls',
        'beneficial_owner_of', 'parent_of', 'held_by',
    )
    visited: set[int] = {root_entity_id}
    frontier: list[int] = [root_entity_id]

    for _depth in range(max_depth):
        if not frontier:
            break
        cur.execute("""
            SELECT DISTINCT er.target_entity_id
              FROM entity_relationships er
             WHERE er.tenant_id = %s
               AND er.source_entity_id = ANY(%s)
               AND er.relationship = ANY(%s)
        """, (tenant_id, frontier, list(OWNERSHIP_EDGES)))
        next_frontier: list[int] = []
        for (eid,) in cur.fetchall():
            if eid and eid not in visited:
                visited.add(eid)
                next_frontier.append(eid)
        frontier = next_frontier

    # Strip the root from the chain so we only roll up subsidiaries
    chain_ids = sorted(visited - {root_entity_id})
    if not chain_ids:
        return

    # Pull every address belonging to any chain entity
    cur.execute("""
        SELECT ea.address_id, ea.entity_id, ea.address_type, ea.raw_address,
               ea.street, ea.city, ea.region, ea.postal_code, ea.country,
               ea.lat, ea.lon, ea.source, ea.confidence,
               e.canonical_name AS via_entity_name,
               e.canonical_type AS via_entity_type
          FROM entity_addresses ea
          JOIN entities e ON e.entity_id = ea.entity_id
         WHERE ea.entity_id = ANY(%s)
         ORDER BY ea.confidence DESC NULLS LAST
         LIMIT 200
    """, (chain_ids,))
    cols = [d[0] for d in cur.description]
    rows = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        for k in ("lat", "lon", "confidence"):
            if d.get(k) is not None:
                try:
                    d[k] = float(d[k])
                except (TypeError, ValueError):
                    pass
        rows.append(d)
    dossier["extended_addresses"] = rows

    # Pull every asset (aircraft / vessel / facility) registered to a chain entity
    cur.execute("""
        SELECT er.relationship, er.confidence,
               e2.entity_id AS asset_entity_id,
               e2.canonical_name AS asset_name,
               e2.canonical_type AS asset_type,
               e2.identifiers AS asset_identifiers,
               e1.entity_id AS via_entity_id,
               e1.canonical_name AS via_entity_name
          FROM entity_relationships er
          JOIN entities e1 ON e1.entity_id = er.source_entity_id
          JOIN entities e2 ON e2.entity_id = er.target_entity_id
         WHERE er.tenant_id = %s
           AND er.source_entity_id = ANY(%s)
           AND er.relationship IN ('owns','registered_to','controls','held_by')
           AND e2.canonical_type IN ('aircraft','vessel','facility','vehicle','spacecraft')
         ORDER BY er.confidence DESC NULLS LAST
         LIMIT 200
    """, (tenant_id, chain_ids))
    cols = [d[0] for d in cur.description]
    asset_rows = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        if d.get("confidence") is not None:
            try:
                d["confidence"] = float(d["confidence"])
            except (TypeError, ValueError):
                pass
        asset_rows.append(d)
    dossier["extended_assets"] = asset_rows


def _add_dossier_section(dossier: dict, key: str, cur, sql: str, params: tuple) -> None:
    """Run a query and append rows to dossier[key]. Best-effort — log + skip
    on failure so one missing table doesn't blank the whole card."""
    try:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = []
        for r in cur.fetchall():
            row = dict(zip(cols, r))
            for tk in ("issue_date", "expiry_date", "created_at", "captured_at",
                       "added_at", "detected_at", "split_at", "flagged_at"):
                if row.get(tk) and hasattr(row[tk], "isoformat"):
                    row[tk] = row[tk].isoformat()
            rows.append(row)
        dossier[key] = rows
    except Exception as e:
        log.warning("dossier section %s failed: %s", key, e)


# ── Phase 7e: per-finding verify/reject + entity split endpoints ──────────

@router.post("/{node_id}/findings/{finding_id}/attribute")
async def attribute_finding(
    node_id: str,
    finding_id: int,
    body: dict = Body(...),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Mark an investigation_finding as confirmed | rejected | reassigned.

    Body:
      decision: "confirmed" | "rejected" | "reassigned"  (required)
      reviewer_note: free text (optional)
      reassign_to_entity_id: int (required iff decision == "reassigned")

    Phase 7e of research-validation plan. Overrides the LLM relevance score
    so user judgment wins. The decision is upserted on (tenant, finding_id)
    so the user can change their mind without creating duplicate rows.
    """
    decision = (body.get("decision") or "").lower()
    if decision not in ("confirmed", "rejected", "reassigned"):
        raise HTTPException(status_code=400,
            detail="decision must be one of: confirmed, rejected, reassigned")

    note = body.get("reviewer_note") or None
    reassign_to = body.get("reassign_to_entity_id")
    if decision == "reassigned" and not reassign_to:
        raise HTTPException(status_code=400,
            detail="reassign_to_entity_id required when decision='reassigned'")

    reviewer = (auth.identity_id if hasattr(auth, "identity_id") else None) or auth.tenant_id
    from storage.postgres.db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO finding_attributions
                    (tenant_id, finding_id, entity_id, decision, reviewer, reviewer_note)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, finding_id) DO UPDATE
                  SET decision = EXCLUDED.decision,
                      entity_id = EXCLUDED.entity_id,
                      reviewer = EXCLUDED.reviewer,
                      reviewer_note = EXCLUDED.reviewer_note,
                      created_at = now()
                RETURNING attribution_id
                """,
                (auth.tenant_id, finding_id,
                 reassign_to if decision == "reassigned" else None,
                 decision, reviewer, note),
            )
            row = cur.fetchone()
        conn.commit()
    return {"status": "ok", "attribution_id": row[0] if row else None,
            "decision": decision}


@router.post("/{node_id}/split")
async def split_entity(
    node_id: str,
    body: dict = Body(...),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Split this entity into a sibling entity, reassigning rejected findings.

    Body:
      new_entity_name: str (required)
      new_entity_type: str (default = same as parent)
      finding_ids: list[int] (required, the findings to move to new entity)
      reason: str (optional)
      cluster: dict (optional) — when present, the split was triggered
        from the Persona Clustering panel. Carries the cluster's binding
        evidence so the base-60 substrate (form/concept/binding per
        migration 093) is also updated alongside the entity-level split.
        Schema (from core.workers.persona_clustering.PersonaCluster):
          {
            cluster_id, confidence, canonical_subtype, subtype_path,
            identifiers, dominant_attrs, evidence_tier
          }

    Phase 7e (disambiguation closer) + Phase 1 of persona clustering
    (base-60 binding writes).

    Worked example: "David Harlow" investigation has 5 court cases; 1 is
    really Acme Widgets CEO and 4 are about a bankruptcy filer with
    the same name. User checks ✗ on the 4, then hits SPLIT — those 4
    findings get reassigned to a new entity, AND the form "David Harlow"
    now has two form_concept_bindings: one to the CEO concept, one to
    the other filer concept. Future ingestion of "David Harlow"
    consults that distribution instead of falling back to entity-as-form.

    Both entities exist after split (no destructive op). Audited in
    entity_splits; finding-level reassignment via finding_attributions;
    substrate via concept_nodes + form_concept_bindings.
    """
    new_name = (body.get("new_entity_name") or "").strip()
    finding_ids = body.get("finding_ids") or []
    if not new_name:
        raise HTTPException(status_code=400, detail="new_entity_name required")
    if not isinstance(finding_ids, list) or not finding_ids:
        raise HTTPException(status_code=400, detail="finding_ids (non-empty list) required")
    reason = body.get("reason") or "manual disambiguation"
    cluster = body.get("cluster") if isinstance(body.get("cluster"), dict) else None

    from storage.postgres.db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Resolve parent entity from node_id (entity:NNN convention)
            cur.execute(
                "SELECT master_entity_id FROM knowledge_nodes "
                "WHERE tenant_id=%s AND node_id=%s",
                (auth.tenant_id, node_id),
            )
            r = cur.fetchone()
            if not r or not r[0]:
                raise HTTPException(status_code=404, detail="parent entity not found")
            parent_entity_id = r[0]

            cur.execute(
                "SELECT canonical_name, canonical_type FROM entities WHERE entity_id=%s",
                (parent_entity_id,),
            )
            parent_row = cur.fetchone()
            parent_canonical_name = (parent_row[0] if parent_row else "") or ""
            parent_type = parent_row[1] if parent_row else None
            new_type = (body.get("new_entity_type")
                        or (cluster.get("canonical_subtype") if cluster else None)
                        or parent_type
                        or "person")

        # Admit the new entity through the gateway so it gets a base60
        # address + lineage stamp like everything else. The cluster's
        # dominant_attrs are the binding evidence; pass them as
        # attribute_hints so Layer 0 routing can pick them up on future
        # admissions of this same surface form.
        try:
            from core.gdu.gateway import get_gateway
            gw = get_gateway()
            split_idents: dict = {"split_from_entity_id": str(parent_entity_id)}
            if cluster and isinstance(cluster.get("identifiers"), dict):
                split_idents.update({k: str(v) for k, v in cluster["identifiers"].items() if v})
            adm = gw.admit_entity(
                auth.tenant_id, new_name, new_type,
                signal_rid=0,
                identifiers=split_idents,
                attribute_hints=(cluster.get("dominant_attrs") if cluster else None),
            )
            if not adm or not adm.entity_id:
                raise HTTPException(status_code=500, detail="gateway refused admission for new entity")
            new_entity_id = adm.entity_id
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"new entity admission failed: {e}")

        substrate_result = None
        with conn.cursor() as cur:
            # Reassign each finding via finding_attributions
            reviewer = (auth.identity_id if hasattr(auth, "identity_id") else None) or auth.tenant_id
            for fid in finding_ids:
                cur.execute(
                    """
                    INSERT INTO finding_attributions
                        (tenant_id, finding_id, entity_id, decision, reviewer, reviewer_note)
                    VALUES (%s, %s, %s, 'reassigned', %s, %s)
                    ON CONFLICT (tenant_id, finding_id) DO UPDATE
                      SET decision='reassigned',
                          entity_id=EXCLUDED.entity_id,
                          reviewer=EXCLUDED.reviewer,
                          reviewer_note=EXCLUDED.reviewer_note,
                          created_at=now()
                    """,
                    (auth.tenant_id, int(fid), new_entity_id, reviewer,
                     f"split from entity {parent_entity_id} → {new_entity_id}"),
                )

            # Audit row
            cur.execute(
                """
                INSERT INTO entity_splits
                    (tenant_id, parent_entity_id, new_entity_id,
                     split_by, reason, reassigned_findings)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING split_id
                """,
                (auth.tenant_id, parent_entity_id, new_entity_id,
                 reviewer, reason, finding_ids),
            )
            split_id = cur.fetchone()[0]

            # Base-60 substrate write: form_node (Sam Rowan) →
            # concept_node (new persona) → form_concept_binding. The
            # parent entity's existing concept is also rebound to the
            # form so future ingestion of this surface form consults the
            # full distribution. Failures here are logged but don't
            # abort the split — the audit row + finding reassignment
            # carry the operator's intent; substrate can be backfilled.
            if parent_canonical_name:
                try:
                    from core.ldu.admit_candidate import write_persona_split_substrate
                    subtype_path = (cluster.get("subtype_path") if cluster else None) \
                        or f"entity.{new_type}"
                    substrate_result = write_persona_split_substrate(
                        cur=cur,
                        tenant_id=auth.tenant_id,
                        form_surface=parent_canonical_name,
                        new_entity_id=new_entity_id,
                        new_canonical_name=new_name,
                        canonical_subtype=new_type,
                        subtype_path=subtype_path,
                        identifiers=(cluster.get("identifiers") if cluster else None) or {},
                        binding_evidence=(cluster.get("dominant_attrs") if cluster else None) or {},
                        binding_source=("persona_clustering_v1" if cluster
                                        else "manual_split"),
                        evidence_tier=(cluster.get("evidence_tier") if cluster else None) or "D",
                        binding_confidence=float((cluster.get("confidence") if cluster else 0.85) or 0.85),
                    )
                except Exception as e:
                    import logging
                    logging.getLogger("enki.split").warning(
                        "persona-split substrate write failed (split still committed): %s", e)

        conn.commit()

    out = {
        "status": "ok",
        "split_id": split_id,
        "parent_entity_id": parent_entity_id,
        "new_entity_id": new_entity_id,
        "new_entity_node_id": f"entity:{new_entity_id}",
        "reassigned_count": len(finding_ids),
    }
    if substrate_result:
        out["substrate"] = substrate_result
    return out


@router.get("/map-assets")
async def get_entity_map_assets(
    auth: UnifiedAuthInfo = Depends(unified_auth),
    limit: int = Query(2000, le=10000),
    address_types: Optional[str] = Query(None,
        description="Comma-separated address_type filter (hq,registered,asset_location,...)"),
):
    """Phase 8b.6.5 — GeoJSON FeatureCollection of every entity address
    with lat/lon, for the map's `entity_assets` layer. Each Feature carries
    entity_id + canonical_name + canonical_type + address_type so the map
    can color-code (HQ vs aircraft vs property) and link clicks back to
    the entity dossier.
    """
    from storage.postgres.db import get_connection

    tenant_id = auth.tenant_id
    types_filter = None
    if address_types:
        types_filter = [t.strip() for t in address_types.split(",") if t.strip()]

    features = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                params: list = [tenant_id]
                where_extra = ""
                if types_filter:
                    where_extra = " AND ea.address_type = ANY(%s)"
                    params.append(types_filter)
                params.append(limit)
                cur.execute(f"""
                    SELECT ea.address_id, ea.entity_id, ea.address_type,
                           ea.raw_address, ea.lat, ea.lon, ea.source,
                           e.canonical_name, e.canonical_type
                      FROM entity_addresses ea
                      JOIN entities e ON e.entity_id = ea.entity_id
                     WHERE ea.tenant_id = %s
                       AND ea.lat IS NOT NULL AND ea.lon IS NOT NULL
                       {where_extra}
                     ORDER BY ea.confidence DESC NULLS LAST
                     LIMIT %s
                """, tuple(params))
                for (aid, eid, atype, raw, lat, lon, src,
                     name, ctype) in cur.fetchall():
                    features.append({
                        "type": "Feature",
                        "id": aid,
                        "geometry": {
                            "type": "Point",
                            "coordinates": [float(lon), float(lat)],
                        },
                        "properties": {
                            "address_id": aid,
                            "entity_id": eid,
                            "node_id": f"entity:{eid}",
                            "entity_name": name,
                            "entity_type": ctype,
                            "address_type": atype,
                            "raw_address": raw,
                            "source": src,
                        },
                    })
    except Exception as e:
        log.warning("/nodes/map-assets failed: %s", e)
        # Best-effort: return empty collection rather than 500
    return {"type": "FeatureCollection", "features": features}


@router.get("/{node_id}/sources")
async def get_node_sources(
    node_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
    limit: int = Query(50, le=500),
    offset: int = Query(0, ge=0),
):
    """Paginated source stack for a node. Returns every document (article,
    feed entry, telegram post, etc.) that has a `mentions` edge pointing at
    this node, with outlet / URL / publish time for each. This is the list
    behind the collapsed "523 articles across 47 outlets" chip on a card."""
    from storage.postgres.db import get_connection

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT se.node_id,
                           se.envelope->'identity'->>'title_display'  AS title,
                           se.source_outlet                           AS outlet,
                           se.source_url                              AS url,
                           se.event_time,
                           se.signal_rid,
                           ke.strength
                      FROM knowledge_edges ke
                      JOIN signal_extractions se ON se.node_id = ke.source_node_id
                     WHERE ke.target_node_id = %s
                       AND ke.relationship  = 'mentions'
                       AND se.tenant_id     = %s
                     ORDER BY se.event_time DESC NULLS LAST, se.signal_rid DESC
                     LIMIT %s OFFSET %s
                    """,
                    (node_id, auth.tenant_id, limit, offset),
                )
                cols = [d[0] for d in cur.description]
                sources = [dict(zip(cols, r)) for r in cur.fetchall()]

                cur.execute(
                    """
                    SELECT COUNT(*) AS total,
                           COUNT(DISTINCT se.source_outlet) AS outlet_count
                      FROM knowledge_edges ke
                      JOIN signal_extractions se ON se.node_id = ke.source_node_id
                     WHERE ke.target_node_id = %s
                       AND ke.relationship  = 'mentions'
                       AND se.tenant_id     = %s
                    """,
                    (node_id, auth.tenant_id),
                )
                summary = cur.fetchone()
    except Exception as e:
        log.exception("get_node_sources failed")
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "sources": sources,
        "total":        int(summary[0] or 0),
        "outlet_count": int(summary[1] or 0),
        "limit": limit,
        "offset": offset,
    }


@router.get("/{node_id}/trace")
async def get_node_trace(
    node_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Full provenance trace for a knowledge node.

    Read-only audit view showing the complete chain from raw signal to
    finished intelligence product. Assembles data from signals,
    signal_extractions, extraction_passes, research_passes, and
    knowledge_edges — no new writes, no security surface.

    Returns the governance chain: source signal → extraction passes →
    entity resolution → edges created → research passes → conflicts.
    """
    from storage.postgres.db import get_connection
    import json as _json

    trace: dict = {
        "node_id": node_id,
        "governance": {},
        "source_signals": [],
        "extraction_passes": [],
        "entities_resolved": [],
        "edges": [],
        "research_passes": [],
        "conflicts": [],
    }

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # 1. Get the node envelope for governance stamps
                cur.execute("""
                    SELECT envelope, completeness_score, completeness_tier,
                           created_at, updated_at
                    FROM knowledge_nodes
                    WHERE node_id = %s AND tenant_id = %s
                """, (node_id, auth.tenant_id))
                node_row = cur.fetchone()

                if not node_row:
                    # Try signal_extractions (articles live here)
                    cur.execute("""
                        SELECT envelope, completeness_score, completeness_tier,
                               created_at, updated_at
                        FROM signal_extractions
                        WHERE node_id = %s AND tenant_id = %s
                        LIMIT 1
                    """, (node_id, auth.tenant_id))
                    node_row = cur.fetchone()

                if node_row:
                    env = node_row[0] if isinstance(node_row[0], dict) else _json.loads(node_row[0] or "{}")
                    gov = env.get("governance", {})
                    prov = env.get("provenance", {})
                    trace["governance"] = {
                        "gdu_canonical": gov.get("gdu_canonical"),
                        "active_set_version": gov.get("active_set_version"),
                        "pack_versions": gov.get("pack_versions", {}),
                        "ldu_t_id": gov.get("ldu_t_id"),
                        "admitted_at": gov.get("admitted_at"),
                        "completeness_score": node_row[1],
                        "completeness_tier": node_row[2],
                        "created_at": node_row[3].isoformat() if node_row[3] else None,
                        "updated_at": node_row[4].isoformat() if node_row[4] else None,
                    }
                    # In-envelope extraction passes
                    trace["extraction_passes"] = prov.get("extraction_passes", [])

                # 2. Find source signals (via source_signals array or signal_rid)
                cur.execute("""
                    SELECT s.signal_id, s.signal_type, s.device_id,
                           s.source_domain, s.timestamp, s.ldu_t_id, s.cid,
                           s.origin_node_id, LENGTH(s.payload::text) as payload_size,
                           s.payload->>'title' as title,
                           s.payload->>'source' as source_name,
                           se.extraction_id, se.llm_processed,
                           se.admitted_under_active_set, se.gdu_canonical
                    FROM signal_extractions se
                    JOIN signals s ON s.rid = se.signal_rid
                    WHERE se.node_id = %s AND se.tenant_id = %s
                    ORDER BY s.timestamp DESC
                    LIMIT 20
                """, (node_id, auth.tenant_id))
                for r in cur.fetchall():
                    trace["source_signals"].append({
                        "signal_id": r[0],
                        "signal_type": r[1],
                        "device_id": r[2],
                        "source_domain": r[3],
                        "timestamp": r[4].isoformat() if r[4] else None,
                        "ldu_t_id": r[5],
                        "chain_id": r[6],
                        "origin_node_id": r[7],
                        "payload_size": r[8],
                        "title": r[9],
                        "source_name": r[10],
                        "extraction_id": r[11],
                        "llm_processed": r[12],
                        "admitted_under_active_set": r[13],
                        "gdu_canonical": r[14],
                    })

                # 3. Extraction passes from the DB table
                signal_rids = [s["extraction_id"] for s in trace["source_signals"] if s.get("extraction_id")]
                if signal_rids:
                    # Get extraction_passes linked to these signals
                    cur.execute("""
                        SELECT ep.pass_id, ep.stage, ep.model_name, ep.model_version,
                               ep.prompt_version, ep.fields_filled, ep.duration_ms,
                               ep.success, ep.error,
                               ep.admitted_under_active_set, ep.gdu_canonical,
                               ep.created_at
                        FROM extraction_passes ep
                        WHERE ep.signal_rid IN (
                            SELECT signal_rid FROM signal_extractions
                            WHERE node_id = %s AND tenant_id = %s
                        )
                        ORDER BY ep.created_at ASC
                        LIMIT 50
                    """, (node_id, auth.tenant_id))
                    for r in cur.fetchall():
                        trace["extraction_passes"].append({
                            "pass_id": r[0],
                            "stage": r[1],
                            "model": r[2],
                            "model_version": r[3],
                            "prompt_version": r[4],
                            "fields_filled": r[5] or [],
                            "duration_ms": r[6],
                            "success": r[7],
                            "error": r[8],
                            "active_set": r[9],
                            "gdu_canonical": r[10],
                            "timestamp": r[11].isoformat() if r[11] else None,
                        })

                # 4. Resolved entities linked to this node
                cur.execute("""
                    SELECT ke.target_node_id, ke.relationship, ke.strength,
                           ke.evidence, ke.ai_generated
                    FROM knowledge_edges ke
                    WHERE ke.source_node_id = %s
                      AND ke.relationship = 'mentions'
                    LIMIT 50
                """, (node_id,))
                for r in cur.fetchall():
                    trace["entities_resolved"].append({
                        "entity_node_id": r[0],
                        "relationship": r[1],
                        "strength": r[2],
                        "evidence": r[3],
                        "ai_generated": r[4],
                    })

                # Also get edges pointing TO this node
                cur.execute("""
                    SELECT ke.source_node_id, ke.relationship, ke.strength,
                           ke.evidence, ke.ai_generated
                    FROM knowledge_edges ke
                    WHERE ke.target_node_id = %s
                    LIMIT 50
                """, (node_id,))
                for r in cur.fetchall():
                    trace["edges"].append({
                        "source_node_id": r[0],
                        "relationship": r[1],
                        "strength": r[2],
                        "evidence": r[3],
                        "ai_generated": r[4],
                    })

                # 5. Research passes that touched this node
                cur.execute("""
                    SELECT pass_id, started_at, completed_at,
                           extractions_considered, extraction_ids,
                           fields_updated, entities_added, places_added,
                           topics_added, contradictions_flagged,
                           llm_model, llm_duration_ms, llm_prompt_version,
                           success, error, new_completeness,
                           admitted_under_active_set, gdu_canonical
                    FROM research_passes
                    WHERE node_id = %s
                    ORDER BY started_at DESC
                    LIMIT 20
                """, (node_id,))
                for r in cur.fetchall():
                    trace["research_passes"].append({
                        "pass_id": r[0],
                        "started_at": r[1].isoformat() if r[1] else None,
                        "completed_at": r[2].isoformat() if r[2] else None,
                        "extractions_considered": r[3],
                        "extraction_ids": r[4] or [],
                        "fields_updated": r[5] or [],
                        "entities_added": r[6],
                        "places_added": r[7],
                        "topics_added": r[8],
                        "contradictions_flagged": r[9],
                        "llm_model": r[10],
                        "llm_duration_ms": r[11],
                        "llm_prompt_version": r[12],
                        "success": r[13],
                        "error": r[14],
                        "new_completeness": r[15],
                        "active_set": r[16],
                        "gdu_canonical": r[17],
                    })

                # 6. Conflicts on this node
                try:
                    cur.execute("""
                        SELECT conflict_id, field_path, field_kind,
                               value_a, value_b, sources_a, sources_b,
                               severity, resolution, detected_at, resolved_at
                        FROM node_conflicts
                        WHERE node_id = %s
                        ORDER BY detected_at DESC
                        LIMIT 20
                    """, (node_id,))
                    for r in cur.fetchall():
                        trace["conflicts"].append({
                            "conflict_id": r[0],
                            "field": r[1],
                            "kind": r[2],
                            "value_a": r[3],
                            "value_b": r[4],
                            "sources_a": r[5] or [],
                            "sources_b": r[6] or [],
                            "severity": r[7],
                            "resolution": r[8],
                            "detected_at": r[9].isoformat() if r[9] else None,
                            "resolved_at": r[10].isoformat() if r[10] else None,
                        })
                except Exception:
                    pass  # node_conflicts table may not exist

    except Exception as e:
        log.exception("get_node_trace failed for %s", node_id)
        raise HTTPException(status_code=500, detail=str(e))

    trace["summary"] = {
        "source_signal_count": len(trace["source_signals"]),
        "extraction_pass_count": len(trace["extraction_passes"]),
        "entity_count": len(trace["entities_resolved"]),
        "edge_count": len(trace["edges"]),
        "research_pass_count": len(trace["research_passes"]),
        "conflict_count": len(trace["conflicts"]),
    }

    return trace


@router.patch("/{node_id}")
async def update_node(
    node_id: str,
    body: dict = Body(...),
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Update title, narrative, or research_enabled on an existing node."""
    from storage.postgres.db import get_connection

    updates = []
    params: list = []
    if "title" in body:
        updates.append("envelope = jsonb_set(envelope, '{identity,title_display}', to_jsonb(%s::text))")
        params.append(body["title"])
    if "narrative" in body:
        updates.append("envelope = jsonb_set(envelope, '{summary,narrative}', to_jsonb(%s::text))")
        params.append(body["narrative"])
    if "research_enabled" in body:
        updates.append("research_enabled = %s")
        params.append(bool(body["research_enabled"]))

    if not updates:
        raise HTTPException(status_code=400, detail="no fields to update")

    params.extend([auth.tenant_id, node_id])
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE knowledge_nodes
                       SET {', '.join(updates)},
                           updated_at = NOW()
                     WHERE tenant_id = %s AND node_id = %s
                     RETURNING node_id
                    """,
                    params,
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="not found")
            conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        log.exception("update_node failed")
        raise HTTPException(status_code=500, detail=str(e))

    return {"status": "updated", "node_id": node_id}


@router.post("/{node_id}/research")
async def trigger_research(
    node_id: str,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Research an entity using Wikipedia, Wikidata, SEC, OpenSanctions.

    Calls external APIs directly and stores results in the entity's
    envelope + entity_events for provenance. Returns the enriched profile.
    """
    from storage.postgres.db import get_connection

    # 1. Get the node's current data
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT node_id, title, node_type, subtype, envelope,
                       master_entity_id, entity_name
                FROM knowledge_nodes WHERE node_id = %s AND tenant_id = %s
            """, (node_id, auth.tenant_id))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Node not found")

            _, title, node_type, subtype, envelope, entity_id, entity_name = row
            name = entity_name or title or node_id

    # 2. Call research sources based on entity type
    profile: dict = {}
    sources_used: list[str] = []

    try:
        from core.research.sources import (
            wikipedia_summary, wikidata_entity,
            sanctions_check, sec_company_search,
        )

        # Wikipedia
        wiki = wikipedia_summary(name)
        if wiki:
            profile["description"] = wiki.get("extract", "")
            profile["thumbnail"] = wiki.get("thumbnail")
            profile["wikipedia_url"] = wiki.get("page_url")
            wikidata_id = wiki.get("wikidata_id")
            sources_used.append("Wikipedia")

            # Wikidata (if Wikipedia gave us a QID)
            if wikidata_id:
                wd = wikidata_entity(wikidata_id)
                if wd:
                    profile["wikidata_id"] = wikidata_id
                    for key in ["date_of_birth", "date_of_death", "nationality",
                                "occupation", "position_held", "employer",
                                "educated_at", "twitter_handle", "official_website",
                                "population", "area_km2", "head_of_state",
                                "head_of_government", "capital", "currency",
                                "official_language", "continent", "country"]:
                        if wd.get(key):
                            profile[key] = wd[key]
                    sources_used.append("Wikidata")

        # Sanctions check (for people and orgs)
        if subtype in ("person", "organization", "military_unit", "government_agency"):
            try:
                sanc = sanctions_check(name)
                if sanc:
                    profile["sanctions_match"] = sanc.get("match", False)
                    profile["sanctions_score"] = sanc.get("score", 0)
                    profile["sanctions_results"] = sanc.get("results", [])[:3]
                    sources_used.append("OpenSanctions")
            except Exception:
                pass

        # SEC (for organizations)
        if subtype in ("organization", "company"):
            try:
                sec = sec_company_search(name)
                if sec:
                    profile["sec_cik"] = sec.get("cik")
                    profile["sec_ticker"] = sec.get("ticker")
                    profile["sec_name"] = sec.get("name")
                    sources_used.append("SEC EDGAR")
            except Exception:
                pass

    except ImportError as e:
        log.warning("Research sources unavailable: %s", e)
        return {"status": "error", "detail": f"Research sources unavailable: {e}"}

    # 3. Store results in envelope + update entity
    if profile:
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    # Update envelope with profile data
                    envelope = envelope or {}
                    envelope["profile"] = profile
                    envelope["research"] = {
                        "sources_used": sources_used,
                        "researched_at": datetime.now(timezone.utc).isoformat(),
                    }

                    # Update validation status
                    if profile.get("wikidata_id"):
                        envelope["validation"] = {
                            "status": "validated",
                            "source": "Wikidata",
                            "source_id": profile["wikidata_id"],
                            "source_url": f"https://www.wikidata.org/wiki/{profile['wikidata_id']}",
                            "confidence": 0.9,
                        }

                    cur.execute("""
                        UPDATE knowledge_nodes
                        SET envelope = %s,
                            description = COALESCE(NULLIF(%s, ''), description),
                            updated_at = NOW()
                        WHERE node_id = %s AND tenant_id = %s
                    """, (
                        json.dumps(envelope),
                        profile.get("description", "")[:500],
                        node_id, auth.tenant_id,
                    ))

                    # Also update the entity record if it exists
                    if entity_id:
                        # completeness_score/_tier deliberately not written — see
                        # core/gdu/completeness.py. Note the tier expression here
                        # was `GREATEST(completeness_tier, 'partial')`, a STRING
                        # comparison: alphabetically 'stub' > 'solid' > 'partial',
                        # so it promoted a solid entity to 'stub'. The governed
                        # ladder maps score to tier and is the only thing that
                        # should decide it.
                        cur.execute("""
                            UPDATE entities
                            SET envelope = %s,
                                updated_at = NOW()
                            WHERE entity_id = %s AND tenant_id = %s
                        """, (
                            json.dumps({"profile": profile}),
                            entity_id, auth.tenant_id,
                        ))

                conn.commit()
        except Exception as e:
            log.warning("Failed to store research results: %s", e)

    return {
        "status": "ok",
        "entity": name,
        "profile": profile,
        "sources_used": sources_used,
        "fields_found": len([v for v in profile.values() if v]),
    }
