"""
Auto-node hook — turn a populated envelope into a knowledge_nodes row.

Phase 5 of the extraction pipeline. After Phase 3 resolution has stamped
node_ids on entities/places/topics, the auto-node hook persists the
envelope as a knowledge_nodes row so the existing UI (signals/cards/feed)
can render it.

Behavior:
    - Insert one row per envelope, keyed by envelope.node_id
      (deterministic, set by the worker → idempotent re-runs)
    - Populate BOTH legacy columns (title, description, concepts,
      latitude/longitude, images, epistemic_type, source_authority)
      AND envelope columns (envelope JSONB, node_type, subtype,
      completeness_*, master_*_id, LDU lineage)
    - ON CONFLICT (node_id) DO UPDATE refreshes content + bumps
      updated_at; the row is the authoritative current state of the
      envelope at the time of the most recent extraction
    - LDU lineage is stamped on every insert/update so we can audit
      which active_set wrote which fields
    - Failures are logged and swallowed — auto-node is best-effort, the
      extraction pipeline never breaks because persistence failed

The hook does NOT:
    - Create knowledge_edges (Phase 5b — edge extraction)
    - Touch the master tables (that's Phase 3 resolvers)
    - Compute completeness scores (Phase 8)
    - Trigger card rendering (Phase 9)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from core.extraction.envelope import get_path
from core.ldu.lineage import get_lineage_stamp
from core.gdu.gateway import get_gateway
from core.gdu.pack_registry import is_valid_node_type

log = logging.getLogger("enki.extraction.auto_node")


def _extract_master_ids(envelope: dict) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """Pick the most-mentioned entity/place/topic from the envelope and
    return their numeric master ids (parsed from 'entity:42' style
    node_ids set by the resolvers). These become the master_*_id FK
    columns on the knowledge_nodes row."""
    def _first_id(items: list, prefix: str) -> Optional[int]:
        for it in items or []:
            nid = it.get("node_id")
            if isinstance(nid, str) and nid.startswith(prefix):
                try:
                    return int(nid.split(":", 1)[1])
                except (ValueError, IndexError):
                    continue
        return None

    entity_id = _first_id(envelope.get("entities") or [], "entity:")
    topic_id = _first_id(envelope.get("topics") or [], "topic:")

    # Post-unification (migration 118): places are entities. resolved_places
    # carries 'entity:NNN' strings (was 'place:NNN' pre-unification). If
    # no entity_id was picked up from envelope.entities, fall back to the
    # first resolved place — they're entities too.
    if entity_id is None:
        for nid in (envelope.get("spatial") or {}).get("resolved_places") or []:
            if isinstance(nid, str) and nid.startswith("entity:"):
                try:
                    entity_id = int(nid.split(":", 1)[1])
                    break
                except (ValueError, IndexError):
                    continue

    return entity_id, topic_id


def _envelope_to_legacy(envelope: dict) -> dict:
    """Project the envelope onto the legacy knowledge_nodes columns the
    existing UI reads. Generic — works on any node_type."""
    coords = (envelope.get("spatial") or {}).get("coordinates") or {}
    lat = coords.get("lat") if isinstance(coords, dict) else None
    lon = coords.get("lon") if isinstance(coords, dict) else None

    media = envelope.get("media") or {}
    images: list[dict] = []
    if media.get("thumbnail"):
        images.append({"url": media["thumbnail"], "source": media.get("thumbnail_source") or "feed"})

    concepts = [
        t.get("label")
        for t in (envelope.get("topics") or [])
        if t.get("label")
    ]

    # Some legacy columns are varchar(20). Truncate to avoid hard failures
    # when an outlet name like "Enki Chronicle Worker" (21 chars) hits
    # source_authority. Real-data outlets like "BBC News" / "Al Jazeera"
    # fit comfortably; this only catches edge cases.
    raw_outlet = get_path(envelope, "source.outlet_name") or None
    source_authority = (raw_outlet[:20] if raw_outlet else None)
    raw_eptype = get_path(envelope, "evidence.epistemic_type") or "observation"
    epistemic_type = raw_eptype[:20]

    return {
        "title": (get_path(envelope, "identity.title_display") or "")[:500] or "Untitled",
        "description": get_path(envelope, "summary.narrative") or "",
        "epistemic_type": epistemic_type,
        "concepts": concepts,
        "source_authority": source_authority,
        "latitude": lat,
        "longitude": lon,
        "images": images,
    }


def upsert_knowledge_node(
    envelope: dict,
    conn,
    *,
    tenant_id: str = "default",
    project_id: str = "default",
    signal_rid: Optional[int] = None,
) -> dict:
    """Insert-or-update one knowledge_nodes row from a populated envelope.

    Returns:
        {"status": "ok"|"skipped"|"error", "node_id": <str>, "action": "insert"|"update",
         "reason": <str or None>}
    """
    if conn is None:
        return {"status": "skipped", "reason": "no db connection"}

    node_id = envelope.get("node_id")
    if not node_id:
        return {"status": "skipped", "reason": "envelope has no node_id"}

    legacy = _envelope_to_legacy(envelope)
    entity_id, topic_id = _extract_master_ids(envelope)
    stamp = get_lineage_stamp(ldu_t_id=signal_rid)

    # ─── Governance write-time enforcement ─────────────────────────
    # In strict mode (ENKI_GDU_STRICT=1), refuse to INSERT rows that
    # would be stamped with gdu_canonical=false. This is the
    # complementary check to the boot-time hard stop — it stops
    # data writes too, even if the GDU context degrades mid-run.
    import os as _os
    if not stamp.get("gdu_canonical") and _os.getenv(
        "ENKI_GDU_STRICT", ""
    ).lower() in ("1", "true", "yes"):
        return {
            "status": "refused",
            "reason": "gdu_non_canonical_strict_mode",
            "active_set": stamp.get("active_set_version"),
        }

    # auto_node is the canonical writer for knowledge_nodes (events,
    # sources, documents, etc.). Skip gateway.admit_node entirely —
    # it would create a SECOND row at a different (hash-derived)
    # node_id with the same base60 address, violating the unique
    # constraint on base60_address.
    #
    # Get a fresh base60 from assign_address. The address sequence is
    # globally unique per (category, subtype) so this row's address
    # won't collide with any other.
    node_type = envelope.get("node_type") or "source"
    subtype = envelope.get("subtype")
    base60_addr = None
    base60_cat = None
    base60_sub = None
    if node_type and subtype and is_valid_node_type(node_type, subtype):
        try:
            from core.gdu.address import assign_address
            from core.gdu.pack_registry import get_category_index, get_subtype_index
            base60_addr = assign_address(conn, node_type, subtype)
            ci = get_category_index(node_type)
            si = get_subtype_index(node_type, subtype)
            base60_cat = ci if ci >= 0 else None
            base60_sub = si if si >= 0 else None
        except Exception as _e:
            log.debug("assign_address failed for %s: %s", node_id, _e)

    # location is a PostGIS POINT (geography). Build inline since psycopg2
    # cannot infer it from a Python dict.
    location_sql = "NULL"
    location_params: list[Any] = []
    if legacy["latitude"] is not None and legacy["longitude"] is not None:
        # knowledge_nodes.location is geometry(POINT, 4326), not geography
        location_sql = "ST_SetSRID(ST_MakePoint(%s, %s), 4326)"
        location_params = [float(legacy["longitude"]), float(legacy["latitude"])]

    sql = f"""
        INSERT INTO knowledge_nodes (
            node_id, project_id, tenant_id, signal_rid,
            title, description, epistemic_type, concepts,
            source_authority, ai_enriched, depth,
            latitude, longitude, location, images,
            envelope, envelope_version, node_type, subtype,
            completeness_score, completeness_tier,
            master_entity_id, master_topic_id,
            base60_address, base60_category, base60_subtype,
            admitted_under_active_set,
            admitted_under_pack_versions,
            gdu_canonical, ldu_t_id
        ) VALUES (
            %(node_id)s, %(project_id)s, %(tenant_id)s, %(signal_rid)s,
            %(title)s, %(description)s, %(epistemic_type)s, %(concepts)s,
            %(source_authority)s, %(ai_enriched)s, %(depth)s,
            %(latitude)s, %(longitude)s, {location_sql}, %(images)s::jsonb,
            %(envelope)s::jsonb, %(envelope_version)s, %(node_type)s, %(subtype)s,
            %(completeness_score)s, %(completeness_tier)s,
            %(entity_id)s, %(topic_id)s,
            %(base60_address)s, %(base60_category)s, %(base60_subtype)s,
            %(active_set)s,
            %(pack_versions)s::jsonb,
            %(canonical_flag)s, %(ldu_t)s
        )
        ON CONFLICT (node_id) DO UPDATE SET
            title              = EXCLUDED.title,
            description        = EXCLUDED.description,
            epistemic_type     = EXCLUDED.epistemic_type,
            concepts           = EXCLUDED.concepts,
            source_authority   = EXCLUDED.source_authority,
            latitude           = EXCLUDED.latitude,
            longitude          = EXCLUDED.longitude,
            location           = EXCLUDED.location,
            images             = EXCLUDED.images,
            envelope           = EXCLUDED.envelope,
            envelope_version   = EXCLUDED.envelope_version,
            node_type          = EXCLUDED.node_type,
            subtype            = EXCLUDED.subtype,
            completeness_score = EXCLUDED.completeness_score,
            completeness_tier  = EXCLUDED.completeness_tier,
            master_entity_id   = COALESCE(EXCLUDED.master_entity_id, knowledge_nodes.master_entity_id),
            master_topic_id    = COALESCE(EXCLUDED.master_topic_id,  knowledge_nodes.master_topic_id),
            base60_address     = COALESCE(knowledge_nodes.base60_address, EXCLUDED.base60_address),
            base60_category    = COALESCE(knowledge_nodes.base60_category, EXCLUDED.base60_category),
            base60_subtype     = COALESCE(knowledge_nodes.base60_subtype, EXCLUDED.base60_subtype),
            admitted_under_active_set    = EXCLUDED.admitted_under_active_set,
            admitted_under_pack_versions = EXCLUDED.admitted_under_pack_versions,
            gdu_canonical                = EXCLUDED.gdu_canonical,
            ldu_t_id                     = COALESCE(EXCLUDED.ldu_t_id, knowledge_nodes.ldu_t_id),
            updated_at                   = NOW()
        RETURNING (xmax = 0) AS inserted
    """
    params = {
        "node_id": node_id,
        "project_id": project_id,
        "tenant_id": tenant_id,
        "signal_rid": signal_rid,
        "title": legacy["title"],
        "description": legacy["description"],
        "epistemic_type": legacy["epistemic_type"],
        "concepts": legacy["concepts"],
        "source_authority": legacy["source_authority"],
        "ai_enriched": False,  # cheap pass only — Phase 4 flips this
        "depth": 0,
        "latitude": legacy["latitude"],
        "longitude": legacy["longitude"],
        "images": json.dumps(legacy["images"]),
        "envelope": json.dumps(envelope, default=str),
        "envelope_version": envelope.get("envelope_version") or "v1",
        "node_type": envelope.get("node_type") or "source",
        "subtype": envelope.get("subtype"),
        "completeness_score": float(get_path(envelope, "completeness.score") or 0.0),
        "completeness_tier": get_path(envelope, "completeness.tier") or "empty",
        "entity_id": entity_id,
        "topic_id": topic_id,
        "base60_address": base60_addr,
        "base60_category": base60_cat,
        "base60_subtype": base60_sub,
        "active_set": stamp.get("active_set_version"),
        "pack_versions": json.dumps(stamp.get("pack_versions") or {}),
        "canonical_flag": bool(stamp.get("gdu_canonical")),
        "ldu_t": signal_rid,
    }

    try:
        with conn.cursor() as cur:
            # Replace the location_sql placeholders with named params so we can
            # use a single dict-based execute. Easier path: replace inline.
            if location_params:
                full_sql = sql.replace(
                    location_sql,
                    "ST_SetSRID(ST_MakePoint(%(loc_lon)s, %(loc_lat)s), 4326)",
                )
                params["loc_lon"] = location_params[0]
                params["loc_lat"] = location_params[1]
            else:
                full_sql = sql
            cur.execute(full_sql, params)
            row = cur.fetchone()
            inserted = bool(row[0]) if row else False
        conn.commit()
        return {
            "status": "ok",
            "node_id": node_id,
            "action": "insert" if inserted else "update",
        }
    except Exception as e:
        log.warning("upsert_knowledge_node(%s) failed: %s", node_id, e)
        try:
            conn.rollback()
        except Exception:
            pass
        return {"status": "error", "node_id": node_id, "reason": str(e)}
