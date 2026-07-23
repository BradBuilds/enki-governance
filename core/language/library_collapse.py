"""Lexical-library collapse — feed a library's terms through the governed
substrate, the same grid entities collapse into.

For each sense in a lexical library:
  1. ensure form_nodes rows (deterministic 11.0.<hash> address) for its surfaces
  2. ensure a concept_nodes row at its DETERMINISTIC sense address (12.<pos>.<hash>
     of library_id:sense_id) — sense metadata (register/pos/gloss/domain) in the
     type_evidence JSONB sidecar
  3. bind form → sense (form_concept_bindings) — the polysemy fan-out
  4. (pass 2) ensure concept→concept relation edges — the expansion graph

Idempotent (re-collapsing yields the same rows), deterministic (addresses are
content-derived, not counted), fail-closed (a sense whose register or POS isn't
governed never enters the canonical layer — it's rejected and counted).
"""
from __future__ import annotations

import hashlib
import json
import re

from core.language.sense_address import (
    sense_address, is_governed_register, is_governed_pos,
)

_WS = re.compile(r"\s+")
SYMMETRIC_RELS = {"synonym", "antonym", "related"}
DIRECTED_RELS = {"hypernym", "hyponym", "meronym", "holonym"}
VALID_RELS = SYMMETRIC_RELS | DIRECTED_RELS


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").lower()).strip()


def _form_hash(norm: str) -> str:
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _ensure_form(cur, tenant: str, surface: str) -> int:
    norm = _norm(surface)
    h = _form_hash(norm)
    cur.execute("""
        INSERT INTO form_nodes
            (tenant_id, form_address, surface_form, surface_form_normalized, surface_form_hash)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (tenant_id, surface_form_hash)
        DO UPDATE SET last_seen_at = now()
        RETURNING form_id
    """, (tenant, f"11.0.{h}", surface, norm, h))
    return cur.fetchone()[0]


def _ensure_sense(cur, tenant: str, library_id: str, sense: dict):
    """(concept_id, status). Fail-closed on ungoverned register/POS."""
    sid = sense["sense_id"]
    pos = (sense.get("pos") or "other")
    reg = (sense.get("register") or "literal")
    if not is_governed_register(reg) or not is_governed_pos(pos):
        return None, "rejected_ungoverned"

    addr = sense_address(library_id, sid, pos)
    meta = {
        "library": library_id, "sense_id": sid,
        "register": reg.strip().lower(), "pos": pos.strip().lower(),
        "gloss": sense.get("gloss"), "domain": sense.get("domain"),
    }
    # SELECT-then-write keyed on the deterministic address → idempotent without
    # depending on a unique constraint over the shared concept table.
    cur.execute(
        "SELECT concept_id FROM concept_nodes WHERE tenant_id = %s AND concept_address = %s",
        (tenant, addr))
    row = cur.fetchone()
    if row:
        cur.execute(
            "UPDATE concept_nodes SET type_evidence = %s::jsonb, updated_at = now() "
            "WHERE concept_id = %s", (json.dumps(meta), row[0]))
        return row[0], "ok"
    cur.execute("""
        INSERT INTO concept_nodes
            (tenant_id, concept_address, subtype_path, canonical_name, canonical_subtype,
             depth, identifiers, type_evidence, evidence_tier, signal_count,
             promotion_status, pack_versions)
        VALUES (%s, %s, %s, %s, %s, 1, '{}'::jsonb, %s::jsonb, 'B', 1, 'pending', %s::jsonb)
        RETURNING concept_id
    """, (tenant, addr, f"lexical.{pos.strip().lower()}", sense.get("lemma", ""),
          pos.strip().lower(), json.dumps(meta), json.dumps({library_id: "0.1.0"})))
    return cur.fetchone()[0], "ok"


def _clean(s):
    """Collapse whitespace/newlines so a gloss is one safe CSV/JSON line."""
    if s is None:
        return None
    return _WS.sub(" ", str(s)).strip()


def _csv_buf(rows):
    """Stream rows as a CSV buffer for COPY ... FROM STDIN (FORMAT csv).
    csv.writer quotes embedded commas/quotes/newlines so JSON fields survive."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    for r in rows:
        w.writerow(["" if v is None else v for v in r])
    buf.seek(0)
    return buf


def collapse_library_copy(senses: list[dict], library_id: str, get_conn,
                          tenant: str = "default", phase: str = "all") -> dict:
    """COPY-based collapse — the optimized, cross-library loader.

    Loading (phase 'load' or 'all'): senses/forms/bindings are staged into TEMP
    tables via COPY, then upserted with INSERT … SELECT that resolves every id with
    an indexed JOIN (concept_address / surface_form_hash) — no Python id-map
    round-trips, so it scales to the whole of WordNet fast.

    Linking (phase 'link' or 'all'): relation edges resolve BOTH endpoints by
    (library, sense_id) joins against the senseref index. That makes in-library and
    CROSS-library relations the same operation — a relation in one library can point
    at a sense in another (demonym→place, glossary term→wordnet sense, the slang and
    literal "fire"). connect_all runs 'load' across all libraries, THEN 'link', so
    cross-library targets exist before edges resolve. Symmetric edges canonicalize on
    the content-stable "library:sense_id" key (federates identically across nodes).

    Fail-closed (ungoverned register/POS dropped). Idempotent (NOT EXISTS / ON
    CONFLICT DO NOTHING)."""
    summary = {"senses": 0, "rejected": 0, "forms": 0, "bindings": 0, "relations": 0}

    # govern + stage rows (no DB ids — joins resolve them)
    valid_sids = set()
    concept_rows, form_rows, bind_rows = [], {}, []
    seen_addr = set()
    for s in senses:
        sid = s["sense_id"]
        pos = (s.get("pos") or "other")
        reg = (s.get("register") or "literal")
        if not is_governed_register(reg) or not is_governed_pos(pos):
            summary["rejected"] += 1
            continue
        valid_sids.add(sid)
        addr = sense_address(library_id, sid, pos)
        if addr not in seen_addr:
            seen_addr.add(addr)
            meta = {"library": library_id, "sense_id": sid, "register": reg.strip().lower(),
                    "pos": pos.strip().lower(), "gloss": _clean(s.get("gloss")),
                    "domain": s.get("domain")}
            concept_rows.append((tenant, addr, f"lexical.{pos.strip().lower()}",
                                 _clean(s.get("lemma")) or "", pos.strip().lower(),
                                 json.dumps(meta), json.dumps({library_id: "1"})))
        for surface in s.get("forms", []):
            norm = _norm(surface)
            h = _form_hash(norm)
            form_rows.setdefault(h, (tenant, f"11.0.{h}", _clean(surface), norm, h))
            bind_rows.append((tenant, h, addr))
    summary["senses"] = len(concept_rows)

    with get_conn() as conn, conn.cursor() as cur:
        if phase in ("all", "load"):
            # staging is already deduped in Python (seen_addr / form_rows dict), and
            # unique constraints exist on (tenant_id, concept_address) and
            # (tenant_id, surface_form_hash) — so ON CONFLICT is both correct and
            # faster than a NOT EXISTS anti-join, and no DISTINCT sort is needed.
            cur.execute("CREATE TEMP TABLE _tc (tenant text, addr text, sp text, nm text, "
                        "sub text, te text, pv text) ON COMMIT DROP")
            cur.copy_expert("COPY _tc FROM STDIN WITH (FORMAT csv)", _csv_buf(concept_rows))
            cur.execute("ANALYZE _tc")  # else the planner assumes ~1 row → nested loop
            cur.execute("""
                INSERT INTO concept_nodes (tenant_id, concept_address, subtype_path,
                    canonical_name, canonical_subtype, depth, identifiers, type_evidence,
                    evidence_tier, signal_count, promotion_status, pack_versions)
                SELECT t.tenant, t.addr, t.sp, t.nm, t.sub, 1, '{}'::jsonb, t.te::jsonb,
                    'B', 1, 'pending', t.pv::jsonb
                FROM _tc t
                ON CONFLICT (tenant_id, concept_address) DO NOTHING
            """)
            cur.execute("CREATE TEMP TABLE _tf (tenant text, addr text, sf text, nf text, "
                        "h text) ON COMMIT DROP")
            cur.copy_expert("COPY _tf FROM STDIN WITH (FORMAT csv)",
                            _csv_buf(list(form_rows.values())))
            cur.execute("ANALYZE _tf")
            cur.execute("""
                INSERT INTO form_nodes (tenant_id, form_address, surface_form,
                    surface_form_normalized, surface_form_hash)
                SELECT t.tenant, t.addr, t.sf, t.nf, t.h FROM _tf t
                ON CONFLICT (tenant_id, surface_form_hash) DO NOTHING
            """)
            cur.execute("CREATE TEMP TABLE _tb (tenant text, h text, addr text) ON COMMIT DROP")
            cur.copy_expert("COPY _tb FROM STDIN WITH (FORMAT csv)", _csv_buf(bind_rows))
            cur.execute("ANALYZE _tb")  # 212k rows — without stats this picks a 10-min nested loop
            # The bindings JOIN resolves form_id/concept_id against form_nodes and
            # concept_nodes, which were just bulk-filled IN THIS TRANSACTION — so
            # their planner stats are stale (look near-empty) and the join degrades
            # to a seq-scan nested loop (hangs for hours on a fresh pool). Refresh
            # both before the join so it uses the unique indexes / hash join.
            cur.execute("ANALYZE concept_nodes")
            cur.execute("ANALYZE form_nodes")
            cur.execute("""
                INSERT INTO form_concept_bindings (form_id, concept_id, binding_confidence,
                    binding_source)
                SELECT f.form_id, c.concept_id, 0.99, 'lexical_library'
                FROM _tb t
                JOIN form_nodes f ON f.tenant_id = t.tenant AND f.surface_form_hash = t.h
                JOIN concept_nodes c ON c.tenant_id = t.tenant AND c.concept_address = t.addr
                ON CONFLICT (form_id, concept_id) DO NOTHING
            """)
            summary["bindings"] = cur.rowcount
            conn.commit()

        if phase in ("all", "link"):
            # the relation join resolves endpoints via the senseref index on
            # concept_nodes; after a bulk load that table's stats are stale, so the
            # planner can misjudge and pick a seq-scan nested loop. Refresh stats so
            # it reliably uses the index. (Index must be NON-partial — migration 126.)
            cur.execute("ANALYZE concept_nodes")
            rel_rows = []
            for s in senses:
                a_sid = s["sense_id"]
                if a_sid not in valid_sids:
                    continue
                for rel in s.get("relations", []):
                    rt = (rel.get("rel") or "").lower()
                    b_sid = rel.get("sense_id")
                    if rt not in VALID_RELS or not b_sid:
                        continue
                    b_lib = rel.get("library", library_id)  # cross-library when set
                    a_key, b_key = (library_id, a_sid), (b_lib, b_sid)
                    if rt in SYMMETRIC_RELS and f"{a_key[0]}:{a_key[1]}" > f"{b_key[0]}:{b_key[1]}":
                        a_key, b_key = b_key, a_key  # canonicalize A~B == B~A
                    rel_rows.append((tenant, a_key[0], a_key[1], b_key[0], b_key[1],
                                     rt, rt in SYMMETRIC_RELS, library_id))
            if rel_rows:
                cur.execute("CREATE TEMP TABLE _tr (tenant text, al text, asid text, bl text, "
                            "bsid text, rt text, sym boolean, src text) ON COMMIT DROP")
                cur.copy_expert("COPY _tr FROM STDIN WITH (FORMAT csv)", _csv_buf(rel_rows))
                cur.execute("ANALYZE _tr")
                cur.execute("""
                    INSERT INTO concept_relations (tenant_id, concept_id_a, concept_id_b,
                        rel_type, is_symmetric, source_library)
                    SELECT DISTINCT t.tenant, ca.concept_id, cb.concept_id, t.rt, t.sym, t.src
                    FROM _tr t
                    JOIN concept_nodes ca ON ca.tenant_id = t.tenant
                        AND ca.type_evidence->>'library' = t.al
                        AND ca.type_evidence->>'sense_id' = t.asid
                    JOIN concept_nodes cb ON cb.tenant_id = t.tenant
                        AND cb.type_evidence->>'library' = t.bl
                        AND cb.type_evidence->>'sense_id' = t.bsid
                    ON CONFLICT (concept_id_a, concept_id_b, rel_type) DO NOTHING
                """)
                summary["relations"] = cur.rowcount
                conn.commit()
    return summary


def collapse_library_bulk(senses: list[dict], library_id: str, get_conn,
                          tenant: str = "default") -> dict:
    """Back-compat alias — full single-call collapse via the COPY loader."""
    return collapse_library_copy(senses, library_id, get_conn, tenant, phase="all")


def collapse_library(senses: list[dict], library_id: str, get_conn, tenant: str = "default") -> dict:
    summary = {"senses": 0, "rejected": 0, "forms": 0, "bindings": 0, "relations": 0}
    cid_by_sid: dict[str, int] = {}
    addr_by_sid: dict[str, str] = {}

    # PASS 1 — forms, senses, bindings
    with get_conn() as conn:
        with conn.cursor() as cur:
            for s in senses:
                cid, status = _ensure_sense(cur, tenant, library_id, s)
                if cid is None:
                    summary["rejected"] += 1
                    continue
                summary["senses"] += 1
                cid_by_sid[s["sense_id"]] = cid
                addr_by_sid[s["sense_id"]] = sense_address(
                    library_id, s["sense_id"], s.get("pos", "other"))
                for surface in s.get("forms", []):
                    fid = _ensure_form(cur, tenant, surface)
                    summary["forms"] += 1
                    cur.execute("""
                        INSERT INTO form_concept_bindings
                            (form_id, concept_id, binding_confidence, binding_source)
                        VALUES (%s, %s, 0.99, 'lexical_library')
                        ON CONFLICT (form_id, concept_id) DO NOTHING
                    """, (fid, cid))
                    summary["bindings"] += 1
        conn.commit()

    # PASS 2 — relation/expansion edges (all sense concepts now exist)
    with get_conn() as conn:
        with conn.cursor() as cur:
            for s in senses:
                a = cid_by_sid.get(s["sense_id"])
                if a is None:
                    continue
                for rel in s.get("relations", []):
                    rt = (rel.get("rel") or "").lower()
                    tgt = rel.get("sense_id")
                    b = cid_by_sid.get(tgt)
                    if b is None or rt not in VALID_RELS:
                        continue
                    sym = rt in SYMMETRIC_RELS
                    ca, cb = a, b
                    if sym and addr_by_sid[s["sense_id"]] > addr_by_sid[tgt]:
                        ca, cb = b, a  # canonicalize so A~B == B~A
                    cur.execute("""
                        INSERT INTO concept_relations
                            (tenant_id, concept_id_a, concept_id_b, rel_type, is_symmetric, source_library)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (concept_id_a, concept_id_b, rel_type) DO NOTHING
                    """, (tenant, ca, cb, rt, sym, library_id))
                    summary["relations"] += 1
        conn.commit()

    return summary
