"""
Corporate research — an entity's COMPANIES with real public-record data.

From a person/org profile, surface the companies they own / run / founded, each
with its registered address, officers/board, filings, and identifiers (EIN/CIK) —
all sourced from the investigation engine (SEC, OpenCorporates, ProPublica, state
registries, USAspending…). Plus a map of the whole corporate footprint.

  GET  /entities/{id}/companies         — the companies + address/officers/filings
  POST /entities/{id}/research-companies — deep-investigate the entity + its orgs
  GET  /entities/{id}/corporate-geo      — GeoJSON of addresses (entity + children)
"""

from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo
from storage.postgres.db import get_connection

router = APIRouter(tags=["corporate"])

# Relationships that make a target a "company of" the entity (corporate only — not
# family/personal edges like parent_of/member_of).
ORG_RELS = ["owns", "owned_by", "founded_by", "officer_of", "director_of", "ceo_of",
            "chairperson_of", "subsidiary_of", "parent_org", "beneficial_owner_of",
            "operated_by", "registered_to", "associated_company", "associated_nonprofit",
            "treasurer_of", "federal_contractor", "associated_committee"]
OFFICER_RELS = ["officer_of", "director_of", "ceo_of", "chairperson_of", "founded_by",
                "beneficial_owner_of"]
_ORG_TYPES = ("organization", "company", "nonprofit", "committee")


def _companies_raw(cur, entity_id: int, tenant_id: str) -> list[dict]:
    """Orgs linked to the entity — from promoted relationship edges AND from
    investigation-discovered orgs (even before they're promoted to entity nodes, so
    the panel is useful immediately). Deduped by entity_id or normalized name.
    Each: {entity_id|None, name, relationship, source}."""
    seen: dict = {}

    def _add(eid, name, rel, source, detail=None):
        key = f"e{eid}" if eid else f"n{(name or '').strip().lower()}"
        if not key or key in seen:
            if eid and seen.get(key) and not seen[key]["entity_id"]:
                seen[key]["entity_id"] = int(eid)   # upgrade name→id if we learn it
            # Prefer the richest detail across duplicate discovered rows (a later
            # re-investigation may carry FEC designation/affiliation the first run
            # lacked). Replace when the incoming detail is strictly richer.
            if detail and seen.get(key):
                cur_det = seen[key].get("detail") or {}
                if len(detail) > len(cur_det) or (
                    "designation" in detail and "designation" not in cur_det):
                    seen[key]["detail"] = detail
            return
        seen[key] = {"entity_id": int(eid) if eid else None,
                     "name": name, "relationship": rel or "associated", "source": source,
                     "detail": detail or {}}

    # 1) promoted relationship edges (resolved orgs)
    cur.execute("""
        SELECT DISTINCT er.target_entity_id, e.canonical_name, er.relationship, er.source
          FROM entity_relationships er
          JOIN entities e ON e.entity_id = er.target_entity_id
         WHERE er.source_entity_id = %s AND er.tenant_id = %s
           AND er.relationship = ANY(%s) AND e.canonical_type = ANY(%s)
    """, (entity_id, tenant_id, ORG_RELS, list(_ORG_TYPES)))
    for eid, name, rel, src in cur.fetchall():
        _add(eid, name, rel, src or "graph")

    # 2) orgs discovered investigating this entity (resolved OR name-only).
    # DISTINCT ON keeps only the NEWEST discovered row per committee/org — a
    # re-investigation writes a fresh row, and the latest carries the current
    # FEC designation/affiliation. Without this, a stale earlier row can win the
    # in-Python dedup and mislabel a committee.
    cur.execute("""
        SELECT DISTINCT ON (COALESCE(idisc.entity_id::text, lower(idisc.name)))
               idisc.entity_id, idisc.name, idisc.relationship, idisc.source,
               idisc.relationship_detail
          FROM investigation_discovered idisc
          JOIN entity_investigations ei ON ei.investigation_id = idisc.investigation_id
         WHERE ei.entity_id = %s AND idisc.tenant_id = %s
           AND idisc.entity_type = ANY(%s) AND idisc.relationship = ANY(%s)
         ORDER BY COALESCE(idisc.entity_id::text, lower(idisc.name)),
                  idisc.created_at DESC NULLS LAST
    """, (entity_id, tenant_id, list(_ORG_TYPES), ORG_RELS))
    for eid, name, rel, src, det in cur.fetchall():
        _add(eid, name, rel, src or "investigation",
             detail=det if isinstance(det, dict) else {})
    return list(seen.values())


@router.get("/entities/{entity_id}/companies")
async def entity_companies(entity_id: int, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Companies linked to the entity, each with address, officers, filings, ids."""
    companies = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            for co in _companies_raw(cur, entity_id, auth.tenant_id)[:60]:
                cid, name, rel = co["entity_id"], co["name"], co["relationship"]
                idents, address, officers = {}, None, []
                if cid:
                    cur.execute("SELECT identifiers FROM entities WHERE entity_id=%s", (cid,))
                    r0 = cur.fetchone()
                    idents = (r0[0] if r0 and isinstance(r0[0], dict) else {})
                    cur.execute("""SELECT raw_address, city, region, country, lat, lon, source
                                     FROM entity_addresses WHERE entity_id=%s
                                    ORDER BY (lat IS NOT NULL) DESC, geocoded_at DESC NULLS LAST LIMIT 1""",
                                (cid,))
                    a = cur.fetchone()
                    if a:
                        address = {"raw": a[0], "city": a[1], "region": a[2], "country": a[3],
                                   "lat": a[4], "lon": a[5], "source": a[6]}
                    cur.execute("""SELECT DISTINCT e2.entity_id, e2.canonical_name, er.relationship
                                     FROM entity_relationships er
                                     JOIN entities e2 ON e2.entity_id = er.source_entity_id
                                    WHERE er.target_entity_id=%s AND er.relationship = ANY(%s)
                                      AND e2.canonical_type='person' LIMIT 12""",
                                (cid, OFFICER_RELS))
                    officers = [{"entity_id": o[0], "name": o[1], "role": o[2]} for o in cur.fetchall()]
                # filings — from the org's own investigation (if resolved) OR matched by
                # name in the parent entity's findings (990s, court, filings) with source_url
                cur.execute("""SELECT f.title, f.source, f.finding_type,
                                      COALESCE(f.data->>'source_url', f.data->>'url', f.data->>'document_url', f.data->>'page_url')
                                 FROM investigation_findings f
                                 JOIN entity_investigations ei ON ei.investigation_id=f.investigation_id
                                WHERE (ei.entity_id=%s OR (ei.entity_id=%s AND f.title ILIKE %s))
                                  AND f.finding_type = ANY(ARRAY['filing_document','990_filing','court_case','company','contract','nonprofit','990','financial'])
                                ORDER BY f.confidence DESC NULLS LAST LIMIT 6""",
                            (cid or -1, entity_id, f"%{(name or '')[:40]}%"))
                filings = [{"title": r[0], "source": r[1], "type": r[2], "url": r[3]}
                           for r in cur.fetchall()]
                ids = {k: idents[k] for k in ("ein", "cik", "uei", "ticker", "lei",
                                              "fec_id", "committee_id", "company_number",
                                              "cage", "duns", "qid")
                       if idents.get(k)}
                # For FEC committees: authoritative FEC designation says whether
                # this is the entity's OWN committee (P/A) or an independent one
                # that merely names them. Surface it so the UI never implies an
                # opposing / independent PAC is "his company".
                det = co.get("detail") or {}
                affiliation = det.get("affiliation")
                if rel == "associated_committee" and not affiliation:
                    affiliation = "independent"  # default: only P/A are authorized
                companies.append({
                    "entity_id": cid, "name": name, "relationship": rel, "source": co["source"],
                    "identifiers": ids, "address": address, "officers": officers,
                    "filings": filings, "resolved": bool(cid),
                    "affiliation": affiliation,
                    "designation": det.get("designation_full") or det.get("designation")})
    # richest first (has address / officers / filings)
    companies.sort(key=lambda c: (bool(c["address"]), len(c["officers"]), len(c["filings"])),
                   reverse=True)
    return {"entity_id": entity_id, "companies": companies, "count": len(companies)}


@router.post("/entities/{entity_id}/research-companies")
async def research_companies(entity_id: int, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Deep-investigate the entity (its cascade discovers + investigates the orgs he
    owns/runs) AND batch-queue any already-known company orgs."""
    from core.research.investigation_queue import try_queue_investigation
    queued = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT canonical_name FROM entities WHERE entity_id=%s", (entity_id,))
            row = cur.fetchone()
            seed = row[0] if row else f"entity_{entity_id}"
            names = {c["entity_id"]: c["name"] for c in _companies_raw(cur, entity_id, auth.tenant_id)
                     if c["entity_id"]}
    # 1) the entity itself (cascade walks into owns/officer_of/subsidiary_of orgs)
    try:
        inv_id, outcome = try_queue_investigation(
            entity_id=entity_id, tenant_id=auth.tenant_id, seed_name=seed,
            entity_type="person", discovered_from="research_companies")
        queued.append({"entity_id": entity_id, "name": seed, "status": outcome, "investigation_id": inv_id})
    except Exception as e:
        queued.append({"entity_id": entity_id, "name": seed, "status": f"error: {e}"})
    # 2) each already-known company org
    for cid, nm in names.items():
        try:
            inv_id, outcome = try_queue_investigation(
                entity_id=cid, tenant_id=auth.tenant_id, seed_name=nm,
                entity_type="organization", discovered_from="research_companies")
            queued.append({"entity_id": cid, "name": nm, "status": outcome, "investigation_id": inv_id})
        except Exception as e:
            queued.append({"entity_id": cid, "name": nm, "status": f"error: {e}"})
    return {"entity_id": entity_id, "queued": queued, "count": len(queued)}


@router.get("/entities/{entity_id}/corporate-geo")
async def corporate_geo(entity_id: int, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """GeoJSON of the entity's + its companies' geocoded addresses — the corporate
    footprint on the map."""
    features = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            ids = [entity_id] + [c["entity_id"] for c in _companies_raw(cur, entity_id, auth.tenant_id)
                                 if c["entity_id"]]
            cur.execute("""
                SELECT ea.entity_id, e.canonical_name, ea.raw_address, ea.city, ea.region,
                       ea.address_type, ea.source, ea.lat, ea.lon
                  FROM entity_addresses ea JOIN entities e ON e.entity_id = ea.entity_id
                 WHERE ea.entity_id = ANY(%s) AND ea.lat IS NOT NULL AND ea.lon IS NOT NULL
            """, (ids,))
            for r in cur.fetchall():
                features.append({
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [float(r[8]), float(r[7])]},
                    "properties": {"entity_id": r[0], "name": r[1], "address": r[2],
                                   "city": r[3], "region": r[4], "address_type": r[5],
                                   "source": r[6], "is_root": r[0] == entity_id},
                })
    return {"type": "FeatureCollection", "features": features, "count": len(features)}
