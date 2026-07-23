"""
Cards API — REST endpoints that feed the card kit v2 UI.

Surface for ui/src/lib/components/cards2/:
  GET /api/libraries                       — list all libraries
  GET /api/libraries/[id]                  — one library + counts
  GET /api/libraries/[id]/references       — references in that library
  GET /api/libraries/[id]/documents        — docs in that library
  GET /api/libraries/[id]/entities         — entities in that library
  GET /api/libraries/[id]/facts            — assessments in that library
  GET /api/libraries/[id]/events           — events in that library
  GET /api/entities/[id]                   — entity card payload
  GET /api/entities/[id]/neighbors         — 1-hop neighborhood
  GET /api/assessments/[id]                — fact card payload
  GET /api/events/[id]                     — event card payload
  GET /api/references/[id]                 — reference card payload

Each endpoint returns a flat JSON shape matching what the corresponding
card expects (see ui/src/lib/components/cards2/*.svelte).

Read-only for now; writes happen via dedicated mutation endpoints in
later phases (F4 entity edit, F5 fact reverse, etc.).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from storage.postgres.db import get_connection
from core.documents import anaphora_resolver
from core.documents.docket_parser import parse_docket

log = logging.getLogger("enki.api.cards")

router = APIRouter(tags=["cards"])


def _rows_to_dicts(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# Re-exports for backward compat. Logic lives in services/api/src/_entity_redirect.py
# so other route modules (watch_topics, p2p, entity_profile) can use the same
# resolution + 410-surfacing without duplicating the code.
from services.api.src._entity_redirect import (
    resolve_entity_or_410 as _follow_entity_redirect,
)


def _serialize(value: Any) -> Any:
    """Make sure datetimes / decimals / etc. are JSON-friendly."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "__float__") and not isinstance(value, (int, bool)):
        try:
            return float(value)
        except Exception:
            pass
    return value


def _clean(row: dict) -> dict:
    return {k: _serialize(v) for k, v in row.items()}


# ─── Libraries ────────────────────────────────────────────────────────

@router.get("/libraries")
def list_libraries(
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    """List libraries with their counts."""
    sql = """
        SELECT library_id, base60_address, library_subtype, canonical_name,
               focal_term, description, tenant_id,
               reference_count, acquired_count, pending_count,
               unavailable_count, document_count, entity_count, fact_count,
               discovery_status, refresh_interval_hours,
               last_refreshed_at, next_refresh_at, created_at
          FROM libraries
    """
    params: list[Any] = []
    if status:
        sql += " WHERE discovery_status = %s"
        params.append(status)
    sql += " ORDER BY last_updated_at DESC NULLS LAST LIMIT %s"
    params.append(limit)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            libraries = [_clean(r) for r in _rows_to_dicts(cur)]
    return {"libraries": libraries, "count": len(libraries)}


@router.get("/libraries/{library_id}")
def get_library(library_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT library_id, base60_address, library_subtype, canonical_name,
                       focal_term, description, tenant_id,
                       reference_count, acquired_count, pending_count,
                       unavailable_count, document_count, entity_count, fact_count,
                       discovery_status, refresh_interval_hours,
                       last_refreshed_at, next_refresh_at, created_at,
                       last_updated_at, notes,
                       federation_id, original_producer_node_id, first_published_at
                  FROM libraries WHERE library_id = %s
                """,
                (library_id,),
            )
            rows = _rows_to_dicts(cur)
    if not rows:
        raise HTTPException(404, f"library {library_id} not found")
    return _clean(rows[0])


def _library_member_ids(conn, library_id: int, kind: str) -> list[int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT member_id FROM library_members "
            " WHERE library_id = %s AND member_kind = %s",
            (library_id, kind),
        )
        return [r[0] for r in cur.fetchall()]


@router.get("/libraries/{library_id}/references")
def library_references(library_id: int, status: Optional[str] = None, limit: int = 100):
    sql = """
        SELECT reference_id, base60_address, citation_text, title,
               authors, publication_year, publisher, reference_type,
               identifiers, discovered_via, discovery_query,
               acquisition_status, acquisition_attempts,
               last_attempted_at, document_id, purchase_links
          FROM bibliographic_references
         WHERE library_id = %s
    """
    params: list[Any] = [library_id]
    if status:
        sql += " AND acquisition_status = %s"
        params.append(status)
    sql += " ORDER BY reference_id LIMIT %s"
    params.append(limit)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = [_clean(r) for r in _rows_to_dicts(cur)]
    return {"references": rows, "count": len(rows)}


@router.get("/libraries/{library_id}/documents")
def library_documents(library_id: int, limit: int = 100):
    with get_connection() as conn:
        doc_ids = _library_member_ids(conn, library_id, "document")
        if not doc_ids:
            return {"documents": [], "count": 0}
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT document_id, title, dataset_id, page_count, trust_class,
                       created_at, on_disk_path, source_type
                  FROM documents WHERE document_id = ANY(%s)
                 ORDER BY document_id DESC LIMIT %s
                """,
                (doc_ids, limit),
            )
            rows = [_clean(r) for r in _rows_to_dicts(cur)]
    return {"documents": rows, "count": len(rows)}


@router.get("/libraries/{library_id}/entities")
def library_entities(library_id: int, limit: int = 100):
    with get_connection() as conn:
        ent_ids = _library_member_ids(conn, library_id, "entity")
        if not ent_ids:
            return {"entities": [], "count": 0}
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT entity_id, canonical_name, canonical_type, entity_kind,
                       base60_address, identifiers, signal_count, aliases
                  FROM entities WHERE entity_id = ANY(%s)
                 ORDER BY signal_count DESC LIMIT %s
                """,
                (ent_ids, limit),
            )
            rows = [_clean(r) for r in _rows_to_dicts(cur)]
    return {"entities": rows, "count": len(rows)}


@router.get("/libraries/{library_id}/facts")
def library_facts(library_id: int, tier: Optional[str] = None, limit: int = 100):
    """Facts (assessments) whose source statements originate inside this
    library's documents/signals.

    Source-scoped, not entity-scoped — an assessment is "in" a library iff
    its evidence came from a member doc or signal. This prevents
    cross-contamination (e.g. a Vale-Netanyahu RSS news fact appearing in
    the Acme library just because Vale also shows up in some Acme
    document).
    """
    with get_connection() as conn:
        doc_ids = _library_member_ids(conn, library_id, "document")
        sig_ids = _library_member_ids(conn, library_id, "signal")
        if not doc_ids and not sig_ids:
            return {"facts": [], "count": 0}
        sql = """
            SELECT a.assessment_id, a.base60_address, a.assessment_tier AS tier,
                   a.subj_entity_id, ses.canonical_name AS subj_name,
                   a.predicate_edge_type,
                   a.obj_entity_id, oes.canonical_name AS obj_name,
                   a.obj_value, a.polarity,
                   a.corroborated_by_count, a.contradicted_by_count,
                   a.earliest_event_time, a.latest_event_time,
                   a.confidence
              FROM assessments a
              LEFT JOIN entities ses ON ses.entity_id = a.subj_entity_id
              LEFT JOIN entities oes ON oes.entity_id = a.obj_entity_id
             WHERE EXISTS (
               SELECT 1 FROM statements s
                WHERE s.statement_id = ANY(a.source_statement_ids)
                  AND (s.document_id = ANY(%s) OR s.signal_rid = ANY(%s))
             )
        """
        params: list[Any] = [doc_ids or [-1], sig_ids or [-1]]
        if tier:
            sql += " AND a.assessment_tier = %s"
            params.append(tier)
        sql += """ ORDER BY a.corroborated_by_count DESC NULLS LAST,
                            a.latest_event_time DESC NULLS LAST,
                            a.first_seen DESC
                   LIMIT %s
                """
        # Over-fetch so the noise filter has room to trim before truncating.
        params.append(limit * 3)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = [_clean(r) for r in _rows_to_dicts(cur)]
    # Reuse the same noise/self-loop filter the timeline uses. Map
    # facts-shape (subj_name/obj_name) to the noise-filter's expected
    # keys (subj/obj) for the duration of the call.
    for r in rows:
        r.setdefault("subj", r.get("subj_name"))
        r.setdefault("obj", r.get("obj_name"))
    rows = _filter_noisy_assessments(rows)[:limit]
    return {"facts": rows, "count": len(rows)}


# ─── Universal address router ───────────────────────────────────────────
# Resolves a base-60 address to its typed row so the UI can deep-link
# from a raw address string (e.g. "0.0.2.26" for Alex Acme) to
# the right dossier page without the caller knowing the type up-front.

@router.get("/address/{address:path}")
def resolve_address(address: str):
    """Look up a base-60 address across all governed tables.

    Returns ``{type, id, name, redirect_path}`` for the first hit, or
    404 if no row has that address. ``type`` is the dossier kind the UI
    should render: entity, event, library, knowledge_node, concept.
    """
    addr = (address or "").strip()
    if not addr:
        raise HTTPException(400, "address is required")
    # Tables to probe, in priority order. (table, id_col, name_col, kind, path_fmt)
    probes = [
        ("entities",        "entity_id",     "canonical_name", "entity",         "/entity/{id}"),
        ("events",          "event_id",      "title",          "event",          "/event/{id}"),
        ("libraries",       "library_id",    "canonical_name", "library",        "/library/{id}"),
        ("concept_nodes",   "concept_id",    "canonical_name", "concept",        "/entity/{id}"),
        ("knowledge_nodes", "node_id",       "title",          "knowledge_node", "/document/{id}"),
    ]
    with get_connection() as conn:
        with conn.cursor() as cur:
            for table, idcol, namecol, kind, path_fmt in probes:
                try:
                    cur.execute(
                        f"SELECT {idcol}, {namecol} FROM {table} "
                        f" WHERE base60_address = %s LIMIT 1",
                        (addr,),
                    )
                    row = cur.fetchone()
                except Exception:
                    # Table missing the column or doesn't exist on this
                    # deploy — fall through to the next probe.
                    conn.rollback()
                    continue
                if row:
                    # concept_nodes routes through to its promoted entity
                    # when one exists, since concepts have no own page.
                    rid = row[0]
                    if kind == "concept":
                        cur.execute(
                            "SELECT promoted_to_entity_id FROM concept_nodes "
                            " WHERE concept_id = %s",
                            (rid,),
                        )
                        promoted = cur.fetchone()
                        if promoted and promoted[0]:
                            rid = promoted[0]
                            path_fmt = "/entity/{id}"
                    return {
                        "type": kind,
                        "id": rid,
                        "name": row[1],
                        "address": addr,
                        "redirect_path": path_fmt.format(id=rid),
                    }
    raise HTTPException(404, f"no row found with address {addr!r}")


@router.get("/libraries/{library_id}/about")
def library_about(library_id: int):
    """Wikipedia-style summary for the library's focal entity. Pulled from
    entity_kb_cache (the deep-research enrichment store)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT focal_term, focal_entity_id, canonical_name FROM libraries WHERE library_id=%s",
                (library_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="library not found")
            focal_term, focal_entity_id, canonical_name = row
            blurb: dict = {"focal_term": focal_term, "canonical_name": canonical_name}

            # Try entity_id direct lookup first, then by normalized_name.
            if focal_entity_id:
                cur.execute(
                    "SELECT description, source, source_url FROM entity_kb_cache "
                    " WHERE canonical_name = (SELECT canonical_name FROM entities WHERE entity_id=%s) LIMIT 1",
                    (focal_entity_id,),
                )
                kb = cur.fetchone()
                if kb:
                    blurb["description"] = kb[0]
                    blurb["source"] = kb[1]
                    blurb["source_url"] = kb[2]

            if "description" not in blurb and focal_term:
                cur.execute(
                    "SELECT description, source, source_url FROM entity_kb_cache "
                    " WHERE LOWER(canonical_name) = LOWER(%s) LIMIT 1",
                    (focal_term,),
                )
                kb = cur.fetchone()
                if kb:
                    blurb["description"] = kb[0]
                    blurb["source"] = kb[1]
                    blurb["source_url"] = kb[2]
    return blurb


# OCR / structural noise filter applied to every fact-listing endpoint.
# A name is "noisy" if it looks like an extraction artefact, not a real entity.
# Targets the actual rot we have today, NOT every borderline case:
#   - ends with ! or ? (e.g. "Adrninetrative Office of lhas Cour!")
#   - mid-token ! or ?
#   - bare honorific (Mr., Ms., Mrs., Dr.)
#   - random-looking case mix (XyZABCd, mIxEdCaSe cAsEd) — 3+ case
#     transitions in a single token, where neither all-upper nor TitleCase
# Legitimate names with periods/commas (Washington, D.C. · LLP · Jr.) survive,
# as do TitleCase names (McCawley) and acronyms (LLP, NATO).
_NOISE_HONORIFICS = {"mr", "ms", "mrs", "dr", "mr.", "ms.", "mrs.", "dr."}


def _is_noisy_name(name: str | None) -> bool:
    if not name:
        return False
    s = name.strip()
    if not s:
        return False
    if s.lower() in _NOISE_HONORIFICS:
        return True
    # Trailing or internal ! / ? — punctuation we never see in real names
    if "!" in s or "?" in s:
        return True
    # Trailing artefact punctuation: a name ending in - _ : ; or a
    # standalone trailing comma followed by nothing is an OCR line-break
    # fragment ("Peter-" was "Peterson" wrapped across lines).
    # Legitimate cases like "Washington, D.C." or "Jr." keep their suffix.
    if s[-1] in "-_:;":
        return True
    # Trailing isolated single-char suffix like "X," or "Y." with no group
    # before — too short to be a real name.
    if len(s) <= 2:
        return True
    # Per-token case-mix checks. Acronyms (all upper) and TitleCase compounds
    # (e.g. McCawley, LeFevre, iPhone) survive; OCR scramble does not.
    for tok in s.split():
        letters = [c for c in tok if c.isalpha()]
        if len(letters) < 4:
            continue
        # Rule 1: 4+ case transitions in one token. SigridMcCawley has 5,
        # mIxEdCaSe has 5, cAsEd has 4. Normal TitleCase has at most 3.
        transitions = sum(
            1 for i in range(1, len(letters))
            if letters[i].isupper() != letters[i - 1].isupper()
        )
        if transitions >= 4:
            return True
        # Rule 2: mid-token uppercase ratio. Skip pure acronyms (no lower).
        # XyZABCd: 4 of 6 non-first letters upper = 67%. iPhone: 1/5 = 20%.
        if any(c.islower() for c in letters):
            non_first = letters[1:]
            if non_first:
                upper_ratio = sum(1 for c in non_first if c.isupper()) / len(non_first)
                if upper_ratio > 0.35:
                    return True
    return False


# ─── Lens / context scoping ────────────────────────────────────────────────
#
# A "lens" narrows an entity-keyed query to source provenance within a
# named context. Format: "<scope>/<value>". Recognised scopes today:
#
#   library/<id>   — only facts whose source statements originate from
#                    documents or signals that are members of library <id>
#   topic/<entity_id> — only facts touching the entity in a corpus of
#                       documents/signals where the topic entity is also a
#                       member (placeholder, refined later)
#
# When no lens is passed, the entity's canonical (cross-corpus) view is
# returned. Lens application happens via _lens_assessment_clause() which
# returns an extra WHERE-fragment + param list to AND into the caller's SQL.


def _parse_lens(lens: str | None) -> tuple[str | None, str | None]:
    """Parse 'library/3' → ('library', '3'). Returns (None, None) on no/invalid lens."""
    if not lens:
        return None, None
    if "/" not in lens:
        return None, None
    scope, value = lens.split("/", 1)
    scope = scope.strip().lower()
    value = value.strip()
    if not value or scope not in {"library", "topic"}:
        return None, None
    return scope, value


def _lens_assessment_clause(conn, lens: str | None) -> tuple[str, list]:
    """Return (extra_where_sql, params) that, when appended to a query
    over assessments aliased `a`, restricts to source-provenance within
    the lens. Empty (no-op) when lens is None or unknown.

    The clause references `a.source_statement_ids` so the caller must alias
    the assessments table as `a`.
    """
    scope, value = _parse_lens(lens)
    if scope == "library":
        try:
            lib_id = int(value)
        except (TypeError, ValueError):
            return "", []
        doc_ids = _library_member_ids(conn, lib_id, "document") or [-1]
        sig_ids = _library_member_ids(conn, lib_id, "signal") or [-1]
        clause = """
          AND EXISTS (
            SELECT 1 FROM statements s
             WHERE s.statement_id = ANY(a.source_statement_ids)
               AND (s.document_id = ANY(%s) OR s.signal_rid = ANY(%s))
          )
        """
        return clause, [doc_ids, sig_ids]
    return "", []


def _enrich_display_names(rows: list[dict]) -> None:
    """For each row, if subj/obj is a single-token name AND the KB cache
    has a fuller matching canonical name (Marlow → Kevin Marlow), set a
    `subj_display`/`obj_display` field the UI can render. Substrate name
    stays as-is for navigation; only the visible label upgrades."""
    # Collect single-token names we might want to enrich.
    names: set[str] = set()
    for a in rows:
        for side in ("subj", "obj"):
            v = a.get(side) or a.get(f"{side}_name")
            if not v:
                continue
            if " " not in v.strip() and len(v) >= 3:
                names.add(v.strip())
    if not names:
        return

    display_lookup: dict[str, str] = {}
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Find KB entries whose canonical_name contains the short
                # name as a token AND has at least one other token.
                # "marlow" → "Kevin Marlow". Single result per short name only
                # (ambiguous matches are skipped — we don't want to render
                # the wrong fuller name).
                for short in names:
                    cur.execute(
                        """
                        SELECT canonical_name FROM entity_kb_cache
                         WHERE canonical_name ILIKE %s
                           AND POSITION(' ' IN canonical_name) > 0
                         ORDER BY LENGTH(description) DESC NULLS LAST
                         LIMIT 2
                        """,
                        (f"% {short}",),
                    )
                    matches = [r[0] for r in cur.fetchall()]
                    # Also try matching as a prefix word: "Alex *"
                    cur.execute(
                        """
                        SELECT canonical_name FROM entity_kb_cache
                         WHERE canonical_name ILIKE %s
                           AND POSITION(' ' IN canonical_name) > 0
                         ORDER BY LENGTH(description) DESC NULLS LAST
                         LIMIT 2
                        """,
                        (f"{short} %",),
                    )
                    matches.extend(r[0] for r in cur.fetchall())
                    matches = list({m for m in matches if m})
                    if len(matches) == 1:
                        display_lookup[short] = matches[0]
    except Exception as e:
        log.debug("KB display-name enrichment failed: %s", e)
        return

    for a in rows:
        for side in ("subj", "obj"):
            v = a.get(side) or a.get(f"{side}_name")
            if v and v.strip() in display_lookup:
                a[f"{side}_display"] = display_lookup[v.strip()]


def _filter_noisy_assessments(rows: list[dict]) -> list[dict]:
    """Drop assessments whose subject or object name is OCR-noisy or
    that are pure self-loops (subj_id == obj_id, an extractor bug)."""
    kept: list[dict] = []
    for a in rows:
        sid = a.get("subj_entity_id")
        oid = a.get("obj_entity_id")
        if sid is not None and oid is not None and sid == oid:
            continue
        if _is_noisy_name(a.get("subj")) or _is_noisy_name(a.get("subj_name")):
            continue
        if _is_noisy_name(a.get("obj")) or _is_noisy_name(a.get("obj_name")):
            continue
        kept.append(a)
    return kept


@router.get("/libraries/{library_id}/timeline")
def library_timeline(library_id: int, limit: int = 200, include_noise: bool = False):
    """Time-ordered facts for this library, with source provenance.

    Returns assessments ordered by event_time (when known), each carrying
    the supporting statements (raw text + source document or signal).
    This is the substrate for /library/[id] Timeline tab — who said what
    about whom, when, and where it came from.

    OCR-noise and self-loop assessments are filtered by default. Pass
    include_noise=true to see them (useful for debugging extraction).
    """
    with get_connection() as conn:
        # Scope by SOURCE PROVENANCE, not by entity membership. An assessment
        # belongs in a library's timeline iff at least one of its source
        # statements originated in a document or signal that's a member of
        # this library. This is what prevents cross-library contamination
        # (e.g. a Vale-Netanyahu fact from RSS news leaking into the
        # Acme library just because Vale also appears in some Acme
        # document).
        doc_ids = _library_member_ids(conn, library_id, "document")
        sig_ids = _library_member_ids(conn, library_id, "signal")
        if not doc_ids and not sig_ids:
            return {"events": [], "count": 0}
        with conn.cursor() as cur:
            # Over-fetch when filtering noise — Python pass below trims it.
            sql_limit = limit * 3 if not include_noise else limit
            cur.execute(
                """
                SELECT a.assessment_id, a.base60_address, a.assessment_tier,
                       a.subj_entity_id, ses.canonical_name AS subj,
                       ses.canonical_type AS subj_type,
                       a.predicate_edge_type,
                       a.obj_entity_id, oes.canonical_name AS obj,
                       oes.canonical_type AS obj_type, a.obj_value,
                       a.earliest_event_time, a.latest_event_time,
                       a.primary_location_entity_id,
                       loc.canonical_name AS location_name,
                       a.corroborated_by_count, a.contradicted_by_count,
                       a.confidence, a.source_statement_ids,
                       -- Derived display date: extracted time wins, else
                       -- earliest source document release date (when the
                       -- claim was published), else assessment first_seen
                       -- (when Enki captured it). The kind tells the UI
                       -- whether to render as "occurred" vs "reported".
                       COALESCE(
                           a.earliest_event_time,
                           (SELECT MIN(s2.event_time) FROM statements s2
                             WHERE s2.statement_id = ANY(a.source_statement_ids)
                               AND s2.event_time IS NOT NULL),
                           (SELECT MIN(d2.release_date)::timestamptz FROM statements s2
                              JOIN documents d2 ON d2.document_id = s2.document_id
                             WHERE s2.statement_id = ANY(a.source_statement_ids)
                               AND d2.release_date IS NOT NULL),
                           a.first_seen
                       ) AS display_date,
                       CASE
                         WHEN a.earliest_event_time IS NOT NULL THEN 'occurred'
                         WHEN EXISTS (SELECT 1 FROM statements s2
                                       WHERE s2.statement_id = ANY(a.source_statement_ids)
                                         AND s2.event_time IS NOT NULL) THEN 'occurred'
                         WHEN EXISTS (SELECT 1 FROM statements s2
                                       JOIN documents d2 ON d2.document_id = s2.document_id
                                      WHERE s2.statement_id = ANY(a.source_statement_ids)
                                        AND d2.release_date IS NOT NULL) THEN 'reported'
                         ELSE 'captured'
                       END AS display_date_kind
                  FROM assessments a
                  LEFT JOIN entities ses ON ses.entity_id = a.subj_entity_id
                  LEFT JOIN entities oes ON oes.entity_id = a.obj_entity_id
                  LEFT JOIN entities loc ON loc.entity_id = a.primary_location_entity_id
                 WHERE EXISTS (
                   SELECT 1 FROM statements s
                    WHERE s.statement_id = ANY(a.source_statement_ids)
                      AND (s.document_id = ANY(%s) OR s.signal_rid = ANY(%s))
                 )
                 -- date-sanity: drop assessments whose extracted event
                 -- time is bogus (pre-1900 sequence-number parses, or a
                 -- future date) so the timeline never leads with 2051.
                 -- Undated assessments are kept (they fall back to the
                 -- source document's release_date below).
                   AND (a.earliest_event_time IS NULL
                        OR a.earliest_event_time::date BETWEEN DATE '1900-01-01' AND CURRENT_DATE + INTERVAL '1 year')
                   AND (a.latest_event_time IS NULL
                        OR a.latest_event_time::date BETWEEN DATE '1900-01-01' AND CURRENT_DATE + INTERVAL '1 year')
                 ORDER BY COALESCE(
                     a.latest_event_time, a.earliest_event_time,
                     (SELECT MIN(s2.event_time) FROM statements s2
                       WHERE s2.statement_id = ANY(a.source_statement_ids)
                         AND s2.event_time IS NOT NULL),
                     (SELECT MIN(d2.release_date)::timestamptz FROM statements s2
                        JOIN documents d2 ON d2.document_id = s2.document_id
                       WHERE s2.statement_id = ANY(a.source_statement_ids)
                         AND d2.release_date IS NOT NULL),
                     a.first_seen
                 ) DESC NULLS LAST
                 LIMIT %s
                """,
                (doc_ids or [-1], sig_ids or [-1], sql_limit),
            )
            assessments = []
            for r in cur.fetchall():
                a = dict(zip([d[0] for d in cur.description], r))
                for k in ("earliest_event_time", "latest_event_time", "display_date"):
                    if a.get(k) is not None:
                        a[k] = a[k].isoformat()
                a["sources"] = []
                assessments.append(a)
            if not include_noise:
                assessments = _filter_noisy_assessments(assessments)
            assessments = assessments[:limit]
            stmt_ids_pool: list[int] = [
                sid for a in assessments for sid in (a.get("source_statement_ids") or [])
            ]

            # Pull source statements (raw_text + document_id/signal_rid)
            sources_by_stmt: dict[int, dict] = {}
            if stmt_ids_pool:
                # ±2 paragraph window for real context (same shape as
                # /entities/{id}/facts above).
                cur.execute(
                    """
                    WITH pstmt AS (
                        SELECT s.statement_id, s.raw_text, s.event_time,
                               s.document_id, d.title AS doc_title,
                               s.paragraph_id, p.page_id, p.order_index,
                               s.signal_rid, sig.payload->>'title' AS sig_title,
                               sig.payload->>'source' AS sig_source,
                               sig.payload->>'link' AS sig_link,
                               sig.timestamp AS sig_time
                          FROM statements s
                          LEFT JOIN documents          d ON d.document_id = s.document_id
                          LEFT JOIN document_paragraphs p ON p.paragraph_id = s.paragraph_id
                          LEFT JOIN signals            sig ON sig.rid = s.signal_rid
                         WHERE s.statement_id = ANY(%s)
                    )
                    SELECT pstmt.*, COALESCE(string_agg(neighbor.raw_text,
                                                       ' ' ORDER BY neighbor.order_index),
                                             '') AS context_window
                      FROM pstmt
                      LEFT JOIN document_paragraphs neighbor
                             ON neighbor.page_id = pstmt.page_id
                            AND neighbor.order_index BETWEEN pstmt.order_index - 2
                                                     AND pstmt.order_index + 2
                     GROUP BY pstmt.statement_id, pstmt.raw_text, pstmt.event_time,
                              pstmt.document_id, pstmt.doc_title, pstmt.paragraph_id,
                              pstmt.page_id, pstmt.order_index,
                              pstmt.signal_rid, pstmt.sig_title, pstmt.sig_source,
                              pstmt.sig_link, pstmt.sig_time
                    """,
                    (stmt_ids_pool,),
                )
                for r in cur.fetchall():
                    sources_by_stmt[r[0]] = {
                        "statement_id": r[0],
                        "raw_text": r[1],
                        "event_time": r[2].isoformat() if r[2] else None,
                        "document_id": r[3],
                        "document_title": r[4],
                        "paragraph_id": r[5],
                        "page_id": r[6],
                        "order_index": r[7],
                        "signal_rid": r[8],
                        "signal_title": r[9],
                        "signal_source": r[10],
                        "signal_link": r[11],
                        "signal_time": r[12].isoformat() if r[12] else None,
                        "paragraph_text": (r[13] or "").strip() or None,
                    }

            for a in assessments:
                for sid in a.get("source_statement_ids") or []:
                    if sid in sources_by_stmt:
                        a["sources"].append(sources_by_stmt[sid])

            # Entity blurbs from entity_kb_cache — short description per entity
            # so the timeline can render "Imhotep (Egyptian polymath, …)" inline.
            ent_ids: set[int] = set()
            for a in assessments:
                for k in ("subj_entity_id", "obj_entity_id", "primary_location_entity_id"):
                    v = a.get(k)
                    if v: ent_ids.add(v)
            blurb_by_eid: dict[int, str] = {}
            if ent_ids:
                cur.execute(
                    """
                    SELECT e.entity_id, kb.description
                      FROM entities e
                      JOIN entity_kb_cache kb ON LOWER(kb.canonical_name) = LOWER(e.canonical_name)
                     WHERE e.entity_id = ANY(%s)
                       AND kb.description IS NOT NULL
                       AND kb.description <> ''
                    """,
                    (list(ent_ids),),
                )
                for r in cur.fetchall():
                    eid, desc = r
                    # 1-line trim for the inline display
                    if desc and eid not in blurb_by_eid:
                        blurb_by_eid[eid] = (desc.split(".")[0] + ".")[:200]
            for a in assessments:
                a["subj_blurb"] = blurb_by_eid.get(a.get("subj_entity_id"))
                a["obj_blurb"] = blurb_by_eid.get(a.get("obj_entity_id"))
                a["location_blurb"] = blurb_by_eid.get(a.get("primary_location_entity_id"))

    # Collapse-only corpora never promote statements into assessments, so the
    # assessment-based timeline above comes back empty even when the library is
    # full of DATED statements (deposition dates, flight-log dates). Fall back to
    # those directly so the Timeline tab shows real events. (Same fix shape as
    # the entity story's mention fallback.)
    if not assessments:
        fb = _timeline_from_statements(library_id, limit)
        return {"events": fb, "count": len(fb)}

    # Apply learned anaphora bindings (placeholder → specific entity).
    _apply_bindings(assessments)
    _enrich_display_names(assessments)

    # Clean numeric ids for JSON
    return {"events": [_clean(a) for a in assessments], "count": len(assessments)}


_COURT_BOILERPLATE = re.compile(
    r"case\s+\d+:\d+-|page\s+\d+\s+of\s+\d+|document\s+\d+[-\d]*\s+filed|"
    r"filed\s+\d{2}/\d{2}/\d{2}|certificate\s+of\s+service|"
    r"\bmagna\d|hereby\s+certify|pursuant\s+to", re.I)


def _timeline_from_statements(library_id: int, limit: int = 200) -> list[dict]:
    """Timeline events from DATED statements when no assessments exist. Each
    dated statement becomes a TLEvent: subj/predicate/obj when the statement has
    resolved speaker/object entities, else the readable statement text as the
    line. Deduped by (date, text-prefix); quality-gated to clean prose."""
    out: list[dict] = []
    with get_connection() as conn:
        doc_ids = _library_member_ids(conn, library_id, "document") or [-1]
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (s.event_time::date, LEFT(COALESCE(s.summarized_text, s.raw_text), 70))
                       s.statement_id, s.predicate, s.raw_text, s.summarized_text,
                       s.event_time, s.document_id, d.title,
                       s.speaker_entity_id, se.canonical_name, se.canonical_type,
                       s.object_entity_id, oe.canonical_name, oe.canonical_type
                  FROM statements s
                  LEFT JOIN documents d  ON d.document_id = s.document_id
                  LEFT JOIN document_paragraphs p ON p.paragraph_id = s.paragraph_id
                  LEFT JOIN entities se ON se.entity_id = s.speaker_entity_id
                  LEFT JOIN entities oe ON oe.entity_id = s.object_entity_id
                 WHERE s.document_id = ANY(%s)
                   AND s.event_time IS NOT NULL
                   AND s.event_time::date >= DATE '1900-01-01'
                   AND s.event_time::date <= DATE '2035-01-01'
                   AND COALESCE(p.metadata->>'quality_band', 'pass') IN ('pass', 'record')
                 -- DESC so the LIMIT keeps the MOST RECENT events. Ascending
                 -- capped the timeline at the earliest N statements, so a
                 -- court-records topic (1969-2024) never surfaced its pivotal
                 -- recent events — they fell past the limit.
                 ORDER BY s.event_time::date DESC,
                          LEFT(COALESCE(s.summarized_text, s.raw_text), 70)
                 LIMIT %s
                """,
                (doc_ids, limit),
            )
            for r in cur.fetchall():
                (sid, pred, raw, summ, et, did, dtitle,
                 sp_eid, sp_name, sp_type, ob_eid, ob_name, ob_type) = r
                text = (summ or raw or "").strip()
                # Court-document boilerplate carries a filing date but is not an
                # event — case captions, page/doc stamps, certificates. Skip it
                # unless the statement resolved to real subject+object entities.
                if not (sp_name and ob_name) and _COURT_BOILERPLATE.search(text):
                    continue
                # A row headlines the parsed triple ONLY when it's a COMPLETE
                # fact (subject AND object resolved). A degenerate triple —
                # "Carver entered ?" mangled out of a docket-metadata line —
                # is worse than useless, so we headline the faithful SOURCE text
                # instead. Source is truth; the collapse triple is a lossy
                # interpretation shown only when it's actually clean.
                has_edge = bool(sp_name and ob_name)
                et_disp = et.isoformat() if et else None
                text_headline = None
                # Court docket lines are structured metadata, not prose — parse
                # them into a clean dated event ("Reply Memorandum of Law re:
                # Motion to Quash — Evan Fields") instead of the mangled triple.
                docket = None if has_edge else parse_docket(text)
                if docket:
                    if docket.get("date"):
                        et_disp = docket["date"]        # the docket date is the event
                    text_headline = docket["summary"]
                elif not has_edge:
                    # not a docket line and no clean fact — the source must still
                    # SAY something; drop fragments/OCR gibberish that only had a date.
                    # OCR-table soup slips a stray year through and floats to the top
                    # of the timeline, so also reject the tell-tale artifacts: (cid:N)
                    # PDF glyph fallbacks, percent-glued-to-letters ("%ow"), and
                    # adjacent decimal pairs ("0,02 ... 41.19") that mark tabular rows.
                    if len(text) < 25 or re.search(
                        r"[a-z]\)[a-z]|#:|Id \d{3,}|[A-Za-z]{2}\d[a-z;]"
                        r"|\(cid:|%[A-Za-z]|\d+[.,]\d+\D{0,40}\d+[.,]\d+", text):
                        continue
                    text_headline = text[:180]
                out.append({
                    "assessment_id": sid,            # unique key for the UI + expand
                    "assessment_tier": "docket" if docket else "statement",
                    "subj": sp_name if has_edge else text_headline,
                    "subj_type": sp_type if has_edge else None,
                    "subj_entity_id": sp_eid if has_edge else None,
                    "predicate_edge_type": (pred or "") if has_edge else "",
                    "obj": ob_name if has_edge else None,
                    "obj_type": ob_type if has_edge else None,
                    "obj_entity_id": ob_eid if has_edge else None,
                    "obj_value": None,
                    "display_date": et_disp,
                    "display_date_kind": "occurred",
                    "location_name": None,
                    "corroborated_by_count": 0,
                    "contradicted_by_count": 0,
                    "sources": [{
                        "statement_id": sid, "raw_text": raw,
                        "document_id": did, "document_title": dtitle,
                        "paragraph_text": text or None,
                    }],
                })
    out.sort(key=lambda e: e["display_date"] or "", reverse=True)
    return out


def _apply_bindings(assessments: list[dict]) -> None:
    """For each assessment whose subject or object is a placeholder
    ("House", "the ranch", ...), look up a learned binding scoped to the
    document the underlying statement came from. If found, replace
    obj_name/subj_name in-place and tag the assessment so the UI can
    surface the audit trail.

    Mutates `assessments` in place. Silent on errors — failure to resolve
    just leaves the original name."""
    for a in assessments:
        try:
            sources = a.get("sources") or []
            doc_ids = {s.get("document_id") for s in sources if s.get("document_id")}
            if not doc_ids:
                continue
            for side in ("subj", "obj"):
                name = a.get(side) or a.get(f"{side}_name")
                if not name:
                    continue
                # Try each document the assessment touches; first hit wins.
                bound = None
                for doc_id in doc_ids:
                    b = anaphora_resolver.lookup_binding(name, doc_id)
                    if b:
                        bound = (b, doc_id)
                        break
                if bound is None:
                    continue
                (entity_id, confidence, binding_id), doc_id = bound
                # Fetch the resolved entity's canonical_name once.
                with get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT canonical_name, canonical_type, base60_address FROM entities WHERE entity_id=%s",
                            (entity_id,),
                        )
                        row = cur.fetchone()
                if not row:
                    continue
                resolved_name, resolved_type, base60 = row
                a[f"{side}_original"] = name
                a[f"{side}"] = resolved_name
                if side == "subj":
                    a["subj_entity_id"] = entity_id
                    a["subj_type"] = resolved_type
                else:
                    a["obj_entity_id"] = entity_id
                    a["obj_type"] = resolved_type
                a.setdefault("resolutions", []).append({
                    "side": side,
                    "from": name,
                    "to": resolved_name,
                    "entity_id": entity_id,
                    "base60_address": base60,
                    "confidence": confidence,
                    "binding_id": binding_id,
                    "via_document_id": doc_id,
                })
                # Record the audit application (fire-and-forget).
                try:
                    anaphora_resolver.record_application(
                        binding_id=binding_id, assessment_id=a.get("assessment_id")
                    )
                except Exception:
                    pass
        except Exception as e:
            log.debug("binding application skipped on assessment %s: %s",
                      a.get("assessment_id"), e)


@router.get("/timeline/master")
def master_timeline(limit: int = 200, offset: int = 0,
                    library_id: Optional[int] = None,
                    entity_id: Optional[int] = None,
                    from_date: Optional[str] = None,
                    to_date: Optional[str] = None,
                    order: str = "desc"):
    """Cross-library MASTER timeline — every dated, entity-anchored event across
    all topics in one chronological spine. Quality-gated the same way the topic
    timelines are (grounded event dates, clean paragraphs, a real person/org
    actor, no reclassified-to-topic junk). Each event is tagged with the
    libraries (topics) its source document belongs to, so the UI can colour /
    filter by topic. Optional filters: library_id, entity_id, from_date,
    to_date (ISO). This is the spine; a topic timeline is a filtered lens on it.
    """
    order_sql = "ASC" if str(order).lower() == "asc" else "DESC"
    where = ["s.event_time IS NOT NULL",
             "s.event_time::date >= DATE '1900-01-01'",
             "s.event_time::date <= DATE '2100-01-01'",
             # real actor: a person/organization speaker or object
             "(se.canonical_type IN ('person','organization') "
             " OR oe.canonical_type IN ('person','organization'))",
             "COALESCE(dp.metadata->>'quality_band','pass') IN ('pass','record')",
             "char_length(s.raw_text) >= 40"]
    params: list[Any] = []
    if from_date:
        where.append("s.event_time::date >= %s"); params.append(from_date)
    if to_date:
        where.append("s.event_time::date <= %s"); params.append(to_date)
    if entity_id is not None:
        where.append("(s.speaker_entity_id = %s OR s.object_entity_id = %s)")
        params.extend([entity_id, entity_id])
    if library_id is not None:
        where.append("EXISTS (SELECT 1 FROM library_members lm "
                     " WHERE lm.member_id = s.document_id AND lm.member_kind='document' "
                     "   AND lm.library_id = %s)")
        params.append(library_id)
    where_sql = " AND ".join(where)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT DISTINCT ON (s.event_time::date, LEFT(coalesce(s.summarized_text, s.raw_text),70))
                          s.statement_id, s.event_time::date AS date, s.predicate,
                          coalesce(s.summarized_text, s.raw_text) AS text,
                          s.document_id, d.title,
                          s.speaker_entity_id, se.canonical_name,
                          s.object_entity_id, oe.canonical_name,
                          (SELECT array_agg(DISTINCT lm.library_id)
                             FROM library_members lm
                            WHERE lm.member_id = s.document_id
                              AND lm.member_kind='document') AS library_ids
                     FROM statements s
                     LEFT JOIN documents d ON d.document_id = s.document_id
                     LEFT JOIN entities se ON se.entity_id = s.speaker_entity_id
                     LEFT JOIN entities oe ON oe.entity_id = s.object_entity_id
                     LEFT JOIN document_paragraphs dp ON dp.paragraph_id = s.paragraph_id
                    WHERE {where_sql}
                    ORDER BY s.event_time::date {order_sql},
                             LEFT(coalesce(s.summarized_text, s.raw_text),70)
                    LIMIT %s OFFSET %s""",
                (*params, limit, offset),
            )
            rows = cur.fetchall()
            # resolve library titles for the tags
            lib_ids = sorted({lid for r in rows for lid in (r[10] or [])})
            titles: dict[int, str] = {}
            if lib_ids:
                cur.execute(
                    "SELECT library_id, COALESCE(notes::jsonb->>'display_title', focal_term, canonical_name) "
                    "FROM libraries WHERE library_id = ANY(%s)", (lib_ids,))
                titles = {r[0]: r[1] for r in cur.fetchall()}

    events = []
    for r in rows:
        (sid, date, pred, text, doc_id, dtitle,
         sp_eid, sp_name, ob_eid, ob_name, lib_arr) = r
        events.append({
            "statement_id": sid,
            "date": date.isoformat() if date else None,
            "predicate": (pred or "").replace("_", " "),
            "text": (text or "").strip()[:280],
            "document_id": doc_id, "document_title": dtitle,
            "subj_entity_id": sp_eid, "subj_name": sp_name,
            "obj_entity_id": ob_eid, "obj_name": ob_name,
            "topics": [{"library_id": lid, "title": titles.get(lid)}
                       for lid in (lib_arr or [])],
        })
    return {"events": events, "count": len(events),
            "offset": offset, "limit": limit, "order": order_sql.lower()}


@router.get("/libraries/{library_id}/coverage")
def library_coverage(library_id: int):
    """Completeness: how much of this library's known document set we've actually
    ingested and collapsed. `collapsed` = docs that carry extracted intelligence;
    `pending` = queued but not yet acquired. Makes 'what are we missing' visible."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT acquisition_status, COUNT(*) FROM bibliographic_references "
                        "WHERE library_id=%s GROUP BY 1", (library_id,))
            by_status = {k: v for k, v in cur.fetchall()}
            total_refs = sum(by_status.values())
            cur.execute("SELECT COUNT(DISTINCT member_id) FROM library_members "
                        "WHERE library_id=%s AND member_kind='document'", (library_id,))
            ingested = cur.fetchone()[0]
            cur.execute("""SELECT COUNT(DISTINCT dm.document_id) FROM document_mentions dm
                             JOIN library_members lm ON lm.member_id=dm.document_id
                              AND lm.library_id=%s""", (library_id,))
            collapsed = cur.fetchone()[0]
            cur.execute("SELECT notes::jsonb->>'universe_estimate' FROM libraries WHERE library_id=%s",
                        (library_id,))
            row = cur.fetchone()
    universe = int(row[0]) if row and row[0] else None
    denom = universe or total_refs or 1
    return {
        "total_refs": total_refs, "ingested": ingested, "collapsed": collapsed,
        "pending": by_status.get("pending", 0),
        "universe_estimate": universe,
        "coverage_pct": round(100 * collapsed / denom),
        "by_status": by_status,
    }


@router.get("/libraries/{library_id}/flights")
def library_flights(library_id: int, year_from: int | None = None,
                    year_to: int | None = None):
    """Flight-log network as GeoJSON — routes (weighted LineStrings) + airports
    (Points sized by traffic). Extracted from the flight-log documents and
    geocoded by IATA/ICAO. Optional year range filters the legs. This is the
    map layer that turns 'who flew where' into a picture."""
    where = "WHERE library_id = %s"
    params: list = [library_id]
    if year_from:
        where += " AND EXTRACT(YEAR FROM flight_date) >= %s"; params.append(year_from)
    if year_to:
        where += " AND EXTRACT(YEAR FROM flight_date) <= %s"; params.append(year_to)
    routes, airports = [], {}
    with get_connection() as conn:
        with conn.cursor() as cur:
            # routes aggregated by ordered airport pair
            cur.execute(
                f"""SELECT origin_code, dest_code, origin_name, dest_name,
                           origin_lat, origin_lon, dest_lat, dest_lon, COUNT(*) n
                      FROM flight_legs {where}
                     GROUP BY 1,2,3,4,5,6,7,8 ORDER BY n DESC""",
                params)
            for (oc, dc, on, dn, olat, olon, dlat, dlon, n) in cur.fetchall():
                routes.append({
                    "type": "Feature",
                    "geometry": {"type": "LineString",
                                 "coordinates": [[olon, olat], [dlon, dlat]]},
                    "properties": {"origin": oc, "dest": dc, "origin_name": on,
                                   "dest_name": dn, "flights": n},
                })
                for code, name, lat, lon in ((oc, on, olat, olon), (dc, dn, dlat, dlon)):
                    a = airports.setdefault(code, {"code": code, "name": name,
                                                   "lat": lat, "lon": lon, "flights": 0})
                    a["flights"] += n
    airport_feats = [{
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [a["lon"], a["lat"]]},
        "properties": {"code": a["code"], "name": a["name"], "flights": a["flights"]},
    } for a in airports.values()]
    return {
        "routes": {"type": "FeatureCollection", "features": routes},
        "airports": {"type": "FeatureCollection", "features": airport_feats},
        "route_count": len(routes), "airport_count": len(airport_feats),
    }


@router.get("/libraries/{library_id}/signals")
def library_signals(library_id: int, limit: int = 50):
    """Recent RSS signals routed to this library by the library_router worker.

    Each row is a single news article that mentioned the library's focal_term
    (or otherwise scored a match). Powers the "Live news" tab on /library/[id].
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ls.signal_rid, ls.matched_at, ls.match_kind, ls.match_score,
                       ls.keyword_hit,
                       s.signal_id, s.timestamp,
                       s.payload->>'title' AS title,
                       s.payload->>'source' AS source,
                       s.payload->>'link' AS link,
                       s.payload->>'category' AS category,
                       s.payload->>'thumbnail_url' AS thumbnail_url,
                       s.payload->>'summary' AS summary
                  FROM library_signals ls
                  JOIN signals s ON s.rid = ls.signal_rid
                 WHERE ls.library_id = %s
                 ORDER BY s.timestamp DESC NULLS LAST
                 LIMIT %s
                """,
                (library_id, limit),
            )
            rows = []
            for r in cur.fetchall():
                rows.append({
                    "signal_rid": r[0],
                    "matched_at": r[1].isoformat() if r[1] else None,
                    "match_kind": r[2],
                    "match_score": r[3],
                    "keyword_hit": r[4],
                    "signal_id": r[5],
                    "timestamp": r[6].isoformat() if r[6] else None,
                    "headline": r[7],
                    "source": r[8],
                    "link": r[9],
                    "category": r[10],
                    "thumbnail_url": r[11],
                    "summary": (r[12] or "")[:200],
                })
    return {"signals": rows, "count": len(rows)}


@router.get("/libraries/{library_id}/events")
def library_events(library_id: int, limit: int = 200):
    with get_connection() as conn:
        evt_ids = _library_member_ids(conn, library_id, "event")
        if not evt_ids:
            return {"events": [], "count": 0}
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT event_id, event_type, title, event_time, event_end_time,
                       temporal_precision, latitude, longitude, location_entity_id,
                       actor_entity_ids, target_entity_ids, confidence,
                       base60_address, source_count
                  FROM events WHERE event_id = ANY(%s)
                 ORDER BY event_time NULLS LAST LIMIT %s
                """,
                (evt_ids, limit),
            )
            rows = [_clean(r) for r in _rows_to_dicts(cur)]
    return {"events": rows, "count": len(rows)}


# ─── Entities ─────────────────────────────────────────────────────────

@router.get("/entities/{entity_id}")
def get_entity_card(entity_id: int, lens: Optional[str] = None):
    """Entity dossier. When `lens` is set (e.g. lens=library/3), the
    fact_count/event_count are restricted to facts/events with source
    provenance inside that lens. The entity's canonical metadata
    (name, type, aliases, identifiers, etc.) is always canonical —
    only the activity counts change with the lens.

    Follows entity_redirects: a stale link to a merged-away entity
    silently resolves to the canonical survivor. The response includes
    a redirected_from field so the UI can show the audit trail."""
    original_id = entity_id
    with get_connection() as conn:
        entity_id = _follow_entity_redirect(conn, entity_id)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.entity_id, e.tenant_id, e.canonical_name, e.canonical_type,
                       e.entity_kind, e.base60_address, e.concept_entity_id,
                       e.aliases, e.identifiers, e.signal_count, e.source_kind,
                       e.completeness_score, e.completeness_tier,
                       e.first_seen, e.last_seen, e.reality_status,
                       c.canonical_name AS concept_name,
                       c.base60_address AS concept_address
                  FROM entities e
                  LEFT JOIN entities c ON c.entity_id = e.concept_entity_id
                 WHERE e.entity_id = %s
                """,
                (entity_id,),
            )
            rows = _rows_to_dicts(cur)
            if not rows:
                raise HTTPException(404, f"entity {entity_id} not found")
            ent = _clean(rows[0])

            # KB description (Wikipedia stub, ~1 line). Try exact match
            # first, then a token match (substrate's "Marlow" → KB's
            # "Kevin Marlow" if it's the unique multi-word match).
            cname = ent.get("canonical_name") or ""
            cur.execute(
                """
                SELECT description, source_id, canonical_name
                  FROM entity_kb_cache
                 WHERE LOWER(canonical_name) = LOWER(%s)
                   AND description IS NOT NULL AND description <> ''
                 ORDER BY LENGTH(description) DESC
                 LIMIT 1
                """,
                (cname,),
            )
            kb = cur.fetchone()
            if not kb and cname and " " not in cname.strip():
                # Try fuller form: "% <name>" then "<name> %"
                cur.execute(
                    """
                    SELECT description, source_id, canonical_name
                      FROM entity_kb_cache
                     WHERE (canonical_name ILIKE %s OR canonical_name ILIKE %s)
                       AND POSITION(' ' IN canonical_name) > 0
                       AND description IS NOT NULL AND description <> ''
                     ORDER BY LENGTH(description) DESC
                     LIMIT 2
                    """,
                    (f"% {cname}", f"{cname} %"),
                )
                matches = cur.fetchall()
                # Only adopt if there's exactly one fuller form
                # (otherwise we don't know which is right).
                if len(matches) == 1:
                    kb = matches[0]
            if kb:
                ent["description"] = kb[0]
                ent["description_source"] = kb[1]
                if kb[2] and kb[2] != cname:
                    ent["kb_canonical_name"] = kb[2]

            # Structured addresses (entity_addresses table). Surfaces
            # FEC-disclosed addresses, geocoder hits, and any source that
            # ran through the address admission path.
            #
            # Filter out FEC committee jurisdiction noise — these are state
            # / district metadata that the FEC ingest pipeline writes into
            # entity_addresses as if they were locations. They lack a real
            # street component AND lack GPS coords, so requiring at least
            # one of those drops the noise without dropping real addresses.
            # We also exclude the address_type values that are known to be
            # jurisdictional rather than locational.
            cur.execute(
                """
                SELECT address_id, address_type, raw_address,
                       street, city, region, postal_code, country,
                       lat, lon, source, confidence,
                       valid_from, valid_to
                  FROM entity_addresses
                 WHERE entity_id = %s
                   AND (UPPER(COALESCE(address_type, '')) NOT IN
                        ('BRANCH', 'DISTRICT', 'JURISDICTION',
                         'COMMITTEE_STATE', 'FEC_METADATA'))
                   AND (
                         (street IS NOT NULL AND street <> '')
                      OR (lat IS NOT NULL AND lon IS NOT NULL)
                      OR (postal_code IS NOT NULL AND postal_code <> '')
                   )
                 ORDER BY (valid_to IS NULL) DESC, valid_to DESC NULLS FIRST,
                          confidence DESC NULLS LAST,
                          address_id DESC
                 LIMIT 25
                """,
                (entity_id,),
            )
            ent["addresses"] = []
            for ar in cur.fetchall():
                ent["addresses"].append({
                    "address_id": ar[0],
                    "address_type": ar[1],
                    "raw_address": ar[2],
                    "street": ar[3],
                    "city": ar[4],
                    "region": ar[5],
                    "postal_code": ar[6],
                    "country": ar[7],
                    "lat": float(ar[8]) if ar[8] is not None else None,
                    "lon": float(ar[9]) if ar[9] is not None else None,
                    "source": ar[10],
                    "confidence": float(ar[11]) if ar[11] is not None else None,
                    "valid_from": ar[12].isoformat() if ar[12] else None,
                    "valid_to": ar[13].isoformat() if ar[13] else None,
                })

            # Pull every structured-profile finding for this entity and
            # merge their data blobs into a single canonical view. We use
            # this for both the avatar AND the type-aware structured
            # profile section. Wikipedia is preferred over Wikidata when
            # both have the same key, because Wikipedia summaries are
            # human-readable while Wikidata is raw fact records.
            ent["avatar_url"] = None
            ent["avatar_caption"] = None
            ent["avatar_source"] = None
            ent["structured_profile"] = []
            cur.execute(
                """
                SELECT f.source, f.data
                  FROM investigation_findings f
                  JOIN entity_investigations i
                    ON i.investigation_id = f.investigation_id
                 WHERE i.entity_id = %s
                   AND f.source IN ('wikipedia', 'wikidata')
                   AND f.data IS NOT NULL
                 ORDER BY
                   -- Prefer wikipedia (richer human prose) over wikidata
                   CASE f.source WHEN 'wikipedia' THEN 0 ELSE 1 END,
                   f.relevance_score DESC NULLS LAST,
                   f.created_at DESC
                """,
                (entity_id,),
            )
            merged: dict = {}
            avatar_source = None
            avatar_caption = None
            for src, data in cur.fetchall():
                if not isinstance(data, dict):
                    continue
                # First merge wins per key (Wikipedia first thanks to ORDER BY)
                for k, v in data.items():
                    if k not in merged or merged[k] in (None, "", [], {}):
                        merged[k] = v
                # Capture the first usable avatar source we encounter
                if avatar_source is None and (
                    data.get("thumbnail") or data.get("thumbnail_url")
                    or data.get("image")
                ):
                    avatar_source = src
                    avatar_caption = (
                        data.get("description") or data.get("summary")
                    )

            if merged:
                thumb = merged.get("thumbnail") or merged.get("thumbnail_url")
                image_name = merged.get("image")
                if thumb:
                    ent["avatar_url"] = thumb
                elif image_name:
                    from urllib.parse import quote
                    ent["avatar_url"] = (
                        f"https://commons.wikimedia.org/wiki/Special:FilePath/"
                        f"{quote(image_name)}?width=300"
                    )
                ent["avatar_source"] = avatar_source
                if avatar_caption and len(avatar_caption) > 200:
                    avatar_caption = avatar_caption[:197] + "…"
                ent["avatar_caption"] = avatar_caption

                # Type-aware structured profile — only the fields that
                # displayvocab.type_profiles says are meaningful for this
                # entity's canonical_type (and subtype, if known).
                from core.display.profile_routing import project_profile
                ent["structured_profile"] = project_profile(
                    ent.get("canonical_type"),
                    ent.get("subtype") or ent.get("entity_kind"),
                    merged,
                )

            # Lens-aware activity counts
            lens_clause, lens_params = _lens_assessment_clause(conn, lens)
            cur.execute(
                f"""
                SELECT COUNT(*) FROM assessments a
                 WHERE (a.subj_entity_id = %s OR a.obj_entity_id = %s)
                       {lens_clause}
                """,
                (entity_id, entity_id, *lens_params),
            )
            ent["fact_count"] = int(cur.fetchone()[0] or 0)
            # Events: when a lens is active, restrict to events whose
            # originating_statement_ids overlap the lens's docs/signals.
            if lens_clause:
                cur.execute(
                    f"""
                    SELECT COUNT(*) FROM events e
                     WHERE (%s = ANY(e.actor_entity_ids) OR %s = ANY(e.target_entity_ids))
                       AND EXISTS (
                         SELECT 1 FROM statements s
                          WHERE s.statement_id = ANY(e.originating_statement_ids)
                            AND (s.document_id = ANY(%s) OR s.signal_rid = ANY(%s))
                       )
                    """,
                    (entity_id, entity_id, lens_params[0], lens_params[1]),
                )
            else:
                cur.execute(
                    "SELECT COUNT(*) FROM events "
                    " WHERE %s = ANY(actor_entity_ids) OR %s = ANY(target_entity_ids)",
                    (entity_id, entity_id),
                )
            ent["event_count"] = int(cur.fetchone()[0] or 0)
            if lens:
                ent["lens"] = lens

            if ent.get("concept_entity_id"):
                ent["concept_parent"] = {
                    "entity_id":     ent["concept_entity_id"],
                    "canonical_name": ent.pop("concept_name", None),
                    "base60_address": ent.pop("concept_address", None),
                }
            else:
                ent.pop("concept_name", None)
                ent.pop("concept_address", None)
    if original_id != entity_id:
        ent["redirected_from"] = original_id

    # Federation Phase 2 — look up the network view of this entity. Reads
    # the local federated_entities table directly so it works whether this
    # node IS the hub or has the hub's data replicated/cached locally.
    # When neither is the case, the table is just empty — no harm done.
    try:
        from core.federation.entity_publisher import canonical_key as _ekey
        if ent.get("canonical_name") and ent.get("canonical_type"):
            ckey = _ekey(ent["canonical_name"], ent["canonical_type"])
            with get_connection() as conn2:
                with conn2.cursor() as cur2:
                    cur2.execute(
                        """SELECT jsonb_array_length(publishing_nodes),
                                  network_signal_count
                             FROM federated_entities
                            WHERE canonical_entity_key = %s""",
                        (ckey,),
                    )
                    fed_row = cur2.fetchone()
            if fed_row:
                ent["federation"] = {
                    "n_nodes": int(fed_row[0] or 0),
                    "network_signal_count": int(fed_row[1] or 0),
                    "canonical_entity_key": ckey,
                }
    except Exception:
        pass

    return ent


def _normalize_image_url(source: str, data: dict) -> str | None:
    """Extract a renderable image URL from a finding's data dict.
    Different sources stash images at different keys / formats; this
    collapses them into one resolved URL."""
    if not isinstance(data, dict):
        return None
    raw = (
        data.get("image")
        or data.get("image_url")
        or data.get("thumbnail_url")
        or data.get("photo_url")
    )
    if not raw or not isinstance(raw, str):
        return None
    # Already a full URL
    if raw.startswith(("http://", "https://")):
        return raw
    if raw.startswith("//"):
        return "https:" + raw
    if raw.startswith("/i/") and source == "duckduckgo":
        return "https://duckduckgo.com" + raw
    # Wikidata/Wikipedia: often returns a bare filename like
    # "Example_Person_official_portrait.jpg". Resolve through Wikimedia
    # Commons' Special:FilePath which redirects to the actual image.
    if source in ("wikidata", "wikipedia") and not raw.startswith("/"):
        from urllib.parse import quote
        # Wikimedia uses underscores for spaces in filenames
        fname = raw.replace(" ", "_")
        return f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(fname)}?width=300"
    return None


@router.get("/entities/{entity_id}/topic_memberships")
def get_entity_topic_memberships(
    entity_id: int, limit: int = 50,
    min_score: float = 1.0, include_weak: bool = False,
):
    """Topics that this entity belongs to.

    By default filters memberships with score < 1.0 so weak corpus-bleed
    associations don't pollute the dossier. `include_weak=true` returns
    every membership including ambient ones; `min_score=...` lets the
    caller pick a custom threshold (useful for federation queries that
    want only strong memberships).
    """
    threshold = 0.0 if include_weak else float(min_score)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.topic_id, t.canonical_name, t.canonical_type, t.topic_kind,
                       t.member_count, t.created_by, tm.score, tm.via_rule
                  FROM topic_membership tm
                  JOIN topics t ON t.topic_id = tm.topic_id
                 WHERE tm.member_kind = 'entity'
                   AND tm.member_id = %s
                   AND tm.evicted_at IS NULL
                   AND tm.score >= %s
                 ORDER BY tm.score DESC, t.member_count DESC NULLS LAST
                 LIMIT %s
                """,
                (entity_id, threshold, limit),
            )
            rows = cur.fetchall()

    # Federation enrichment — for each topic, look up the local
    # federated_topics row. Works whether this node IS the hub or has the
    # data cached. Empty when nothing is published.
    federation_data: dict[str, dict] = {}
    try:
        from core.federation.publisher import canonical_key
        keys = [canonical_key(r[1] or "", r[2] or "investigation") for r in rows]
        if keys:
            with get_connection() as conn3:
                with conn3.cursor() as cur3:
                    cur3.execute(
                        """SELECT canonical_topic_key,
                                  jsonb_array_length(publishing_nodes),
                                  network_member_count
                             FROM federated_topics
                            WHERE canonical_topic_key = ANY(%s)""",
                        (keys,),
                    )
                    for ckey, n_nodes, n_members in cur3.fetchall():
                        federation_data[ckey] = {
                            "n_nodes": int(n_nodes or 0),
                            "network_member_count": int(n_members or 0),
                        }
    except Exception:
        federation_data = {}

    items = []
    for r in rows:
        item = {
            "topic_id": r[0],
            "canonical_name": r[1],
            "topic_subtype": r[2],
            "topic_kind": r[3],
            "topic_member_count": r[4],
            "created_by": r[5],
            "membership_score": float(r[6]) if r[6] is not None else None,
            "via_rule": r[7],
            "base60_address": None,
        }
        # Federation enrichment
        if federation_data:
            try:
                from core.federation.publisher import canonical_key as _ck
                ckey = _ck(r[1] or "", r[2] or "investigation")
                fed = federation_data.get(ckey, {})
                item["federation"] = {
                    "n_nodes": fed.get("n_nodes", 0),
                    "network_member_count": fed.get("network_member_count", 0),
                }
            except Exception:
                pass
        items.append(item)
    return {"topics": items, "count": len(items)}


@router.get("/topics/{topic_id}")
def get_topic_card(topic_id: int, member_limit: int = 100):
    """Single-topic detail used by the /topic/[id] page.

    Pulls topic identity + member rows + federation corroboration. Member
    rows include both entity and place kinds (and any other member_kind
    rows present), sorted by membership score descending. Empty/optional
    fields are returned as null so the front-end can render conditionally.
    """
    from services.api.src._node_redirect import resolve_node_or_410
    with get_connection() as conn:
        resolve_node_or_410(conn, table="topics", id_col="topic_id", id_value=topic_id)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT topic_id, canonical_name, canonical_type, topic_kind,
                       member_count, created_by, base60_address,
                       publish_membership_policy, materialized_at, updated_at
                  FROM topics
                 WHERE topic_id = %s
                """,
                (topic_id,),
            )
            r = cur.fetchone()
            if not r:
                raise HTTPException(404, "Topic not found")
            topic = {
                "topic_id": r[0],
                "canonical_name": r[1],
                "canonical_type": r[2],
                "topic_kind": r[3],
                "member_count": r[4],
                "created_by": r[5],
                "base60_address": r[6],
                "publish_membership_policy": r[7],
                "materialized_at": r[8].isoformat() if r[8] else None,
                "updated_at": r[9].isoformat() if r[9] else None,
            }

            # Members — join into entities/places so the UI gets names
            cur.execute(
                """
                SELECT tm.member_kind, tm.member_id, tm.score, tm.via_rule,
                       e.canonical_name, e.canonical_type
                  FROM topic_membership tm
             LEFT JOIN entities e
                    ON tm.member_kind = 'entity' AND e.entity_id = tm.member_id
                 WHERE tm.topic_id = %s
                   AND tm.evicted_at IS NULL
                 ORDER BY tm.score DESC NULLS LAST
                 LIMIT %s
                """,
                (topic_id, member_limit),
            )
            members = []
            for m in cur.fetchall():
                members.append({
                    "member_kind": m[0],
                    "member_id": m[1],
                    "score": float(m[2]) if m[2] is not None else None,
                    "via_rule": m[3],
                    "canonical_name": m[4],
                    "canonical_type": m[5],
                })

            # Federation corroboration
            federation = None
            try:
                from core.federation.publisher import canonical_key as _ck
                ckey = _ck(topic["canonical_name"] or "",
                          topic["canonical_type"] or "investigation")
                cur.execute(
                    """SELECT n_nodes, network_member_count
                         FROM federated_topics
                        WHERE canonical_topic_key = %s""",
                    (ckey,),
                )
                fed = cur.fetchone()
                if fed:
                    federation = {
                        "canonical_topic_key": ckey,
                        "n_nodes": int(fed[0] or 0),
                        "network_member_count": int(fed[1] or 0),
                    }
            except Exception:
                pass

    topic["members"] = members
    topic["federation"] = federation
    return topic


@router.get("/entities/{entity_id}/contrast")
def get_entity_contrast(entity_id: int):
    """Multi-source contrast view for the dossier.

    Returns sources_claims (high-confidence matches) AND possible_matches
    (rows whose source-returned name looks like a different real-world
    entity sharing the name — SAM FRANKLIN ROWAN when seed is Sam
    Rowan, etc.). The split uses the same name-match scorer as
    _store_findings so confidence is consistent across the pipeline.

    Each row:
      source         connector name (wikipedia, wikidata, courtlistener, ...)
      source_label   display label
      claim          1-3 sentence framing claim (extracted from finding)
      source_url     link out to the original
      confidence     0-1 disambiguation × source confidence
      match_score    0-1 pure name-shape match (for UI tooltip)
      finding_id     for click-deeper / 'research more on this' wiring
    """
    from core.workers.investigation import _score_name_match

    verified: list[dict] = []
    possible: list[dict] = []
    with get_connection() as conn:
        entity_id = _follow_entity_redirect(conn, entity_id)
        with conn.cursor() as cur:
            # Get the entity's canonical name as the seed for disambiguation.
            cur.execute(
                "SELECT canonical_name FROM entities WHERE entity_id = %s",
                (entity_id,),
            )
            r = cur.fetchone()
            seed_name = r[0] if r else ""

            # Pull the most recent investigation's findings for this entity,
            # ordered so we get the highest-confidence finding per source.
            cur.execute(
                f"""
                SELECT f.finding_id, f.source, f.finding_type, f.title,
                       f.data, f.confidence
                  FROM investigation_findings f
                  {_FINDING_ATTRIBUTIONS_FROM}
                 WHERE {_FINDING_ATTRIBUTIONS_WHERE}
                 ORDER BY f.source, f.confidence DESC NULLS LAST, f.finding_id DESC
                """,
                (entity_id, entity_id, entity_id, entity_id),
            )
            seen_sources: set[str] = set()
            for finding_id, source, ftype, title, data, confidence in cur.fetchall():
                if source in seen_sources:
                    continue
                seen_sources.add(source)
                claim, source_url = _extract_claim(source, ftype, title, data)
                if not claim:
                    continue
                # Recompute the match score in case the row was stored
                # before the disambiguation pass landed (June 4). New
                # findings will already reflect this in their confidence
                # column; this re-derives so old + new rows are tiered
                # consistently in the response.
                match_score = _score_name_match(seed_name, title)
                row = {
                    "source": source,
                    "source_label": _source_label(source),
                    "claim": claim[:600],
                    "source_url": source_url,
                    "confidence": float(confidence) if confidence is not None else None,
                    "match_score": round(match_score, 2),
                    "finding_id": finding_id,
                    "finding_type": ftype,
                }
                # Split threshold: 0.50 keeps authoritative sources +
                # likely-same-person rows in the main panel; sub-0.50
                # (mostly company suffixes, extra middle names, generic
                # surname-only matches) gets pushed to 'Possible matches'.
                if match_score >= 0.50:
                    verified.append(row)
                else:
                    possible.append(row)

    verified.sort(key=lambda r: _source_priority(r["source"]))
    possible.sort(key=lambda r: _source_priority(r["source"]))
    return {
        "entity_id": entity_id,
        "seed_name": seed_name,
        "sources_claims": verified,
        "possible_matches": possible,
        "count": len(verified),
        "possible_count": len(possible),
    }


def _extract_claim(source: str, ftype: str, title: Optional[str], data: Any) -> tuple[Optional[str], Optional[str]]:
    """Pick the most representative one-line claim a source makes about
    this entity. Per-source fields vary; we look in priority order:
       title (if descriptive) → data.summary → data.description →
       data.snippet → fallback to title even if generic.
    Also returns the source_url if we can find one in the finding data.
    """
    if not isinstance(data, dict):
        data = {}
    candidates = [
        data.get("summary"), data.get("description"), data.get("extract"),
        data.get("snippet"), data.get("excerpt"),
    ]
    claim = None
    for c in candidates:
        if isinstance(c, str) and len(c.strip()) >= 20:
            claim = c.strip()
            break
    if not claim and isinstance(title, str) and len(title.strip()) >= 5:
        # Drop a leading "Wikipedia: " or "CourtListener: " prefix so
        # the panel reads cleanly.
        claim = title.split(":", 1)[-1].strip() if ":" in title and title.startswith(_source_label(source) + ":") else title.strip()
    source_url = (
        data.get("source_url") or data.get("page_url") or data.get("url")
        or data.get("link") or data.get("citation_url")
    )
    return claim, source_url


_SOURCE_LABEL_MAP = {
    "wikipedia": "Wikipedia",
    "wikidata": "Wikidata",
    "courtlistener": "CourtListener (US courts)",
    "opensanctions": "OpenSanctions",
    "ofac": "OFAC Sanctions",
    "un_sanctions": "UN Sanctions",
    "opencorporates": "OpenCorporates",
    "fec": "FEC (US elections)",
    "congress": "US Congress",
    "loc": "Library of Congress",
    "github": "GitHub",
    "reddit": "Reddit",
    "wayback": "Internet Archive",
    "ny_dos": "NY Department of State",
    "ca_sos": "CA Secretary of State",
    "sunbiz_fl": "FL Division of Corporations",
    "google_books": "Google Books",
    "duckduckgo": "DuckDuckGo",
    "epa_echo": "EPA ECHO",
    "usaspending": "USAspending",
}


def _source_label(source: str) -> str:
    return _SOURCE_LABEL_MAP.get(source, source.replace("_", " ").title())


def _source_priority(source: str) -> int:
    """Lower = appears higher in the panel. Authoritative encyclopaedic
    sources first, then official government, then media/aggregators,
    then social/self-published. Within tiers, alphabetical."""
    p = {
        "wikipedia": 10, "wikidata": 11,
        "courtlistener": 20, "ofac": 21, "un_sanctions": 22, "opensanctions": 23,
        "fec": 30, "congress": 31, "loc": 32,
        "opencorporates": 40, "epa_echo": 41, "usaspending": 42,
        "ny_dos": 50, "ca_sos": 51, "sunbiz_fl": 52,
        "google_books": 60,
        "duckduckgo": 70, "wayback": 71,
        "github": 80, "reddit": 81,
    }
    return p.get(source, 99)


@router.get("/entities/{entity_id}/substrate")
def get_entity_substrate(entity_id: int):
    """Substrate view — the form_node and its form_concept_bindings.

    Surfaces the base-60 substrate layer the operator usually doesn't
    see: which form_node maps to this entity's canonical_name, and
    what OTHER concepts that same form binds to. Many bindings on a
    single form = the surface form is shared across distinct concepts
    (the persona-conflation pattern), which the Namesake panel makes
    actionable.

    Returns:
      entity_id, canonical_name
      form: { form_id, form_address, surface_form, signal_count } or null
      bindings: list of { concept_id, binding_source, binding_confidence,
                          concept_address, canonical_name, canonical_subtype,
                          promoted_to_entity_id, is_self }
    """
    with get_connection() as conn:
        entity_id = _follow_entity_redirect(conn, entity_id)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT canonical_name FROM entities WHERE entity_id = %s",
                (entity_id,),
            )
            r = cur.fetchone()
            if not r:
                raise HTTPException(404, f"entity {entity_id} not found")
            canonical_name = r[0] or ""

            # Look up the form by its normalized surface form. The
            # substrate uses lowercased normalization (per the LDU
            # capture-time path) — match by surface_form_normalized
            # rather than by hash so we don't have to recompute the
            # exact normalization scheme here.
            cur.execute(
                """
                SELECT form_id, form_address, surface_form, signal_count
                  FROM form_nodes
                 WHERE surface_form_normalized = LOWER(%s)
                 ORDER BY signal_count DESC
                 LIMIT 1
                """,
                (canonical_name,),
            )
            form_row = cur.fetchone()
            form = None
            bindings: list[dict] = []

            if form_row:
                form = {
                    "form_id": int(form_row[0]),
                    "form_address": form_row[1],
                    "surface_form": form_row[2],
                    "signal_count": int(form_row[3] or 0),
                }

                # Active bindings on this form
                cur.execute(
                    """
                    SELECT c.concept_id, b.binding_source, b.binding_confidence,
                           c.concept_address, c.canonical_name, c.canonical_subtype,
                           c.promoted_to_entity_id
                      FROM form_concept_bindings b
                      JOIN concept_nodes c ON c.concept_id = b.concept_id
                     WHERE b.form_id = %s AND b.active = TRUE
                     ORDER BY b.binding_confidence DESC, b.binding_id
                    """,
                    (form["form_id"],),
                )
                for row in cur.fetchall():
                    bindings.append({
                        "concept_id": int(row[0]),
                        "binding_source": row[1],
                        "binding_confidence": float(row[2]) if row[2] is not None else None,
                        "concept_address": row[3],
                        "canonical_name": row[4],
                        "canonical_subtype": row[5],
                        "promoted_to_entity_id": int(row[6]) if row[6] is not None else None,
                        "is_self": (int(row[6]) == entity_id) if row[6] is not None else False,
                    })

    return {
        "entity_id": entity_id,
        "canonical_name": canonical_name,
        "form": form,
        "bindings": bindings,
    }


@router.get("/entities/{entity_id}/personas")
def get_entity_personas(entity_id: int):
    """Propose persona clusters for an entity that may contain multiple
    distinct real-world referents under one entity_id.

    Each cluster maps 1:1 onto base-60 substrate (form_nodes → concept_
    nodes → form_concept_bindings per migration 093). On operator
    approval the split endpoint writes the new concept + binding so the
    form's resolution distribution is empirically grounded.

    Returns:
      {
        seed_name:    str            — entity's canonical_name
        cluster_count: int           — total clusters, including anchor
        clusters: [
          {
            cluster_id:        str   — 'A' is always the anchor
            label:             str   — proposed name for the new entity
            anchor:            bool  — True for the cluster that stays
            confidence:        float — 0..1 intra-cluster tightness
            canonical_subtype: str   — 'person' | 'organization' | ...
            subtype_path:      str   — 'entity.person' etc.
            identifiers:       dict  — QID, CIK, candidate_id, ...
            dominant_attrs:    dict  — jurisdiction, region, middle_name,
                                       is_corporate, dob_year — used as
                                       binding_evidence on the form→concept
                                       link when the operator approves.
            evidence_tier:     str   — A..G per the evidence model
            finding_ids:       [int] — used by the split endpoint
            findings:          [{    — full rows for the UI to render
              finding_id, source, source_label, finding_type,
              title, claim, source_url, confidence
            }]
          },
          ...
        ]
      }

    The UI hides the panel when cluster_count <= 1 (no conflation to
    show). The frontend wires each non-anchor cluster's "Split off →"
    button to POST /api/nodes/entity:{id}/split with finding_ids.
    """
    from core.workers.persona_clustering import cluster_findings, extract_meta_findings

    with get_connection() as conn:
        entity_id = _follow_entity_redirect(conn, entity_id)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT canonical_name, canonical_type "
                "FROM entities WHERE entity_id = %s",
                (entity_id,),
            )
            r = cur.fetchone()
            if not r:
                raise HTTPException(status_code=404, detail="entity not found")
            seed_name, entity_type = r[0] or "", r[1] or "person"

            cur.execute(
                f"""
                SELECT f.finding_id, f.source, f.finding_type, f.title,
                       f.data, f.confidence
                  FROM investigation_findings f
                  {_FINDING_ATTRIBUTIONS_FROM}
                 WHERE {_FINDING_ATTRIBUTIONS_WHERE}
                """,
                (entity_id, entity_id, entity_id, entity_id),
            )
            findings: list[dict] = []
            for fid, src, ftype, title, data, conf in cur.fetchall():
                findings.append({
                    "finding_id": int(fid) if fid else None,
                    "source": src,
                    "finding_type": ftype,
                    "title": title,
                    "data": data if isinstance(data, dict) else {},
                    "confidence": float(conf) if conf is not None else None,
                })

    clusters = cluster_findings(seed_name, findings,
                                canonical_entity_type=entity_type)
    meta_findings = extract_meta_findings(findings)

    # Inline each finding's UI-ready row (claim + source_url) so the
    # frontend can render without a second pass to /contrast.
    def _render_finding(f: dict) -> dict:
        claim, source_url = _extract_claim(
            f.get("source") or "",
            f.get("finding_type") or "",
            f.get("title"),
            f.get("data"),
        )
        return {
            "finding_id": f.get("finding_id"),
            "source": f.get("source"),
            "source_label": _source_label(f.get("source") or ""),
            "finding_type": f.get("finding_type"),
            "title": f.get("title"),
            "claim": (claim or "")[:600] if claim else None,
            "source_url": source_url,
            "confidence": f.get("confidence"),
        }

    out_clusters = []
    for c in clusters:
        d = c.to_dict()
        rendered = [_render_finding(f) for f in c.findings]
        d["findings"] = rendered
        d["findings_by_source"] = _group_findings_by_source(rendered)
        # Structured property facts (occupation, DOB, party, owner_of, ...)
        # pulled from raw finding.data with provenance per fact.
        d["properties"] = _extract_property_facts(c.findings)
        # Meta findings (sanctions negatives, search aggregates) are
        # facts about the anchor, not separate personas — attach them
        # here so they appear as e.g. "sanctions: not listed [OFAC]".
        if d.get("anchor") and meta_findings:
            d["properties"] = list(d["properties"]) + _meta_findings_to_facts(meta_findings)
        out_clusters.append(d)

    # Reconcile id-bearing org clusters against the entity graph. A cluster
    # carrying a STRONG org identifier (committee_id/ein/cik/uei/…) that already
    # resolves to a real entity is NOT a namesake of this person — it's a
    # distinct organization that references them (an FEC committee, a 990
    # nonprofit). It now lives as its own entity and shows in the Companies
    # panel, so move it OUT of the referents list into `resolved_orgs`. This
    # keeps the "Also called …" panel about genuine person-disambiguation
    # instead of relisting every committee as a possible alias of the person.
    from core.workers.persona_clustering import _ORG_ID_KEYS

    # Same-person fold: a non-anchor PERSON cluster whose name is token-identical
    # to the seed and carries NO identifier is the SAME person as the anchor —
    # the clustering only split it because a context attribute (jurisdiction,
    # event_year) differed, and context is not evidence of a different referent
    # when the name matches the seed exactly and nothing contradicts it. Fold
    # these into the anchor instead of parading N identical "VALE, DONALD J."
    # rows as separate referents. A generational suffix (Jr/Sr/III) or any
    # identifier makes a cluster genuinely distinct → NOT folded.
    import re as _re
    _SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

    def _name_key(s: str):
        toks = [t for t in _re.split(r"[^a-z0-9]+", (s or "").lower()) if t]
        suffix = frozenset(t for t in toks if t in _SUFFIXES)
        core = frozenset(t for t in toks if t not in _SUFFIXES and len(t) > 1)
        return core, suffix

    _seed_core, _seed_suffix = _name_key(seed_name)

    def _folds_into_seed(label: str, idents: dict, subtype: str) -> bool:
        if idents:                       # any identifier → treat as distinct
            return False
        if (subtype or "").lower() != "person":
            return False
        core, suffix = _name_key(label)
        if not core or not _seed_core:
            return False
        if suffix != _seed_suffix:        # Jr/Sr/III distinguishes referents
            return False
        return core == _seed_core or core <= _seed_core or _seed_core <= core

    namesakes, resolved_orgs = [], []
    anchor_d = None
    folded_count = 0
    folded_findings = 0
    with get_connection() as _conn2:
        with _conn2.cursor() as _cur2:
            for d in out_clusters:
                if d.get("anchor"):
                    anchor_d = d
                    namesakes.append(d)
                    continue
                idents = d.get("identifiers") or {}
                # (a) id-bearing org that already resolves to a real entity
                hit = None
                for k in _ORG_ID_KEYS:
                    v = idents.get(k)
                    if not v:
                        continue
                    _cur2.execute(
                        "SELECT entity_id, canonical_name, canonical_type "
                        "FROM entities "
                        "WHERE identifiers @> jsonb_build_object(%s, %s::text) "
                        "AND entity_id <> %s "
                        "ORDER BY signal_count DESC NULLS LAST LIMIT 1",
                        (k, str(v), entity_id),
                    )
                    row = _cur2.fetchone()
                    if row:
                        hit = {"entity_id": int(row[0]), "name": row[1],
                               "type": row[2], "via": {k: str(v)}}
                        break
                if hit:
                    d["resolved_entity_id"] = hit["entity_id"]
                    d["resolved_entity_name"] = hit["name"]
                    d["resolved_entity_type"] = hit["type"]
                    d["resolved_via"] = hit["via"]
                    resolved_orgs.append(d)
                    continue
                # (b) same-person context fragment → fold into the anchor
                if _folds_into_seed(d.get("label") or "", idents,
                                    d.get("canonical_subtype") or ""):
                    folded_count += 1
                    folded_findings += len(d.get("finding_ids") or [])
                    continue
                namesakes.append(d)

    if anchor_d is not None and folded_count:
        anchor_d["folded_same_name_count"] = folded_count
        anchor_d["folded_same_name_findings"] = folded_findings

    return {
        "entity_id": entity_id,
        "seed_name": seed_name,
        "cluster_count": len(namesakes),
        "clusters": namesakes,
        "resolved_orgs": resolved_orgs,
        "resolved_org_count": len(resolved_orgs),
        "folded_same_name_count": folded_count,
    }


# Structured property fields recognized across source data JSONBs.
# Maps raw data-key → (display predicate, value formatter, value_url builder).
#
# value formatter: takes raw value, returns display string (or None to skip)
# value_url builder: takes raw value, returns a URL the VALUE should
#                    navigate to when clicked. Distinct from source_url
#                    (which is the provenance — the source page where
#                    the fact came from). E.g. for 'official website':
#                    value clicks → the website, source chip clicks →
#                    the Wikidata page that asserts it.
# Both formatters may be None.
_FactFmt = Optional[Any]
_FACT_PROPERTIES: dict[str, tuple[str, _FactFmt, _FactFmt]] = {
    # Wikidata person/org facts
    "date_of_birth":      ("date of birth", None, None),
    "date_of_death":      ("date of death", None, None),
    "nationality":        ("nationality", None, None),
    "occupation":         ("occupation", None, None),
    "position_held":      ("position held", None, None),
    "employer":           ("employer", None, None),
    "educated_at":        ("educated at", None, None),
    "official_website":   ("official website", None, lambda v: str(v) if str(v).startswith("http") else None),
    "twitter_handle":     ("Twitter",
                            lambda v: f"@{v}" if v and not str(v).startswith("@") else str(v),
                            lambda v: f"https://twitter.com/{str(v).lstrip('@')}"),
    "instance_of":        ("type", None, None),
    "inception":          ("founded", None, None),
    # Wikidata place/country facts
    "population":         ("population", None, None),
    "area_km2":           ("area (km²)", None, None),
    "head_of_state":      ("head of state", None, None),
    "head_of_government": ("head of government", None, None),
    "capital":            ("capital", None, None),
    "continent":          ("continent", None, None),
    "currency":           ("currency", None, None),
    "official_language":  ("official language", None, None),
    "iso_3166":           ("ISO 3166", None, None),
    # Wikidata multi-value relations
    "owner_of":           ("owns", None, None),
    "owned_by":           ("owned by", None, None),
    "parent_org":         ("parent org", None, None),
    "subsidiary":         ("subsidiary", None, None),
    "founded_by":         ("founded by", None, None),
    "chairperson":        ("chairperson", None, None),
    "ceo":                ("CEO", None, None),
    "board_member":       ("board member", None, None),
    "spouse":             ("spouse", None, None),
    "child":              ("child", None, None),
    "father":             ("father", None, None),
    "mother":             ("mother", None, None),
    "sibling":            ("sibling", None, None),
    "hq_location":        ("headquarters", None, None),
    "operator":           ("operator", None, None),
    "manufacturer":       ("manufacturer", None, None),
    "country_of_origin":  ("country of origin", None, None),
    "located_in":         ("located in", None, None),
    # FEC person/committee facts
    "party":              ("party", None, None),
    "office":             ("office", None, None),
    "state":              ("state", None, None),
    "incumbent_challenge": ("incumbent status", None, None),
    "treasurer_name":     ("treasurer", None, None),
    "committee_type":     ("committee type", None, None),
    "designation":        ("committee designation", None, None),
    "first_file_date":    ("first FEC filing", None, None),
    # OpenCorporates / SunBiz / Companies House
    "company_number":     ("company number", None, None),
    "company_type":       ("company type", None, None),
    "incorporation_date": ("incorporation date", None, None),
    "jurisdiction_code":  ("jurisdiction", None, None),
    # Congress.gov
    "chamber":            ("chamber", None, None),
    "district":           ("district", None, None),
}

# Properties to skip even if present in data — they're metadata, not facts.
_FACT_SKIP_KEYS = frozenset({
    "wikidata_id", "qid", "candidate_id", "committee_id", "cik", "ein",
    "company_number_alt", "source_url", "page_url", "url", "link",
    "image", "image_url", "flag_image", "coat_of_arms", "thumbnail",
    "summary", "description", "extract", "snippet", "excerpt",
    "search_term", "entity_name", "candidate_name", "committee_name",
    "title", "case_name", "election_years",
})


# Override-aware findings query — used by Story / Personas / Contrast.
#
# A finding belongs to entity X if EITHER:
#   1. An operator override exists (finding_attributions) with
#      decision='reassigned' AND entity_id = X  — operator moved this
#      finding to entity X via the persona-split flow.
#   2. The default attribution applies — (f.entity_id = X OR
#      i.entity_id = X) AND no contradicting override exists. A finding
#      is excluded when its attribution is 'rejected' or 'reassigned' to
#      a DIFFERENT entity.
#
# Caller embeds these snippets and passes entity_id FOUR TIMES at the
# `%s` slots, in order. The helper returns the SQL strings (FROM-extra
# + WHERE) so the SELECT shape stays with the caller.
_FINDING_ATTRIBUTIONS_FROM = """
        JOIN entity_investigations i ON i.investigation_id = f.investigation_id
        LEFT JOIN finding_attributions fa
               ON fa.finding_id = f.finding_id AND fa.tenant_id = i.tenant_id
"""

_FINDING_ATTRIBUTIONS_WHERE = """
        (
          (fa.decision = 'reassigned' AND fa.entity_id = %s)
          OR (
            (f.entity_id = %s OR i.entity_id = %s)
            AND (fa.decision IS NULL OR fa.decision = 'confirmed'
                 OR (fa.decision = 'reassigned' AND fa.entity_id = %s))
          )
        )
"""


def _meta_findings_to_facts(meta_findings: list[dict]) -> list[dict]:
    """Convert meta findings (sanctions negatives, search aggregates)
    into anchor-level facts with provenance. Filtered out of clustering
    by persona_clustering._is_meta_finding; surfaced here instead."""
    facts: list[dict] = []
    # Dedupe by (predicate, source) so multiple ingest passes of the
    # same OFAC clearance don't produce 12 identical fact rows.
    seen: set[tuple[str, str]] = set()

    for f in meta_findings:
        source = (f.get("source") or "").lower()
        finding_type = (f.get("finding_type") or "").lower()
        title = f.get("title") or ""
        data = f.get("data") or {}
        if not isinstance(data, dict):
            data = {}

        source_url = (
            data.get("source_url") or data.get("page_url")
            or data.get("url") or data.get("link")
        )
        source_label = _source_label(source)

        # Sanctions sources with sanctioned=False
        if source in ("ofac", "un_sanctions", "opensanctions") \
                and data.get("sanctioned") is False:
            pred = "sanctions"
            value = f"not listed ({source_label})"
            key = (pred, source)
            if key in seen:
                continue
            seen.add(key)
            facts.append({
                "predicate": pred,
                "value": value,
                "value_url": None,
                "source": source,
                "source_label": source_label,
                "source_url": source_url,
            })
            continue

        # CourtListener / search-summary aggregates with a count payload
        if finding_type in ("court_summary", "case_summary"):
            total = data.get("total_cases") or data.get("total") or data.get("count")
            if total is not None:
                pred = "court mentions"
                value = f"{total} cases" if not isinstance(total, str) else str(total)
                key = (pred, source)
                if key in seen:
                    continue
                seen.add(key)
                facts.append({
                    "predicate": pred,
                    "value": value,
                    "value_url": source_url,
                    "source": source,
                    "source_label": source_label,
                    "source_url": source_url,
                })
                continue
        # LOC / DDG search-summary patterns can extend here as needed.
        if finding_type in ("loc_search_summary", "search_summary", "summary_count"):
            pred = "search summary"
            value = title.split(":", 1)[-1].strip() if ":" in title else (title or "summary")
            key = (pred, source)
            if key in seen:
                continue
            seen.add(key)
            facts.append({
                "predicate": pred,
                "value": value,
                "value_url": source_url,
                "source": source,
                "source_label": source_label,
                "source_url": source_url,
            })

    return facts


def _extract_property_facts(findings: list[dict]) -> list[dict]:
    """Walk a cluster's findings and emit structured property facts.

    Each fact is {predicate, value, source, source_label, source_url}
    with provenance. Multi-value Wikidata properties (owner_of, spouse,
    etc.) produce one fact per item. The same predicate from multiple
    sources is preserved — operator sees corroboration / divergence.

    Deduplicates exact (predicate, value, source) triples since
    re-ingested findings repeat the same data.
    """
    facts: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    for f in findings:
        data = f.get("data") or {}
        if not isinstance(data, dict):
            continue
        source = f.get("source") or ""
        source_label = _source_label(source)
        source_url = (
            data.get("source_url") or data.get("page_url")
            or data.get("url") or data.get("link") or data.get("citation_url")
        )

        for raw_key, raw_val in data.items():
            if raw_key in _FACT_SKIP_KEYS:
                continue
            if raw_key not in _FACT_PROPERTIES:
                continue
            predicate, formatter, value_url_builder = _FACT_PROPERTIES[raw_key]

            # Multi-value (list) properties
            values: list = raw_val if isinstance(raw_val, list) else [raw_val]
            for v in values:
                if v is None or v == "" or (isinstance(v, str) and not v.strip()):
                    continue
                raw_for_url = v
                if formatter:
                    v = formatter(v)
                    if v is None:
                        continue
                display = str(v).strip()
                if not display:
                    continue
                # value_url: where the VALUE navigates when clicked.
                # Distinct from source_url (the provenance / where the
                # FACT was asserted).
                value_url: Optional[str] = None
                if value_url_builder:
                    try:
                        value_url = value_url_builder(raw_for_url)
                    except Exception:
                        value_url = None
                # Heuristic fallback: any value that looks like a URL is
                # itself the destination.
                if not value_url and isinstance(display, str) and (
                    display.startswith("http://") or display.startswith("https://")
                ):
                    value_url = display
                key = (predicate, display.lower(), source)
                if key in seen:
                    continue
                seen.add(key)
                facts.append({
                    "predicate": predicate,
                    "value": display,
                    "value_url": value_url,    # NEW: value navigates here
                    "source": source,
                    "source_label": source_label,
                    "source_url": source_url,  # provenance
                })

    # Order: group by predicate while preserving source priority within each
    pred_order: list[str] = []
    by_pred: dict[str, list[dict]] = {}
    for f in facts:
        if f["predicate"] not in by_pred:
            pred_order.append(f["predicate"])
            by_pred[f["predicate"]] = []
        by_pred[f["predicate"]].append(f)
    ordered: list[dict] = []
    for p in pred_order:
        rows = by_pred[p]
        rows.sort(key=lambda r: _source_priority(r["source"]))
        ordered.extend(rows)
    return ordered


def _group_findings_by_source(findings: list[dict]) -> list[dict]:
    """Group cluster findings by source and dedupe identical claims.

    Cluster A on Sam Rowan today returns 39 findings — 8 Wikipedia
    + 8 Wikidata + 8 DuckDuckGo + ... — because every investigation pass
    re-surfaces the same data. Showing them as 39 flat rows is unreadable;
    the operator wants ONE Wikipedia row with the actual prose and a
    badge saying "×8 ingest passes".

    Within each source group, dedupe by (claim text or title), preserve
    the best confidence + first source_url + total count.

    Output: list of {source, source_label, count, unique_claims: [...]}
    ordered by source priority (Wikipedia/Wikidata first, social last).
    """
    if not findings:
        return []

    by_source: dict[str, dict] = {}
    for f in findings:
        src = f.get("source") or "unknown"
        bucket = by_source.setdefault(src, {
            "source": src,
            "source_label": f.get("source_label") or _source_label(src),
            "total_count": 0,
            "_by_key": {},
        })
        bucket["total_count"] += 1

        # Dedupe key: prefer claim text (deduplicates re-ingested
        # snippets); fall back to title (deduplicates "Wikipedia: X");
        # fall back to finding_id (preserves each row when totally
        # opaque).
        claim = (f.get("claim") or "").strip()
        title = (f.get("title") or "").strip()
        key = claim.lower() or title.lower() or f"fid:{f.get('finding_id')}"

        if key in bucket["_by_key"]:
            entry = bucket["_by_key"][key]
            entry["count"] += 1
            # Keep highest confidence; first source_url wins.
            if (f.get("confidence") or 0) > (entry.get("confidence") or 0):
                entry["confidence"] = f.get("confidence")
            if not entry.get("source_url") and f.get("source_url"):
                entry["source_url"] = f.get("source_url")
        else:
            # Strip duplicate source-label prefix from title so it doesn't
            # render as "Wikipedia: Wikipedia: Sam Rowan".
            display_title = title
            label = bucket["source_label"]
            for prefix in (label + ":", src.title() + ":", src + ":"):
                if display_title.startswith(prefix):
                    display_title = display_title[len(prefix):].strip()
                    break
            bucket["_by_key"][key] = {
                "title": display_title,
                "claim": claim or None,
                "source_url": f.get("source_url"),
                "confidence": f.get("confidence"),
                "finding_id": f.get("finding_id"),
                "finding_type": f.get("finding_type"),
                "count": 1,
            }

    groups = []
    for src, bucket in by_source.items():
        groups.append({
            "source": src,
            "source_label": bucket["source_label"],
            "count": bucket["total_count"],
            "unique_count": len(bucket["_by_key"]),
            "unique_claims": list(bucket["_by_key"].values()),
        })

    groups.sort(key=lambda g: _source_priority(g["source"]))
    return groups


@router.get("/entities/{entity_id}/research-deeper")
def get_entity_research_deeper(entity_id: int, topic: str = "", limit: int = 20):
    """Click-deeper interrogation on a dossier item.

    The Sources & Claims and Key Events panels make each row clickable —
    'research more on this'. That click hits here with the entity_id +
    a topic keyword (the claim text or event label). We search the
    corpus for signals matching BOTH the entity's canonical_name AND
    the topic, grouped by source, so the operator sees how different
    outlets covered THIS specific aspect.

    Cheap pass: no new external API calls, just a corpus query. The
    'full deeper investigation' (forcing a new sync run scoped to the
    topic) is a separate explicit action via /research?force=true.

    Returns:
      topic              the topic queried
      matched_signals    list of signals containing both terms
      by_source          { outlet_name: count } breakdown
      total              total matches
    """
    topic = (topic or "").strip()
    if not topic:
        return {"matched_signals": [], "by_source": {}, "total": 0, "topic": ""}

    with get_connection() as conn:
        entity_id = _follow_entity_redirect(conn, entity_id)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT canonical_name FROM entities WHERE entity_id = %s",
                (entity_id,),
            )
            r = cur.fetchone()
            if not r or not r[0]:
                raise HTTPException(404, "entity not found")
            name = r[0]

            # Search signals where the body text or title mentions both
            # the entity name and the topic. Postgres ILIKE — fine for
            # corpus sizes <1M; can switch to tsvector if it gets slow.
            cur.execute(
                """
                SELECT s.rid, s.signal_id, s.timestamp,
                       s.payload->>'title'   AS title,
                       s.payload->>'source'  AS source,
                       s.payload->>'link'    AS link,
                       LEFT(s.payload->>'summary', 200) AS snippet
                  FROM signals s
                 WHERE s.signal_type = 'signal.content.rss_entry'
                   AND (
                        s.payload->>'title' ILIKE %s OR
                        s.payload->>'summary' ILIKE %s OR
                        s.payload->>'body' ILIKE %s
                   )
                   AND (
                        s.payload->>'title' ILIKE %s OR
                        s.payload->>'summary' ILIKE %s OR
                        s.payload->>'body' ILIKE %s
                   )
                 ORDER BY s.timestamp DESC NULLS LAST
                 LIMIT %s
                """,
                (
                    f"%{name}%", f"%{name}%", f"%{name}%",
                    f"%{topic}%", f"%{topic}%", f"%{topic}%",
                    limit,
                ),
            )
            sigs: list[dict] = []
            by_source: dict[str, int] = {}
            for rid, sid, ts, title, source, link, snippet in cur.fetchall():
                src = (source or "unknown").strip()
                by_source[src] = by_source.get(src, 0) + 1
                sigs.append({
                    "signal_rid": rid,
                    "signal_id": sid,
                    "timestamp": ts.isoformat() if ts else None,
                    "title": title or "(untitled)",
                    "source": src,
                    "link": link,
                    "snippet": snippet,
                })

    return {
        "entity_id": entity_id,
        "entity_name": name,
        "topic": topic,
        "matched_signals": sigs,
        "by_source": by_source,
        "total": len(sigs),
    }


@router.get("/entities/{entity_id}/document_appearances")
def get_entity_document_appearances(entity_id: int, limit: int = 30):
    """Corpus documents that mention this entity, ordered by mention count.

    Each item has the document's basic identity + how many times the entity
    is mentioned in it + a snippet from the most-prominent paragraph.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.document_id, d.title, COUNT(*) AS mention_count
                  FROM document_mentions dm
                  JOIN documents d ON d.document_id = dm.document_id
                 WHERE dm.entity_id = %s
                 GROUP BY d.document_id, d.title
                 ORDER BY mention_count DESC
                 LIMIT %s
                """,
                (entity_id, limit),
            )
            rows = cur.fetchall()

    items = [
        {
            "document_id": r[0],
            "title": r[1] or f"document {r[0]}",
            "mention_count": int(r[2]),
        }
        for r in rows
    ]
    return {"documents": items, "count": len(items)}


@router.get("/entities/{entity_id}/assets")
def get_entity_assets(entity_id: int, limit: int = 200):
    """Tangible assets an entity is connected to: companies, real estate,
    aircraft, vessels, vehicles, products.

    Powers the Assets section of the dossier. Walks assessments where the
    entity is on either side of an ownership/operation edge and groups by
    the target's nodevocab category.

    Asset edges considered:
      owns, owned_by                  → ownership
      founded_by, founder_of          → founder relationship
      operates, operated_by           → operator
      controls, subsidiary_of, parent_org → corporate control
      manufactured_by                 → product origin
      headquartered_in, located_at, residence → spatial anchor
      employed, works_for, board_member → org affiliation
    """
    ASSET_EDGES = (
        'owns', 'owned_by', 'founded_by', 'founder_of', 'operates', 'operated_by',
        'controls', 'subsidiary_of', 'parent_org', 'manufactured_by',
        'headquartered_in', 'located_at', 'residence', 'employed', 'works_for',
        'board_member', 'beneficial_owner_of', 'ultimate_owner_of', 'trustee_of',
    )

    # Map nodevocab subtypes to UI category buckets
    CATEGORY_MAP = {
        'organization': 'organizations',
        'government_agency': 'organizations',
        'company': 'organizations',
        'org': 'organizations',
        'facility': 'real_estate',
        'address': 'real_estate',
        'building': 'real_estate',
        'compound': 'real_estate',
        'residence': 'real_estate',
        'place': 'places',
        'city': 'places',
        'region': 'places',
        'country': 'places',
        'aircraft': 'aircraft',
        'vessel': 'vessels',
        'vehicle': 'vehicles',
        'product': 'products',
    }

    CATEGORY_LABELS = {
        'organizations': 'Companies & Organizations',
        'real_estate': 'Real Estate & Facilities',
        'places': 'Locations',
        'aircraft': 'Aircraft',
        'vessels': 'Vessels',
        'vehicles': 'Vehicles',
        'products': 'Products',
    }
    # Category order in the UI
    CATEGORY_ORDER = list(CATEGORY_LABELS.keys())

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    a.assessment_id, a.predicate_edge_type,
                    a.subj_entity_id, ses.canonical_name AS subj_name,
                    a.obj_entity_id, eos.canonical_name AS obj_name,
                    eos.canonical_type AS obj_type,
                    eos.base60_address,
                    a.corroborated_by_count, a.contradicted_by_count,
                    a.earliest_event_time, a.latest_event_time
                FROM assessments a
                LEFT JOIN entities ses ON ses.entity_id = a.subj_entity_id
                LEFT JOIN entities eos ON eos.entity_id = a.obj_entity_id
                WHERE (a.subj_entity_id = %s OR a.obj_entity_id = %s)
                  AND a.predicate_edge_type = ANY(%s)
                  AND a.obj_entity_id IS NOT NULL
                  AND a.subj_entity_id IS NOT NULL
                ORDER BY a.corroborated_by_count DESC NULLS LAST, a.assessment_id
                LIMIT %s
                """,
                (entity_id, entity_id, list(ASSET_EDGES), limit),
            )
            rows = cur.fetchall()

    # Dedupe: same (target_entity_id, edge) pair only counts once even if
    # there are multiple assessments. Take the max corroboration.
    seen: dict[tuple[int, str], dict] = {}
    for r in rows:
        a_id, edge, subj_id, subj_name, obj_id, obj_name, obj_type, addr, corr, contra, ets, lts = r
        # Determine the "other side" (the asset target) and the direction
        if subj_id == entity_id:
            target_id = obj_id
            target_name = obj_name
            target_type = obj_type
            direction = 'out'
        else:
            target_id = subj_id
            target_name = subj_name
            # We don't have subj_type in this query — fetch later if needed; for
            # now treat as unknown which routes to "places" via fallback.
            target_type = None
            direction = 'in'

        if not target_id or not target_name:
            continue
        key = (target_id, edge)
        if key in seen:
            seen[key]['corroboration'] = max(seen[key]['corroboration'], corr or 1)
            continue
        category = CATEGORY_MAP.get((target_type or '').lower(), 'other')
        seen[key] = {
            'target_entity_id': target_id,
            'target_name': target_name,
            'target_type': target_type,
            'target_base60': addr,
            'edge': edge,
            'direction': direction,
            'corroboration': corr or 1,
            'contested': contra or 0,
            'category': category,
            'first_seen': ets.isoformat() if ets else None,
            'last_seen': lts.isoformat() if lts else None,
        }

    # Bucket by category preserving order
    buckets: dict[str, list] = {c: [] for c in CATEGORY_ORDER}
    other_bucket: list = []
    for item in seen.values():
        cat = item['category']
        if cat in buckets:
            buckets[cat].append(item)
        else:
            other_bucket.append(item)

    groups = []
    for cat in CATEGORY_ORDER:
        items = buckets[cat]
        if not items:
            continue
        items.sort(key=lambda x: -x['corroboration'])
        groups.append({
            'category': cat,
            'label': CATEGORY_LABELS[cat],
            'count': len(items),
            'items': items,
        })
    if other_bucket:
        other_bucket.sort(key=lambda x: -x['corroboration'])
        groups.append({
            'category': 'other',
            'label': 'Other',
            'count': len(other_bucket),
            'items': other_bucket,
        })

    total = sum(g['count'] for g in groups)
    return {'groups': groups, 'total_assets': total}


@router.get("/entities/{entity_id}/properties")
def get_entity_properties(entity_id: int):
    """Typed property assertions for an entity, grouped by property and
    sorted by precision depth (deepest = canonical first).

    Powers the property-grouped Relations panel. Returns:
      {
        "groups": [
          { "property_key": "color", "property_label": "Color",
            "canonical": {value, tier, depth, corroboration, ...},
            "attestations": [shallower values in same chain],
            "alternatives": [different-chain values flagged contested]
          },
          ...
        ]
      }
    """
    try:
        from core.substrate.collapse import get_property_assertions
        rows = get_property_assertions(entity_id)
    except Exception as e:
        return {"groups": [], "error": str(e)}

    by_prop: dict[str, list] = {}
    for r in rows:
        by_prop.setdefault(r["property_key"], []).append(r)

    groups = []
    for prop_key, items in by_prop.items():
        items.sort(key=lambda x: (-(x["precision_depth"] or 0), -x["corroborated_by_count"]))
        # Split into chains: canonical = top-depth + 0 contradictions, attestations
        # are shallower same-chain values, alternatives are flagged contested.
        # For v1 of the panel we keep it simple: top-depth row is "canonical",
        # remaining rows are either attestations or alternatives based on
        # contradicted_by_count.
        if not items:
            continue
        canonical = items[0]
        attestations = []
        alternatives = []
        for it in items[1:]:
            if it["contradicted_by_count"] > 0:
                alternatives.append(it)
            else:
                attestations.append(it)
        groups.append({
            "property_key": prop_key,
            "property_label": canonical["property_label"],
            "canonical": canonical,
            "attestations": attestations,
            "alternatives": alternatives,
            "total_assertions": len(items),
        })

    # Sort groups by canonical confidence then property cycle (stable)
    groups.sort(key=lambda g: -(g["canonical"]["corroborated_by_count"] or 0))
    return {"groups": groups, "total_properties": len(groups)}


@router.get("/entities/{entity_id}/corroboration")
def get_entity_corroboration(entity_id: int):
    """Cross-library corroboration: how many INDEPENDENT libraries confirm this
    entity, with the per-library breakdown. Independence is counted by distinct
    content hashes (duplicate copies of the same document don't inflate it), so
    this is a real 'how many separate sources back this' signal, not an echo.

    tier: single_source (1 library) | corroborated (2) | well_corroborated (3+).
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT lm.library_id,
                       COALESCE(l.notes::jsonb->>'display_title', l.focal_term) AS title,
                       COUNT(DISTINCT dm.document_id)     AS documents,
                       COUNT(DISTINCT d.content_sha256)   AS independent_sources,
                       COUNT(*)                           AS mentions
                  FROM document_mentions dm
                  JOIN library_members lm
                       ON lm.member_id = dm.document_id AND lm.member_kind = 'document'
                  JOIN libraries l  ON l.library_id = lm.library_id
                  JOIN documents d  ON d.document_id = dm.document_id
                 WHERE dm.entity_id = %s
                 GROUP BY lm.library_id, l.notes, l.focal_term
                 ORDER BY mentions DESC
                """,
                (entity_id,),
            )
            rows = cur.fetchall()
            # overall independent-document count across everything
            cur.execute(
                """SELECT COUNT(DISTINCT d.content_sha256)
                     FROM document_mentions dm
                     JOIN documents d ON d.document_id = dm.document_id
                    WHERE dm.entity_id = %s""",
                (entity_id,),
            )
            total_independent = cur.fetchone()[0]
    sources = [
        {"library_id": r[0], "title": r[1], "documents": r[2],
         "independent_sources": r[3], "mentions": r[4]}
        for r in rows
    ]
    lib_count = len(sources)
    # Corroboration strength is really "how many INDEPENDENT sources back this",
    # which the content-hash document count measures — not just library count.
    # Basing the tier on libraries alone mislabels an entity attested by 200+
    # independent documents in one library as "single_source". Escalate on
    # either axis: many independent documents, or multiple libraries.
    tier = ("well_corroborated" if (lib_count >= 3 or total_independent >= 5)
            else "corroborated" if (lib_count == 2 or total_independent >= 2)
            else "single_source")
    return {
        "entity_id": entity_id,
        "library_count": lib_count,
        "independent_documents": total_independent,
        "tier": tier,
        "sources": sources,
    }


import re as _story_re

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_MDY = _story_re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_ISO = _story_re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTH_DMY = _story_re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b",
    _story_re.I)
_YEAR = _story_re.compile(r"\b(19\d{2}|20\d{2})\b")
# "West Palm Beach, FL" / "Teterboro, NJ" — Title-case City, 2-letter state.
# City tokens require a lowercase letter so all-caps airport codes (CMH, PBI,
# TEB) are excluded and we grab the real place name.
_PLACE = _story_re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}),\s*([A-Z]{2})\b")


def _story_date(text: str, full_only: bool = False) -> Optional[str]:
    """Best-effort ISO date pulled from a paragraph's text (flight-log rows,
    dated prose). Returns 'YYYY-MM-DD' or 'YYYY', or None. Heuristic — the
    caller marks these rows date_inferred so the UI can show '~'.

    full_only=True accepts ONLY explicit full dates (a record's real date, e.g.
    a flight-log row), not a bare year — bare years in prose are too often
    incidental ('the 2024 election') and pollute an auto-timeline."""
    if not text:
        return None
    m = _MDY.search(text)
    if m:
        mm, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mm <= 12 and 1 <= dd <= 31 and 1500 <= yy <= 2099:
            return f"{yy:04d}-{mm:02d}-{dd:02d}"
    m = _ISO.search(text)
    if m and 1500 <= int(m.group(1)) <= 2099:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = _MONTH_DMY.search(text)
    if m:
        mo = _MONTHS.get(m.group(1)[:3].lower())
        if mo:
            return f"{int(m.group(3)):04d}-{mo:02d}-{int(m.group(2)):02d}"
    if full_only:
        return None
    m = _YEAR.search(text)
    if m:
        return m.group(1)
    return None


def _story_place(text: str) -> Optional[str]:
    """Best-effort 'City, ST' location label from a paragraph's text."""
    if not text:
        return None
    m = _PLACE.search(text)
    if m:
        place = f"{m.group(1)}, {m.group(2)}"
        return place[:60]
    return None


@router.get("/entities/{entity_id}/story")
def get_entity_story(
    entity_id: int,
    lens: Optional[str] = None,
    limit: int = 200,
    include_noise: bool = False,
):
    """Unified chronological story for an entity: Facts + Events + Research
    findings merged into one date-sorted stream, year-grouped.

    Each row carries:
      kind              'fact' | 'event' | 'finding'
      when              ISO date (or null for undated)
      title             short headline
      body              source paragraph / event title / finding summary
      source            doc title, signal source, or research source name
      source_url        click-through to original
      image_url         renderable thumbnail when available
      entity_ids        the entities this row mentions (for hover-card linking)
      assessment_id / event_id / finding_id  (for navigation)

    When lens=library/N, restricts to rows whose source provenance is in
    that library."""
    out: list[dict] = []
    with get_connection() as conn:
        lens_clause, lens_params = _lens_assessment_clause(conn, lens)
        scope, lens_value = _parse_lens(lens)
        doc_ids: list = []
        sig_ids: list = []
        if scope == "library":
            try:
                lib_id = int(lens_value)
                doc_ids = _library_member_ids(conn, lib_id, "document") or [-1]
                sig_ids = _library_member_ids(conn, lib_id, "signal") or [-1]
            except Exception:
                doc_ids, sig_ids = [-1], [-1]

        with conn.cursor() as cur:
            # 1. FACTS — assessments with this entity as subj or obj
            cur.execute(
                f"""
                SELECT a.assessment_id, a.assessment_tier,
                       a.subj_entity_id, ses.canonical_name AS subj_name,
                       a.predicate_edge_type,
                       a.obj_entity_id, oes.canonical_name AS obj_name, a.obj_value,
                       COALESCE(a.earliest_event_time, a.latest_event_time) AS when_ts,
                       a.source_statement_ids
                  FROM assessments a
                  LEFT JOIN entities ses ON ses.entity_id = a.subj_entity_id
                  LEFT JOIN entities oes ON oes.entity_id = a.obj_entity_id
                 WHERE (a.subj_entity_id = %s OR a.obj_entity_id = %s)
                       {lens_clause}
                 ORDER BY COALESCE(a.earliest_event_time, a.latest_event_time, a.first_seen) DESC NULLS LAST
                 LIMIT %s
                """,
                (entity_id, entity_id, *lens_params, limit),
            )
            fact_rows = cur.fetchall()

            # Pull one source per fact for body + image
            stmt_pool = [r[9][0] for r in fact_rows if r[9]]
            sources_by_stmt: dict[int, dict] = {}
            if stmt_pool:
                cur.execute(
                    """
                    WITH pstmt AS (
                      SELECT s.statement_id, s.raw_text, s.document_id,
                             d.title AS doc_title, p.page_id, p.order_index
                        FROM statements s
                        LEFT JOIN documents d ON d.document_id = s.document_id
                        LEFT JOIN document_paragraphs p ON p.paragraph_id = s.paragraph_id
                       WHERE s.statement_id = ANY(%s)
                    )
                    SELECT pstmt.statement_id, pstmt.raw_text,
                           pstmt.document_id, pstmt.doc_title,
                           COALESCE(string_agg(n.raw_text, ' ' ORDER BY n.order_index), '')
                      FROM pstmt
                      LEFT JOIN document_paragraphs n
                             ON n.page_id = pstmt.page_id
                            AND n.order_index BETWEEN pstmt.order_index - 1
                                                AND pstmt.order_index + 1
                     GROUP BY pstmt.statement_id, pstmt.raw_text, pstmt.document_id, pstmt.doc_title
                    """,
                    (stmt_pool,),
                )
                for sid, raw, doc_id, doc_title, context in cur.fetchall():
                    sources_by_stmt[sid] = {
                        "raw_text": raw,
                        "document_id": doc_id,
                        "document_title": doc_title,
                        "paragraph_text": (context or "").strip() or None,
                    }
            for r in fact_rows:
                aid, tier, sid_eid, sid_name, pred, oid_eid, oid_name, oval, when_ts, stmt_ids = r
                pretty_pred = (pred or "related to").replace("_", " ")
                obj_label = oid_name or oval or ""
                title = f"{sid_name or '?'} {pretty_pred} {obj_label}".strip()
                first_src = (sources_by_stmt.get((stmt_ids or [None])[0]) or {})
                out.append({
                    "kind": "fact",
                    "when": when_ts.isoformat() if when_ts else None,
                    "title": title,
                    "body": first_src.get("paragraph_text") or first_src.get("raw_text"),
                    "source": first_src.get("document_title"),
                    "source_url": None,
                    "image_url": None,
                    "tier": tier,
                    "assessment_id": aid,
                    "entity_ids": [eid for eid in (sid_eid, oid_eid) if eid],
                })

            # 2. EVENTS
            if scope == "library":
                cur.execute(
                    """
                    SELECT e.event_id, e.title, e.event_time, e.event_type,
                           e.actor_entity_ids, e.target_entity_ids, e.latitude, e.longitude
                      FROM events e
                     WHERE (%s = ANY(COALESCE(e.actor_entity_ids,  ARRAY[]::bigint[]))
                         OR %s = ANY(COALESCE(e.target_entity_ids, ARRAY[]::bigint[])))
                       AND EXISTS (
                         SELECT 1 FROM statements s
                          WHERE s.statement_id = ANY(e.originating_statement_ids)
                            AND (s.document_id = ANY(%s) OR s.signal_rid = ANY(%s))
                       )
                     ORDER BY e.event_time DESC NULLS LAST LIMIT %s
                    """,
                    (entity_id, entity_id, doc_ids, sig_ids, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT event_id, title, event_time, event_type,
                           actor_entity_ids, target_entity_ids, latitude, longitude
                      FROM events
                     WHERE %s = ANY(COALESCE(actor_entity_ids,  ARRAY[]::bigint[]))
                        OR %s = ANY(COALESCE(target_entity_ids, ARRAY[]::bigint[]))
                     ORDER BY event_time DESC NULLS LAST LIMIT %s
                    """,
                    (entity_id, entity_id, limit),
                )
            for evt in cur.fetchall():
                ev_id, etitle, etime, etype, actors, targets, lat, lon = evt
                actors = actors or []
                targets = targets or []
                out.append({
                    "kind": "event",
                    "when": etime.isoformat() if etime else None,
                    "title": etitle,
                    "body": None,
                    "source": etype,
                    "source_url": None,
                    "image_url": None,
                    "event_id": ev_id,
                    "entity_ids": list({*actors, *targets}),
                    "latitude": float(lat) if lat is not None else None,
                    "longitude": float(lon) if lon is not None else None,
                })

            # 3. FINDINGS (research results). Always canonical — research
            # is about the entity, not the lens.
            #
            # By default, hide findings the LLM relevance scorer has marked as
            # noise (score < 0.3 — e.g. Queen Victoria biographies attached to
            # a "Georgia Brook" investigation). Unscored findings still surface
            # (NULL relevance_score) so a stale backlog doesn't blank the
            # dossier. Caller can pass include_noise=true to override.
            min_relevance = 0.0 if include_noise else 0.3
            cur.execute(
                f"""
                SELECT f.finding_id, f.source, f.finding_type, f.title, f.data,
                       f.confidence, f.relevance_score, f.created_at
                  FROM investigation_findings f
                  {_FINDING_ATTRIBUTIONS_FROM}
                 WHERE {_FINDING_ATTRIBUTIONS_WHERE}
                   AND (f.relevance_score IS NULL OR f.relevance_score >= %s)
                 ORDER BY COALESCE(f.relevance_score, f.confidence) DESC NULLS LAST,
                          f.finding_id DESC
                 LIMIT %s
                """,
                (entity_id, entity_id, entity_id, entity_id, min_relevance, limit),
            )
            # Story stream is CHRONOLOGICAL — only include findings that
            # have a REAL event date (court date_filed, publication date,
            # incident date). Ingestion timestamps don't go here; that
            # noise belongs in the Profile section's source contrast.
            #
            # Also dedupe by (source, normalized-title): every ingestion
            # pass emits a fresh finding row, so without dedup the story
            # fills with "Wikipedia: Sam Rowan ×9" etc.
            _REAL_DATE_KEYS = (
                "date_filed", "event_date", "incident_date",
                "publication_date", "filing_date",
                "first_file_date", "founded", "inception",
                "date", "datetime", "start_date",
            )
            seen_keys: set[tuple[str, str]] = set()
            for fr in cur.fetchall():
                fid, fsrc, ftype, ftitle, fdata, fconf, frel, fcreated = fr
                data = fdata or {}
                if not isinstance(data, dict):
                    data = {}

                # Real-date filter: only include findings with an
                # explicit event-time field. created_at (ingestion) does
                # NOT qualify — that's not the same thing as "happened on".
                real_date = None
                for k in _REAL_DATE_KEYS:
                    v = data.get(k)
                    if isinstance(v, str) and len(v) >= 4:
                        # Light validation: starts with a 4-digit year.
                        if v[:4].isdigit() and 1500 <= int(v[:4]) <= 2099:
                            real_date = v
                            break
                if not real_date:
                    continue  # ingestion-only finding — handled by Profile

                # Dedupe: collapse repeated ingestion of the same
                # (source, title) into one row.
                norm_title = (ftitle or ftype or "").strip().lower()[:200]
                key = (fsrc or "", norm_title)
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                url = data.get("source_url") or data.get("url") or data.get("page_url")
                out.append({
                    "kind": "finding",
                    "when": real_date,
                    "title": ftitle or ftype,
                    "body": data.get("summary") or data.get("description"),
                    "source": fsrc,
                    "source_url": url,
                    "image_url": _normalize_image_url(fsrc, data),
                    "finding_id": fid,
                    "entity_ids": [],
                    "confidence": float(fconf) if fconf else None,
                    "relevance_score": float(frel) if frel else None,
                })

            # 4. APPEARANCES — where this entity is actually mentioned in the
            # corpus. This is the deterministic evidence layer (identity-library
            # + resolved mentions); on a collapse-only corpus the assessment /
            # event / finding layers above are empty, so without this the
            # dossier looks blank even though the entity appears in dozens of
            # documents. Quality-gated to clean paragraphs (no OCR grit),
            # lens-restricted to the library's docs, one row per paragraph.
            mention_filter, mparams = "", [entity_id]
            if scope == "library":
                mention_filter = "AND dm.document_id = ANY(%s)"
                mparams.append(doc_ids)
            cur.execute(
                f"""
                SELECT dm.document_id, d.title, dm.paragraph_id, pg.page_number,
                       max(p.raw_text) AS ptext
                  FROM document_mentions dm
                  JOIN document_paragraphs p ON p.paragraph_id = dm.paragraph_id
                  LEFT JOIN document_pages pg ON pg.page_id = p.page_id
                  LEFT JOIN documents d ON d.document_id = dm.document_id
                 WHERE dm.entity_id = %s {mention_filter}
                   AND COALESCE(p.metadata->>'quality_band', 'pass') IN ('pass', 'record')
                 GROUP BY dm.document_id, d.title, dm.paragraph_id, pg.page_number
                 ORDER BY dm.paragraph_id
                 LIMIT %s
                """,
                (*mparams, min(limit, 40)),
            )
            for m_doc_id, m_doc_title, m_pid, m_page, m_text in cur.fetchall():
                body = (m_text or "").strip()
                if len(body) < 20:
                    continue
                # Date + place are heuristically extracted from the paragraph
                # text (flight-log rows carry the date/route inline). Marked
                # date_inferred so the UI shows an approximate marker.
                inferred_when = _story_date(body, full_only=True)
                out.append({
                    "kind": "mention",
                    "when": inferred_when,
                    "date_inferred": bool(inferred_when),
                    "title": m_doc_title or "Appears in document",
                    "body": body[:600],
                    "location": _story_place(body),
                    "source": (f"page {m_page}" if m_page else None),
                    "source_url": None,
                    "image_url": None,
                    "document_id": m_doc_id,
                    "paragraph_id": m_pid,
                    "page_number": m_page,          # -> deep-link to the source page image
                    "entity_ids": [entity_id],
                })

            # ── Processing coverage: how much of this entity's corpus is still
            # raw (mentioned) vs collapsed into structured facts/events. Makes
            # incompleteness explicit ("12 of 20 documents still processing")
            # instead of silently showing a thin story as if it were complete.
            processing = {"documents": 0, "documents_collapsed": 0,
                          "mentions": 0, "structured_rows": 0, "pending_documents": 0}
            try:
                cur.execute(
                    """
                    SELECT COUNT(DISTINCT dm.document_id) AS docs,
                           COUNT(DISTINCT dm.document_id) FILTER (
                               WHERE EXISTS (SELECT 1 FROM statements s
                                              WHERE s.document_id = dm.document_id)
                           ) AS collapsed,
                           COUNT(*) AS mentions
                      FROM document_mentions dm
                     WHERE dm.entity_id = %s
                    """,
                    (entity_id,),
                )
                pr = cur.fetchone()
                if pr:
                    docs, collapsed, mentions = int(pr[0] or 0), int(pr[1] or 0), int(pr[2] or 0)
                    processing.update({
                        "documents": docs,
                        "documents_collapsed": collapsed,
                        "pending_documents": max(0, docs - collapsed),
                        "mentions": mentions,
                    })
            except Exception:
                pass

    # structured_rows = facts + events + findings actually surfaced (non-mention)
    processing["structured_rows"] = sum(1 for r in out if r.get("kind") != "mention")

    # Sort by date desc (None at end); then year-group
    def sort_key(r: dict):
        w = r.get("when")
        return (0, w) if w else (1, "")
    out.sort(key=sort_key, reverse=True)

    # Year-group in stable order (newest year first, undated last)
    from collections import OrderedDict
    grouped: OrderedDict[str, list] = OrderedDict()
    for r in out:
        when = r.get("when") or ""
        year = when[:4] if when[:4].isdigit() else "undated"
        grouped.setdefault(year, []).append(r)

    groups = [{"year": y, "rows": rs, "count": len(rs)} for y, rs in grouped.items()]
    return {"groups": groups, "total": len(out), "lens": lens, "processing": processing}


@router.get("/entities/{entity_id}/geo")
def get_entity_geo(entity_id: int):
    """Best-effort coordinate lookup for an entity. Tries:
      1. knowledge_nodes keyed by `entity:<id>` (the geocoder writes here)
      2. any knowledge_node whose entity_id field matches
      3. entity_addresses (FEC, geocoder, admission path) — most recent
    Returns None when no coordinates are known."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT node_id, latitude, longitude
                  FROM knowledge_nodes
                 WHERE (node_id = %s OR master_entity_id = %s)
                   AND latitude IS NOT NULL AND longitude IS NOT NULL
                 ORDER BY source_count DESC NULLS LAST
                 LIMIT 1
                """,
                (f"entity:{entity_id}", entity_id),
            )
            row = cur.fetchone()
            if row:
                return {"node_id": row[0],
                        "latitude": float(row[1]), "longitude": float(row[2])}
            # Fallback: any structured address with coords
            cur.execute(
                """
                SELECT address_id, lat, lon, raw_address, address_type
                  FROM entity_addresses
                 WHERE entity_id = %s AND lat IS NOT NULL AND lon IS NOT NULL
                 ORDER BY (valid_to IS NULL) DESC, confidence DESC NULLS LAST
                 LIMIT 1
                """,
                (entity_id,),
            )
            row = cur.fetchone()
            if row:
                return {
                    "address_id": row[0],
                    "latitude": float(row[1]), "longitude": float(row[2]),
                    "raw_address": row[3], "address_type": row[4],
                }
    return JSONResponse({"latitude": None, "longitude": None}, status_code=200)


@router.get("/entities/{entity_id}/facts")
def get_entity_facts(
    entity_id: int,
    tier: Optional[str] = None,
    limit: int = 100,
    lens: Optional[str] = None,
):
    """Every assessment where this entity is subj OR obj. When `lens` is
    set (e.g. lens=library/3), restricts to assessments whose source
    statements originate from documents/signals inside that lens."""
    with get_connection() as conn:
        lens_clause, lens_params = _lens_assessment_clause(conn, lens)
        sql = f"""
            SELECT a.assessment_id, a.base60_address, a.assessment_tier AS tier,
                   a.subj_entity_id, ses.canonical_name AS subj_name,
                   a.predicate_edge_type,
                   a.obj_entity_id, oes.canonical_name AS obj_name,
                   a.obj_value, a.polarity,
                   a.corroborated_by_count, a.contradicted_by_count,
                   a.earliest_event_time, a.latest_event_time,
                   a.confidence, a.source_statement_ids
              FROM assessments a
              LEFT JOIN entities ses ON ses.entity_id = a.subj_entity_id
              LEFT JOIN entities oes ON oes.entity_id = a.obj_entity_id
             WHERE (a.subj_entity_id = %s OR a.obj_entity_id = %s)
                   {lens_clause}
        """
        params: list[Any] = [entity_id, entity_id, *lens_params]
        if tier:
            sql += " AND a.assessment_tier = %s"
            params.append(tier)
        sql += " ORDER BY a.corroborated_by_count DESC NULLS LAST, a.assessment_id LIMIT %s"
        params.append(limit)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = [_clean(r) for r in _rows_to_dicts(cur)]

            # Pull the FIRST source statement per assessment plus a window
            # of paragraphs (±2 order_index on the same page) so the user
            # sees real context not just the OCR-line the predicate fell on.
            # "resides in Wellington" alone is useless; the surrounding
            # paragraphs tell the full incident story.
            stmt_pool: list[int] = []
            for r in rows:
                ids = r.get("source_statement_ids") or []
                if ids:
                    stmt_pool.append(ids[0])
            sources_by_stmt: dict[int, dict] = {}
            if stmt_pool:
                cur.execute(
                    """
                    WITH pstmt AS (
                        SELECT s.statement_id, s.raw_text, s.summarized_text,
                               s.document_id, d.title AS doc_title,
                               s.paragraph_id, p.page_id, p.order_index,
                               s.signal_rid, sig.payload->>'title' AS sig_title,
                               sig.payload->>'source' AS sig_source
                          FROM statements s
                          LEFT JOIN documents          d ON d.document_id = s.document_id
                          LEFT JOIN document_paragraphs p ON p.paragraph_id = s.paragraph_id
                          LEFT JOIN signals            sig ON sig.rid = s.signal_rid
                         WHERE s.statement_id = ANY(%s)
                    )
                    SELECT pstmt.*, COALESCE(string_agg(neighbor.raw_text,
                                                       ' ' ORDER BY neighbor.order_index),
                                             '') AS context_window
                      FROM pstmt
                      LEFT JOIN document_paragraphs neighbor
                             ON neighbor.page_id = pstmt.page_id
                            AND neighbor.order_index BETWEEN pstmt.order_index - 2
                                                     AND pstmt.order_index + 2
                     GROUP BY pstmt.statement_id, pstmt.raw_text, pstmt.summarized_text,
                              pstmt.document_id, pstmt.doc_title, pstmt.paragraph_id,
                              pstmt.page_id, pstmt.order_index,
                              pstmt.signal_rid, pstmt.sig_title, pstmt.sig_source
                    """,
                    (stmt_pool,),
                )
                for sr in cur.fetchall():
                    sources_by_stmt[sr[0]] = {
                        "statement_id": sr[0],
                        "raw_text": sr[1],
                        "summarized_text": sr[2],
                        "document_id": sr[3],
                        "document_title": sr[4],
                        "paragraph_id": sr[5],
                        "page_id": sr[6],
                        "order_index": sr[7],
                        "signal_rid": sr[8],
                        "signal_title": sr[9],
                        "signal_source": sr[10],
                        # ±2 paragraph window stitched into a single
                        # ~5-line context block. Far more useful than the
                        # single OCR-line `paragraph_text`.
                        "paragraph_text": (sr[11] or "").strip() or None,
                    }
            for r in rows:
                ids = r.get("source_statement_ids") or []
                r["sources"] = [sources_by_stmt[i] for i in ids if i in sources_by_stmt]

    _enrich_display_names(rows)
    return {"facts": rows, "count": len(rows), "lens": lens}


@router.get("/entities/{entity_id}/events")
def get_entity_events(entity_id: int, limit: int = 100, lens: Optional[str] = None):
    """Every event where this entity participated (actor or target). With
    a lens, restricts to events whose originating_statement_ids come from
    docs/signals in the lens."""
    with get_connection() as conn:
        scope, value = _parse_lens(lens)
        params: list[Any] = [entity_id, entity_id]
        if scope == "library":
            try:
                lib_id = int(value)
                doc_ids = _library_member_ids(conn, lib_id, "document") or [-1]
                sig_ids = _library_member_ids(conn, lib_id, "signal") or [-1]
            except (TypeError, ValueError):
                doc_ids, sig_ids = [-1], [-1]
            sql = """
                SELECT event_id, event_type, title, event_time, event_end_time,
                       temporal_precision, latitude, longitude, location_entity_id,
                       actor_entity_ids, target_entity_ids, confidence,
                       base60_address, source_count, originating_statement_ids
                  FROM events e
                 WHERE (%s = ANY(COALESCE(e.actor_entity_ids,  ARRAY[]::bigint[]))
                     OR %s = ANY(COALESCE(e.target_entity_ids, ARRAY[]::bigint[])))
                   AND EXISTS (
                     SELECT 1 FROM statements s
                      WHERE s.statement_id = ANY(e.originating_statement_ids)
                        AND (s.document_id = ANY(%s) OR s.signal_rid = ANY(%s))
                   )
                 ORDER BY event_time NULLS LAST LIMIT %s
            """
            params.extend([doc_ids, sig_ids, limit])
        else:
            sql = """
                SELECT event_id, event_type, title, event_time, event_end_time,
                       temporal_precision, latitude, longitude, location_entity_id,
                       actor_entity_ids, target_entity_ids, confidence,
                       base60_address, source_count, originating_statement_ids
                  FROM events
                 WHERE %s = ANY(COALESCE(actor_entity_ids,  ARRAY[]::bigint[]))
                    OR %s = ANY(COALESCE(target_entity_ids, ARRAY[]::bigint[]))
                 ORDER BY event_time NULLS LAST LIMIT %s
            """
            params.append(limit)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = [_clean(r) for r in _rows_to_dicts(cur)]
    return {"events": rows, "count": len(rows), "lens": lens}


@router.get("/entities/{entity_id}/neighbors")
def get_entity_neighbors(
    entity_id: int,
    limit: int = Query(20, ge=1, le=100),
    lens: Optional[str] = None,
):
    """Direct 1-hop neighbors via assessments. Each neighbor row carries
    predicate + direction + corroboration count. With a lens, only edges
    whose source statements come from docs/signals inside the lens count."""
    with get_connection() as conn:
        lens_clause, lens_params = _lens_assessment_clause(conn, lens)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT entity_id, canonical_name, canonical_type, base60_address
                  FROM entities WHERE entity_id = %s
                """,
                (entity_id,),
            )
            rows = _rows_to_dicts(cur)
            if not rows:
                raise HTTPException(404, f"entity {entity_id} not found")
            focal = _clean(rows[0])

            # Outgoing edges
            cur.execute(
                f"""
                SELECT a.obj_entity_id AS neighbor_id,
                       e.canonical_name, e.canonical_type, e.base60_address,
                       a.predicate_edge_type AS predicate,
                       'out' AS direction,
                       a.corroborated_by_count,
                       (SELECT s.raw_text FROM statements s WHERE s.statement_id = ANY(a.source_statement_ids) AND s.raw_text IS NOT NULL ORDER BY length(s.raw_text) DESC, s.statement_id LIMIT 1) AS quote,
                       (SELECT s.statement_id FROM statements s WHERE s.statement_id = ANY(a.source_statement_ids) AND s.raw_text IS NOT NULL ORDER BY length(s.raw_text) DESC, s.statement_id LIMIT 1) AS quote_stmt,
                       (SELECT s.document_id FROM statements s WHERE s.statement_id = ANY(a.source_statement_ids) AND s.raw_text IS NOT NULL ORDER BY length(s.raw_text) DESC, s.statement_id LIMIT 1) AS quote_doc
                  FROM assessments a
                  JOIN entities e ON e.entity_id = a.obj_entity_id
                 WHERE a.subj_entity_id = %s
                   AND a.obj_entity_id IS NOT NULL
                       {lens_clause}
                 ORDER BY a.corroborated_by_count DESC NULLS LAST
                 LIMIT %s
                """,
                (entity_id, *lens_params, limit),
            )
            out_edges = [_clean(r) for r in _rows_to_dicts(cur)]

            # Incoming edges
            cur.execute(
                f"""
                SELECT a.subj_entity_id AS neighbor_id,
                       e.canonical_name, e.canonical_type, e.base60_address,
                       a.predicate_edge_type AS predicate,
                       'in' AS direction,
                       a.corroborated_by_count,
                       (SELECT s.raw_text FROM statements s WHERE s.statement_id = ANY(a.source_statement_ids) AND s.raw_text IS NOT NULL ORDER BY length(s.raw_text) DESC, s.statement_id LIMIT 1) AS quote,
                       (SELECT s.statement_id FROM statements s WHERE s.statement_id = ANY(a.source_statement_ids) AND s.raw_text IS NOT NULL ORDER BY length(s.raw_text) DESC, s.statement_id LIMIT 1) AS quote_stmt,
                       (SELECT s.document_id FROM statements s WHERE s.statement_id = ANY(a.source_statement_ids) AND s.raw_text IS NOT NULL ORDER BY length(s.raw_text) DESC, s.statement_id LIMIT 1) AS quote_doc
                  FROM assessments a
                  JOIN entities e ON e.entity_id = a.subj_entity_id
                 WHERE a.obj_entity_id = %s
                   AND a.subj_entity_id IS NOT NULL
                       {lens_clause}
                 ORDER BY a.corroborated_by_count DESC NULLS LAST
                 LIMIT %s
                """,
                (entity_id, *lens_params, limit),
            )
            in_edges = [_clean(r) for r in _rows_to_dicts(cur)]

            # Edges from knowledge_edges — the table operator-driven
            # actions (selection research, manual links) write to.
            # Assessments cover EXTRACTED facts; knowledge_edges covers
            # everything else (mentions, cites, located_at, ...). Both
            # need to surface in Connections so the operator sees the
            # full graph their actions produced. Slice off the 'entity:'
            # prefix (7 chars) to recover the bigint.
            node_id = f"entity:{entity_id}"
            cur.execute(
                """
                SELECT (SUBSTRING(ke.target_node_id FROM 8))::bigint AS neighbor_id,
                       e.canonical_name, e.canonical_type, e.base60_address,
                       ke.relationship AS predicate,
                       'out' AS direction,
                       1 AS corroborated_by_count
                  FROM knowledge_edges ke
                  JOIN entities e
                    ON e.entity_id = (SUBSTRING(ke.target_node_id FROM 8))::bigint
                 WHERE ke.source_node_id = %s
                   AND ke.target_node_id LIKE 'entity:%%'
                 ORDER BY ke.created_at DESC NULLS LAST
                 LIMIT %s
                """,
                (node_id, limit),
            )
            ke_out = [_clean(r) for r in _rows_to_dicts(cur)]

            cur.execute(
                """
                SELECT (SUBSTRING(ke.source_node_id FROM 8))::bigint AS neighbor_id,
                       e.canonical_name, e.canonical_type, e.base60_address,
                       ke.relationship AS predicate,
                       'in' AS direction,
                       1 AS corroborated_by_count
                  FROM knowledge_edges ke
                  JOIN entities e
                    ON e.entity_id = (SUBSTRING(ke.source_node_id FROM 8))::bigint
                 WHERE ke.target_node_id = %s
                   AND ke.source_node_id LIKE 'entity:%%'
                 ORDER BY ke.created_at DESC NULLS LAST
                 LIMIT %s
                """,
                (node_id, limit),
            )
            ke_in = [_clean(r) for r in _rows_to_dicts(cur)]

            # Co-occurrence edges — the deterministic-collapse relationship
            # signal. Two entities named in the SAME paragraph co-occur. The
            # strength is the number of distinct DOCUMENTS the pair co-occurs
            # in — i.e. how many independent sources corroborate the tie, which
            # is what the field name promises and what the platform's
            # cross-source thesis rewards. Counting shared *paragraphs* instead
            # lets a single ledger/manifest document (a donation dump, a flight
            # log) manufacture a phantom top-relationship from one source, so we
            # rank by distinct documents and keep shared_paragraphs as detail.
            # Symmetric ("appears_with"), reads only from document_mentions (the
            # layer that always exists post-collapse), and powers relationships
            # when a node has no assessments/knowledge_edges.
            cur.execute(
                """
                SELECT other.entity_id AS neighbor_id,
                       e.canonical_name, e.canonical_type, e.base60_address,
                       'appears_with' AS predicate,
                       'cooccur' AS direction,
                       COUNT(DISTINCT me.document_id) AS corroborated_by_count,
                       COUNT(DISTINCT me.paragraph_id) AS shared_paragraphs
                  FROM document_mentions me
                  JOIN document_mentions other
                    ON other.paragraph_id = me.paragraph_id
                   AND other.entity_id <> me.entity_id
                  JOIN entities e ON e.entity_id = other.entity_id
                 WHERE me.entity_id = %s
                   AND me.paragraph_id IS NOT NULL
                   AND other.entity_id IS NOT NULL
                 GROUP BY other.entity_id, e.canonical_name,
                          e.canonical_type, e.base60_address
                 ORDER BY COUNT(DISTINCT me.document_id) DESC,
                          COUNT(DISTINCT me.paragraph_id) DESC
                 LIMIT %s
                """,
                (entity_id, limit),
            )
            cooccur_edges = [_clean(r) for r in _rows_to_dicts(cur)]

    neighbors: list[dict] = []
    for e in out_edges + in_edges + ke_out + ke_in + cooccur_edges:
        e["entity_id"] = e.pop("neighbor_id")
        neighbors.append(e)
    # Dedupe by (entity_id, predicate, direction)
    seen = set()
    dedup: list[dict] = []
    for n in neighbors:
        k = (n["entity_id"], n["predicate"], n["direction"])
        if k in seen:
            continue
        seen.add(k)
        dedup.append(n)
    dedup.sort(key=lambda r: -(r.get("corroborated_by_count") or 0))
    return {"focal": focal, "neighbors": dedup[:limit], "lens": lens}


@router.get("/entities/{entity_id}/fact_graph")
def get_entity_fact_graph(entity_id: int, limit: int = Query(300, ge=1, le=600)):
    """Typed, SOURCED relationships for the fact graph — assessment edges only
    (no co-occurrence). Each fact carries its predicate, direction, tier,
    corroboration, a source quote, and the source document + statement so the
    UI can link straight to the original page. `preds` groups them by
    relationship type so the UI can offer a per-relationship web."""
    from collections import Counter as _Counter
    Q = ("(SELECT s.raw_text FROM statements s WHERE s.statement_id = ANY(a.source_statement_ids) "
         "AND s.raw_text IS NOT NULL ORDER BY length(s.raw_text) DESC, s.statement_id LIMIT 1)")
    DC = ("(SELECT s.document_id FROM statements s WHERE s.statement_id = ANY(a.source_statement_ids) "
          "AND s.raw_text IS NOT NULL ORDER BY length(s.raw_text) DESC, s.statement_id LIMIT 1)")
    ST = ("(SELECT s.statement_id FROM statements s WHERE s.statement_id = ANY(a.source_statement_ids) "
          "AND s.raw_text IS NOT NULL ORDER BY length(s.raw_text) DESC, s.statement_id LIMIT 1)")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT canonical_name FROM entities WHERE entity_id = %s", (entity_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, f"entity {entity_id} not found")
            name = row[0]
            facts = []
            for direc, me, other in (("out", "subj_entity_id", "obj_entity_id"),
                                      ("in", "obj_entity_id", "subj_entity_id")):
                cur.execute(
                    f"""
                    SELECT oe.entity_id, oe.canonical_name, a.predicate_edge_type,
                           a.assessment_tier, a.corroborated_by_count,
                           {Q} AS quote, {DC} AS doc, {ST} AS stmt
                      FROM assessments a
                      JOIN entities oe ON oe.entity_id = a.{other}
                     WHERE a.{me} = %s AND a.{other} IS DISTINCT FROM %s
                       AND oe.canonical_name ~ '^[A-Z]'
                     ORDER BY a.corroborated_by_count DESC NULLS LAST
                    """,
                    (entity_id, entity_id),
                )
                for oid, onm, edge, tier, cor, quote, doc, stmt in cur.fetchall():
                    facts.append({"other": onm, "other_id": oid, "edge": edge, "dir": direc,
                                  "tier": tier, "corrob": cor or 0, "quote": (quote or "")[:200],
                                  "doc": doc, "stmt": stmt})
    seen = set(); ded = []
    for f in sorted(facts, key=lambda x: -x["corrob"]):
        k = (f["other"], f["edge"], f["dir"])
        if k in seen:
            continue
        seen.add(k); ded.append(f)
    ded = ded[:limit]
    counts = _Counter(f["edge"] for f in ded)
    return {"name": name, "facts": ded, "preds": [[e, c] for e, c in counts.most_common()]}


# ─── Assessments ──────────────────────────────────────────────────────

@router.get("/assessments/{assessment_id}")
def get_assessment(assessment_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.assessment_id, a.tenant_id, a.base60_address,
                       a.assessment_tier AS tier,
                       a.subj_entity_id, ses.canonical_name AS subj_name,
                       a.predicate_edge_type,
                       a.obj_entity_id, oes.canonical_name AS obj_name,
                       a.obj_value, a.polarity, a.collapse_key,
                       a.corroborated_by_count, a.contradicted_by_count,
                       a.source_statement_ids,
                       a.earliest_event_time, a.latest_event_time,
                       a.primary_location_entity_id, a.confidence,
                       a.first_seen, a.last_seen, a.canonical_summary
                  FROM assessments a
                  LEFT JOIN entities ses ON ses.entity_id = a.subj_entity_id
                  LEFT JOIN entities oes ON oes.entity_id = a.obj_entity_id
                 WHERE a.assessment_id = %s
                """,
                (assessment_id,),
            )
            rows = _rows_to_dicts(cur)
            if not rows:
                raise HTTPException(404, f"assessment {assessment_id} not found")
            return _clean(rows[0])


# ─── Events ───────────────────────────────────────────────────────────

@router.get("/events/{event_id}")
def get_event(event_id: int):
    """Event dossier — core fields + participants (with canonical names),
    source statements (with article context), and related events
    (same actors/location)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.event_id, e.tenant_id, e.base60_address, e.event_type,
                       e.title, e.narrative, e.event_time, e.event_end_time,
                       e.temporal_precision, e.latitude, e.longitude,
                       e.location_entity_id, l.canonical_name AS location_name,
                       e.actor_entity_ids, e.target_entity_ids,
                       e.originating_statement_ids, e.source_count,
                       e.confidence, e.created_at, e.updated_at
                  FROM events e
                  LEFT JOIN entities l ON l.entity_id = e.location_entity_id
                 WHERE e.event_id = %s
                """,
                (event_id,),
            )
            rows = _rows_to_dicts(cur)
            if not rows:
                raise HTTPException(404, f"event {event_id} not found")
            event = _clean(rows[0])

            # Resolve participant names — current UI shows "entity 123"
            # because the bare list of IDs has no names attached.
            participant_ids: list[int] = []
            for k in ("actor_entity_ids", "target_entity_ids"):
                participant_ids.extend([
                    int(i) for i in (event.get(k) or []) if i
                ])
            participant_ids = list(dict.fromkeys(participant_ids))  # dedupe, preserve order

            participants: list[dict] = []
            if participant_ids:
                cur.execute(
                    """
                    SELECT entity_id, canonical_name, canonical_type, base60_address
                      FROM entities
                     WHERE entity_id = ANY(%s)
                    """,
                    (participant_ids,),
                )
                by_id = {r[0]: {
                    "entity_id": r[0],
                    "canonical_name": r[1],
                    "canonical_type": r[2],
                    "base60_address": r[3],
                } for r in cur.fetchall()}
                actor_set = set(event.get("actor_entity_ids") or [])
                target_set = set(event.get("target_entity_ids") or [])
                for pid in participant_ids:
                    p = by_id.get(pid) or {
                        "entity_id": pid,
                        "canonical_name": None,
                        "canonical_type": None,
                        "base60_address": None,
                    }
                    roles = []
                    if pid in actor_set:
                        roles.append("actor")
                    if pid in target_set:
                        roles.append("target")
                    p["roles"] = roles
                    participants.append(p)
            event["participants"] = participants

            # Source statements — the article paragraphs that mentioned
            # this event. Operators see WHICH sources reported the event
            # and click through to original docs.
            stmt_ids = [int(i) for i in (event.get("originating_statement_ids") or []) if i]
            sources: list[dict] = []
            if stmt_ids:
                cur.execute(
                    """
                    SELECT s.statement_id, s.raw_text,
                           d.document_id, d.title AS doc_title,
                           d.source_type, d.source_url, d.publisher,
                           d.created_at
                      FROM statements s
                      LEFT JOIN documents d ON d.document_id = s.document_id
                     WHERE s.statement_id = ANY(%s)
                     LIMIT 12
                    """,
                    (stmt_ids,),
                )
                for sid, raw, did, dtitle, stype, surl, pub, pub_at in cur.fetchall():
                    sources.append({
                        "statement_id": sid,
                        "raw_text": (raw or "")[:600],
                        "document_id": did,
                        "document_title": dtitle,
                        "source_type": stype,
                        "source_url": surl,
                        "publisher": pub,
                        "published_at": pub_at.isoformat() if pub_at else None,
                    })
            event["sources"] = sources

            # Related events — share at least one actor and are within ±7
            # days. Cheap heuristic, useful for spotting the OTHER 16
            # "Article published" events that look the same.
            related: list[dict] = []
            if participant_ids and event.get("event_time"):
                cur.execute(
                    """
                    SELECT event_id, title, event_type, event_time,
                           latitude, longitude
                      FROM events
                     WHERE event_id <> %s
                       AND event_time BETWEEN %s::timestamptz - INTERVAL '7 days'
                                          AND %s::timestamptz + INTERVAL '7 days'
                       AND (actor_entity_ids && %s::bigint[]
                            OR target_entity_ids && %s::bigint[])
                     ORDER BY event_time DESC
                     LIMIT 12
                    """,
                    (event_id, event["event_time"], event["event_time"],
                     participant_ids, participant_ids),
                )
                for rid, rtitle, retype, retime, rlat, rlon in cur.fetchall():
                    related.append({
                        "event_id": rid,
                        "title": rtitle,
                        "event_type": retype,
                        "event_time": retime.isoformat() if retime else None,
                        "latitude": float(rlat) if rlat is not None else None,
                        "longitude": float(rlon) if rlon is not None else None,
                    })
            event["related_events"] = related

            return event


# ─── Places ───────────────────────────────────────────────────────────

@router.get("/places/{place_id}")
def get_place(place_id: int):
    """Single-place detail used by /place/[id]. Post-unification
    (migration 118), 'place_id' is an entity_id whose canonical_type is
    place-like (city/region/country/place/facility/...). Spatial data
    is JOINed from the place_attributes sidecar.

    Response shape preserved for backwards compatibility with the
    /place/[id] UI route — 'place_id' field returned, but it's now the
    entity_id under the hood. 410 if envelope.reclassification present."""
    from services.api.src._node_redirect import resolve_node_or_410
    with get_connection() as conn:
        # Pre-migration-118: places table still exists, entities don't
        # carry place_attributes. Post-migration-118: places dropped,
        # place_attributes sidecar exists. Detect which shape we're in
        # and pick the right query so /place/[id] keeps working through
        # the fresh-DB transition.
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.place_attributes')")
            has_sidecar = cur.fetchone()[0] is not None

        if has_sidecar:
            # Post-migration: place_id IS entity_id with a place sidecar.
            resolve_node_or_410(conn, table="entities", id_col="entity_id", id_value=place_id)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT e.entity_id, e.canonical_name, e.canonical_type,
                           pa.iso_country_code, pa.iso_region_code,
                           ST_Y(pa.coordinates::geometry) AS latitude,
                           ST_X(pa.coordinates::geometry) AS longitude,
                           pa.elevation_m, e.aliases, pa.parent_entity_id,
                           parent.canonical_name AS parent_name,
                           e.signal_count, e.first_seen, e.last_seen,
                           e.envelope, e.base60_address, pa.geonames_id,
                           pa.geocode_source
                      FROM entities e
                      LEFT JOIN place_attributes pa ON pa.entity_id = e.entity_id
                      LEFT JOIN entities parent ON parent.entity_id = pa.parent_entity_id
                     WHERE e.entity_id = %s
                    """,
                    (place_id,),
                )
                r = cur.fetchone()
        else:
            # Pre-migration: query the legacy places table directly.
            resolve_node_or_410(conn, table="places", id_col="place_id", id_value=place_id)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT p.place_id, p.canonical_name, p.canonical_type,
                           p.iso_country_code, p.iso_region_code,
                           ST_Y(p.coordinates::geometry) AS latitude,
                           ST_X(p.coordinates::geometry) AS longitude,
                           p.elevation_m, p.aliases, p.parent_place_id,
                           parent.canonical_name AS parent_name,
                           p.mention_count, p.first_seen, p.last_seen,
                           p.envelope, NULL, NULL, p.geocode_source
                      FROM places p
                      LEFT JOIN places parent ON parent.place_id = p.parent_place_id
                     WHERE p.place_id = %s
                    """,
                    (place_id,),
                )
                r = cur.fetchone()

        with conn.cursor() as cur:
            if not r:
                raise HTTPException(404, f"place {place_id} not found")
            place = {
                "place_id": r[0],  # back-compat alias for entity_id
                "entity_id": r[0],
                "canonical_name": r[1],
                "canonical_type": r[2],
                "iso_country_code": r[3],
                "iso_region_code": r[4],
                "latitude": float(r[5]) if r[5] is not None else None,
                "longitude": float(r[6]) if r[6] is not None else None,
                "elevation_m": r[7],
                "aliases": list(r[8] or []),
                "parent_place_id": r[9],  # back-compat alias
                "parent_entity_id": r[9],
                "parent_name": r[10],
                "mention_count": int(r[11] or 0),
                "first_seen": r[12].isoformat() if r[12] else None,
                "last_seen": r[13].isoformat() if r[13] else None,
                "envelope": r[14] or {},
                "base60_address": r[15] if len(r) > 15 else None,
                "geonames_id": r[16] if len(r) > 16 else None,
                "geocode_source": r[17] if len(r) > 17 else None,
            }
            cur.execute(
                """
                SELECT e.entity_id, e.canonical_name, e.canonical_type, e.signal_count
                  FROM entities e
                  JOIN knowledge_edges ke
                    ON ke.target_node_id = 'entity:' || %s
                   AND ke.source_node_id = 'entity:' || e.entity_id::text
                   AND ke.relationship = 'located_at'
                 ORDER BY e.signal_count DESC NULLS LAST
                 LIMIT 50
                """,
                (place_id,),
            )
            place["entities_here"] = [
                {"entity_id": x[0], "canonical_name": x[1], "canonical_type": x[2],
                 "signal_count": int(x[3] or 0)} for x in cur.fetchall()
            ]
            cur.execute(
                """
                SELECT event_id, event_type, title, event_time, source_count
                  FROM events
                 WHERE place_node_id = %s
                 ORDER BY event_time DESC NULLS LAST
                 LIMIT 30
                """,
                (f"entity:{place_id}",),
            )
            place["events_here"] = [
                {"event_id": x[0], "event_type": x[1], "title": x[2],
                 "event_time": x[3].isoformat() if x[3] else None,
                 "source_count": int(x[4] or 0)} for x in cur.fetchall()
            ]
            return place


# ─── Universal search ─────────────────────────────────────────────────

@router.get("/search")
def universal_search(q: str = "", limit: int = 15):
    """Cross-substrate ILIKE search on canonical_name. Returns top results
    grouped by node category. Cheap and good enough for the /search page;
    can be upgraded to pg_trgm + embedding search later."""
    q = (q or "").strip()
    if len(q) < 2:
        return {"entities": [], "places": [], "topics": [], "documents": [],
                "events": [], "query": q}
    pat = f"%{q}%"
    cap = max(1, min(limit, 50))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT entity_id, canonical_name, canonical_type, signal_count
                  FROM entities
                 WHERE canonical_name ILIKE %s
                   AND NOT (COALESCE(envelope, '{}'::jsonb) ? 'reclassification')
                 ORDER BY signal_count DESC NULLS LAST
                 LIMIT %s
                """,
                (pat, cap),
            )
            entities = [{"id": r[0], "name": r[1], "type": r[2], "signal_count": int(r[3] or 0)}
                        for r in cur.fetchall()]
            # Post-unification (migration 118): places are entities
            # with place-like canonical_type. Filter by canonical_type.
            cur.execute(
                """
                SELECT entity_id, canonical_name, canonical_type, signal_count
                  FROM entities
                 WHERE canonical_name ILIKE %s
                   AND canonical_type IN ('place', 'city', 'region', 'country',
                                          'gpe', 'admin_region', 'facility_location',
                                          'island', 'peninsula', 'archipelago',
                                          'river', 'ocean', 'sea', 'strait',
                                          'maritime_area', 'poi', 'named_area')
                   AND NOT (COALESCE(envelope, '{}'::jsonb) ? 'reclassification')
                 ORDER BY signal_count DESC NULLS LAST
                 LIMIT %s
                """,
                (pat, cap),
            )
            places = [{"id": r[0], "name": r[1], "type": r[2], "mention_count": int(r[3] or 0)}
                      for r in cur.fetchall()]
            cur.execute(
                """
                SELECT topic_id, canonical_name, canonical_type, mention_count
                  FROM topics
                 WHERE canonical_name ILIKE %s
                   AND NOT (COALESCE(envelope, '{}'::jsonb) ? 'reclassification')
                 ORDER BY mention_count DESC NULLS LAST
                 LIMIT %s
                """,
                (pat, cap),
            )
            topics = [{"id": r[0], "name": r[1], "type": r[2], "mention_count": int(r[3] or 0)}
                      for r in cur.fetchall()]
            cur.execute(
                """
                SELECT document_id, title, source_type, page_count
                  FROM documents
                 WHERE title ILIKE %s
                 ORDER BY document_id DESC
                 LIMIT %s
                """,
                (pat, cap),
            )
            documents = [{"id": r[0], "name": r[1], "type": r[2], "page_count": r[3]}
                         for r in cur.fetchall()]
            cur.execute(
                """
                SELECT event_id, title, event_type, source_count
                  FROM events
                 WHERE title ILIKE %s
                 ORDER BY source_count DESC NULLS LAST
                 LIMIT %s
                """,
                (pat, cap),
            )
            events = [{"id": r[0], "name": r[1], "type": r[2], "source_count": int(r[3] or 0)}
                      for r in cur.fetchall()]
    return {"entities": entities, "places": places, "topics": topics,
            "documents": documents, "events": events, "query": q}


# ─── References ───────────────────────────────────────────────────────

@router.get("/system/depth")
def system_depth():
    """Live pipeline-depth snapshot. Drives the Hub Pipeline panel +
    /admin status views. One round-trip; ~50ms on node-A."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            def scalar(sql: str, params: tuple = ()) -> int:
                cur.execute(sql, params)
                r = cur.fetchone()
                return int(r[0]) if r and r[0] is not None else 0

            depth = {
                "references": {
                    "pending":     scalar("SELECT COUNT(*) FROM bibliographic_references WHERE acquisition_status='pending'"),
                    "acquired":    scalar("SELECT COUNT(*) FROM bibliographic_references WHERE acquisition_status='acquired'"),
                    "ingested":    scalar("SELECT COUNT(*) FROM bibliographic_references WHERE acquisition_status='ingested'"),
                    "unavailable": scalar("SELECT COUNT(*) FROM bibliographic_references WHERE acquisition_status='unavailable'"),
                    "paywalled":   scalar("SELECT COUNT(*) FROM bibliographic_references WHERE acquisition_status='paywall'"),
                },
                "documents": {
                    "total":             scalar("SELECT COUNT(*) FROM documents"),
                    "in_libraries":      scalar("SELECT COUNT(*) FROM library_members WHERE member_kind='document'"),
                    "without_mentions":  scalar("SELECT COUNT(*) FROM documents d WHERE NOT EXISTS (SELECT 1 FROM document_mentions WHERE document_id = d.document_id)"),
                    "with_mentions":     scalar("SELECT COUNT(DISTINCT document_id) FROM document_mentions"),
                    "with_statements":   scalar("SELECT COUNT(DISTINCT document_id) FROM statements"),
                    "with_promoted":     scalar("SELECT COUNT(DISTINCT document_id) FROM statements WHERE validation_status='promoted'"),
                },
                "knowledge": {
                    "mentions_total":         scalar("SELECT COUNT(*) FROM document_mentions"),
                    "mentions_resolved":      scalar("SELECT COUNT(*) FROM document_mentions WHERE entity_id IS NOT NULL"),
                    "mentions_unresolved":    scalar("SELECT COUNT(*) FROM document_mentions WHERE entity_id IS NULL"),
                    "statements_total":       scalar("SELECT COUNT(*) FROM statements"),
                    "statements_pending":     scalar("SELECT COUNT(*) FROM statements WHERE validation_status='pending'"),
                    "statements_promoted":    scalar("SELECT COUNT(*) FROM statements WHERE validation_status='promoted'"),
                    "statements_rejected":    scalar("SELECT COUNT(*) FROM statements WHERE validation_status='rejected'"),
                    "assessments_total":      scalar("SELECT COUNT(*) FROM assessments"),
                    "facts":                  scalar("SELECT COUNT(*) FROM assessments WHERE assessment_tier='fact'"),
                    "observations":           scalar("SELECT COUNT(*) FROM assessments WHERE assessment_tier='observation'"),
                    "claims":                 scalar("SELECT COUNT(*) FROM assessments WHERE assessment_tier='claim'"),
                    "events":                 scalar("SELECT COUNT(*) FROM events WHERE base60_address IS NOT NULL"),
                },
                "entities": {
                    "total":              scalar("SELECT COUNT(*) FROM entities"),
                    "instances":          scalar("SELECT COUNT(*) FROM entities WHERE entity_kind='instance'"),
                    "concepts":           scalar("SELECT COUNT(*) FROM entities WHERE entity_kind='concept'"),
                    "linked_to_concept":  scalar("SELECT COUNT(*) FROM entities WHERE concept_entity_id IS NOT NULL"),
                },
                "dictionary": {
                    "forms":           scalar("SELECT COUNT(*) FROM language_forms"),
                    "lemmas":          scalar("SELECT COUNT(*) FROM language_lemmas"),
                    "definitions":     scalar("SELECT COUNT(*) FROM definitions"),
                    "predicates":      scalar("SELECT COUNT(*) FROM predicate_dictionary"),
                },
                "discovery_signal": {
                    "edge_observations":       scalar("SELECT COUNT(*) FROM edge_type_observations"),
                    "candidate_edge_types":    scalar("SELECT COUNT(*) FROM edge_type_observations WHERE schema_status='candidate'"),
                    "candidate_predicates":    scalar("SELECT COUNT(DISTINCT predicate_lemma) FROM statements WHERE validation_reason LIKE 'no_predicate_mapping:%%'"),
                    "edge_proposals_pending":  scalar("SELECT COUNT(*) FROM edge_proposals WHERE status='pending'"),
                },
                "live_queue": {
                    "collapse_eligible":       scalar(
                        "SELECT COUNT(*) FROM library_members lm "
                        "  JOIN documents d ON d.document_id = lm.member_id "
                        " WHERE lm.member_kind='document' "
                        "   AND NOT EXISTS (SELECT 1 FROM document_mentions WHERE document_id = lm.member_id)"),
                    "resolution_eligible":     scalar("SELECT COUNT(*) FROM document_mentions WHERE entity_id IS NULL"),
                    "promotion_eligible":      scalar("SELECT COUNT(*) FROM statements WHERE validation_status='pending'"),
                    "acquisition_eligible":    scalar(
                        "SELECT COUNT(*) FROM bibliographic_references "
                        " WHERE acquisition_status='pending' "
                        "   AND (next_retry_at IS NULL OR next_retry_at <= NOW())"),
                },
            }
    return depth


@router.get("/assessments/{assessment_id}/statements")
def get_assessment_statements(assessment_id: int):
    """Verbatim source statements that support an assessment. Drives
    the 'supporting statements (3)' panel on the assessment page."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_statement_ids FROM assessments WHERE assessment_id = %s",
                (assessment_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, f"assessment {assessment_id} not found")
            ids = list(row[0] or [])
            if not ids:
                return {"statements": [], "count": 0}
            cur.execute(
                """
                SELECT s.statement_id, s.document_id, s.page_id, s.paragraph_id,
                       s.raw_text, s.summarized_text, s.modality, s.polarity,
                       s.confidence, s.predicate_verb_surface, s.predicate_lemma,
                       s.event_time, s.surrounding_text,
                       d.title AS doc_title, d.dataset_id
                  FROM statements s
                  LEFT JOIN documents d ON d.document_id = s.document_id
                 WHERE s.statement_id = ANY(%s)
                 ORDER BY s.statement_id
                """,
                (ids,),
            )
            rows = [_clean(r) for r in _rows_to_dicts(cur)]
    return {"statements": rows, "count": len(rows)}


@router.get("/references/{reference_id}")
def get_reference(reference_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT reference_id, base60_address, library_id, citation_text,
                       title, authors, publication_year, publisher, reference_type,
                       identifiers, discovered_via, discovery_query,
                       acquisition_status, acquisition_attempts,
                       last_attempted_at, next_retry_at,
                       acquisition_method, acquired_url, file_path,
                       file_size_bytes, file_sha256, document_id,
                       citing_document_ids, purchase_links, notes
                  FROM bibliographic_references WHERE reference_id = %s
                """,
                (reference_id,),
            )
            rows = _rows_to_dicts(cur)
            if not rows:
                raise HTTPException(404, f"reference {reference_id} not found")
            return _clean(rows[0])


# ─── Global fact timeline ─────────────────────────────────────────────────

@router.get("/facts/timeline")
def facts_timeline(
    library_id: Optional[int] = None,
    tier: Optional[str] = None,
    source_kind: Optional[str] = None,   # 'pdf' | 'rss'
    limit: int = 300,
    include_noise: bool = False,
    # Date sanity — drop the OCR / page-number noise (year=2945, year=2058,
    # undated buckets of 295 facts) that made the global /timeline feel
    # silly. Defaults: only show facts that HAVE an event time, within a
    # 90-day rolling window, with a plausible year. Override per-call for
    # library/topic dossier timelines that need wider ranges.
    days: int = 90,
    min_year: int = 1980,
    include_undated: bool = False,
):
    """All assessments across the system, time-ordered, with library tags
    + source provenance. Powers /timeline (the global fact-history view).

    Filters:
      library_id        restrict to facts touching entities in one library
      tier              fact | observation | claim
      source_kind       pdf (statement.document_id set) | rss (signal_rid set)
      days              rolling window (default 90; use a large number like
                        36500 for 'all time' on a library dossier)
      min_year          drops 1900/0001/etc. extraction-error dates
      include_undated   if True, includes facts with no event_time (useful
                        for library views where 'we have this fact but don't
                        know when' is meaningful)
    """
    from datetime import datetime, timezone, timedelta

    since_dt = datetime.now(timezone.utc) - timedelta(days=days)
    # max_year is current year + 1 so end-of-year edge cases work.
    max_year = datetime.now(timezone.utc).year + 1

    with get_connection() as conn:
        with conn.cursor() as cur:
            ent_filter: list[int] = []
            if library_id is not None:
                cur.execute(
                    "SELECT member_id FROM library_members "
                    " WHERE library_id = %s AND member_kind = 'entity'",
                    (library_id,),
                )
                ent_filter = [r[0] for r in cur.fetchall()]
                if not ent_filter:
                    return {"events": [], "count": 0}

            where: list[str] = []
            params: list[Any] = []

            if library_id is not None:
                where.append("(a.subj_entity_id = ANY(%s) OR a.obj_entity_id = ANY(%s))")
                params.extend([ent_filter, ent_filter])
            if tier:
                where.append("a.assessment_tier = %s")
                params.append(tier)

            # Event-time sanity. Two paths:
            #  - include_undated=True  → no event-time predicate at all
            #  - include_undated=False → must have event_time AND in range
            if not include_undated:
                where.append(
                    "COALESCE(a.latest_event_time, a.earliest_event_time) IS NOT NULL"
                )
                where.append(
                    "COALESCE(a.latest_event_time, a.earliest_event_time) >= %s"
                )
                params.append(since_dt)
                where.append(
                    "EXTRACT(year FROM COALESCE(a.latest_event_time, a.earliest_event_time)) BETWEEN %s AND %s"
                )
                params.extend([min_year, max_year])

            where_sql = ("WHERE " + " AND ".join(where)) if where else ""
            sql_limit = limit * 3 if not include_noise else limit

            cur.execute(
                f"""
                SELECT a.assessment_id, a.base60_address, a.assessment_tier,
                       a.subj_entity_id, ses.canonical_name AS subj,
                       ses.canonical_type AS subj_type,
                       a.predicate_edge_type,
                       a.obj_entity_id, oes.canonical_name AS obj,
                       oes.canonical_type AS obj_type, a.obj_value,
                       a.earliest_event_time, a.latest_event_time,
                       a.primary_location_entity_id,
                       loc.canonical_name AS location_name,
                       a.corroborated_by_count, a.contradicted_by_count,
                       a.confidence, a.source_statement_ids
                  FROM assessments a
                  LEFT JOIN entities ses ON ses.entity_id = a.subj_entity_id
                  LEFT JOIN entities oes ON oes.entity_id = a.obj_entity_id
                  LEFT JOIN entities loc ON loc.entity_id = a.primary_location_entity_id
                 {where_sql}
                 ORDER BY COALESCE(a.latest_event_time, a.earliest_event_time, a.first_seen) DESC
                 LIMIT %s
                """,
                (*params, sql_limit),
            )
            assessments = []
            for r in cur.fetchall():
                a = dict(zip([d[0] for d in cur.description], r))
                for k in ("earliest_event_time", "latest_event_time"):
                    if a.get(k) is not None:
                        a[k] = a[k].isoformat()
                a["sources"] = []
                assessments.append(a)
            if not include_noise:
                assessments = _filter_noisy_assessments(assessments)
            assessments = assessments[:limit]
            stmt_ids_pool: list[int] = [
                sid for a in assessments for sid in (a.get("source_statement_ids") or [])
            ]

            sources_by_stmt: dict[int, dict] = {}
            if stmt_ids_pool:
                cur.execute(
                    """
                    SELECT s.statement_id, s.raw_text, s.event_time,
                           s.document_id, d.title AS doc_title,
                           s.signal_rid, sig.payload->>'title' AS sig_title,
                           sig.payload->>'source' AS sig_source,
                           sig.payload->>'link' AS sig_link,
                           sig.timestamp AS sig_time
                      FROM statements s
                      LEFT JOIN documents d  ON d.document_id = s.document_id
                      LEFT JOIN signals  sig ON sig.rid = s.signal_rid
                     WHERE s.statement_id = ANY(%s)
                    """,
                    (stmt_ids_pool,),
                )
                for r in cur.fetchall():
                    sources_by_stmt[r[0]] = {
                        "statement_id": r[0],
                        "raw_text": r[1],
                        "event_time": r[2].isoformat() if r[2] else None,
                        "document_id": r[3],
                        "document_title": r[4],
                        "signal_rid": r[5],
                        "signal_title": r[6],
                        "signal_source": r[7],
                        "signal_link": r[8],
                        "signal_time": r[9].isoformat() if r[9] else None,
                    }

            # Filter by source_kind if requested
            if source_kind in ("pdf", "rss"):
                def matches(a):
                    for sid in a.get("source_statement_ids") or []:
                        s = sources_by_stmt.get(sid)
                        if not s: continue
                        if source_kind == "pdf" and s.get("document_id"): return True
                        if source_kind == "rss" and s.get("signal_rid"): return True
                    return False
                assessments = [a for a in assessments if matches(a)]

            for a in assessments:
                for sid in a.get("source_statement_ids") or []:
                    if sid in sources_by_stmt:
                        a["sources"].append(sources_by_stmt[sid])

            # Tag each assessment with libraries it touches (cross-library view)
            ent_ids: set[int] = set()
            for a in assessments:
                for k in ("subj_entity_id", "obj_entity_id", "primary_location_entity_id"):
                    v = a.get(k)
                    if v: ent_ids.add(v)
            libs_by_eid: dict[int, list] = {}
            if ent_ids:
                cur.execute(
                    """
                    SELECT lm.member_id, l.library_id, l.focal_term, l.base60_address
                      FROM library_members lm
                      JOIN libraries l ON l.library_id = lm.library_id
                     WHERE lm.member_kind = 'entity'
                       AND lm.member_id = ANY(%s)
                    """,
                    (list(ent_ids),),
                )
                for r in cur.fetchall():
                    libs_by_eid.setdefault(r[0], []).append({
                        "library_id": r[1],
                        "focal_term": r[2],
                        "address": r[3],
                    })
            for a in assessments:
                tagged: dict[int, dict] = {}
                for eid in [a.get("subj_entity_id"), a.get("obj_entity_id"),
                            a.get("primary_location_entity_id")]:
                    if eid and eid in libs_by_eid:
                        for lib in libs_by_eid[eid]:
                            tagged.setdefault(lib["library_id"], lib)
                a["libraries"] = list(tagged.values())

            # Entity blurbs
            blurb_by_eid: dict[int, str] = {}
            if ent_ids:
                cur.execute(
                    """
                    SELECT e.entity_id, kb.description
                      FROM entities e
                      JOIN entity_kb_cache kb ON LOWER(kb.canonical_name) = LOWER(e.canonical_name)
                     WHERE e.entity_id = ANY(%s)
                       AND kb.description IS NOT NULL
                       AND kb.description <> ''
                    """,
                    (list(ent_ids),),
                )
                for r in cur.fetchall():
                    eid, desc = r
                    if desc and eid not in blurb_by_eid:
                        blurb_by_eid[eid] = (desc.split(".")[0] + ".")[:200]
            for a in assessments:
                a["subj_blurb"] = blurb_by_eid.get(a.get("subj_entity_id"))
                a["obj_blurb"] = blurb_by_eid.get(a.get("obj_entity_id"))

    _apply_bindings(assessments)
    _enrich_display_names(assessments)

    return {"events": [_clean(a) for a in assessments], "count": len(assessments)}


# ─── Deep entity merge (handles assessments, statements, mentions, etc.) ──
# Distinct from /governance/merge-candidates (which is pg_trgm-similarity
# based and only updates entities+redirects). These endpoints use rules
# (initial+surname, normalized-form, no-space-concat) and redirect every
# downstream table so the substrate becomes truly honest.

@router.get("/governance/merge-candidates-deep")
def list_merge_candidates_deep(library_id: Optional[int] = None, limit: int = 50):
    from core.gdu.entity_merge import find_merge_candidates
    candidates = find_merge_candidates(library_id=library_id, limit=limit)
    return {"candidates": candidates, "count": len(candidates)}


@router.post("/governance/merge-preview-deep")
def preview_entity_merge_deep(body: dict):
    from core.gdu.entity_merge import preview_merge
    if "canonical_id" not in body or "duplicate_id" not in body:
        raise HTTPException(400, "canonical_id and duplicate_id required")
    return preview_merge(int(body["canonical_id"]), int(body["duplicate_id"]))


@router.post("/governance/merge-deep")
def execute_entity_merge_deep(body: dict):
    """Execute a deep merge — redirects assessments, statements, mentions,
    library_members, events, etc. Body: {canonical_id, duplicate_id, reason?}."""
    from core.gdu.entity_merge import merge_entities
    if "canonical_id" not in body or "duplicate_id" not in body:
        raise HTTPException(400, "canonical_id and duplicate_id required")
    return merge_entities(
        int(body["canonical_id"]),
        int(body["duplicate_id"]),
        reason=body.get("reason") or "operator_merge",
    )


@router.post("/governance/library-reextract")
def library_reextract(body: dict = None):
    """Targeted re-extraction for one library.

    Wipes the library's downstream substrate (mentions, statements,
    assessments tied to that library's documents) and re-runs the
    collapse pipeline on each document with the CURRENT prompts and
    filters. Used to A/B test prompt changes without contaminating
    other libraries.

    Body:
      library_id    int — required
      wipe          bool (default true) — clear existing rows first
      max_docs      int (default 50) — cap re-extracted docs (safety)
      dry_run       bool (default false) — return counts only
    """
    from core.workers.collapse_worker import process_document
    body = body or {}
    library_id = body.get("library_id")
    if library_id is None:
        raise HTTPException(400, "library_id required")
    library_id = int(library_id)
    wipe = bool(body.get("wipe", True))
    max_docs = int(body.get("max_docs", 50))
    dry_run = bool(body.get("dry_run", False))

    summary: dict = {
        "library_id": library_id, "wipe": wipe, "max_docs": max_docs,
        "before": {}, "after": {}, "docs_processed": 0,
    }

    with get_connection() as conn:
        doc_ids = _library_member_ids(conn, library_id, "document")
        if not doc_ids:
            return {"error": "no documents in library", **summary}
        doc_ids = doc_ids[:max_docs]
        with conn.cursor() as cur:
            # Snapshot "before" counts
            cur.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM document_mentions WHERE document_id = ANY(%s)),
                  (SELECT COUNT(*) FROM statements        WHERE document_id = ANY(%s)),
                  (SELECT COUNT(*) FROM assessments a
                    WHERE EXISTS (SELECT 1 FROM statements s
                                   WHERE s.statement_id = ANY(a.source_statement_ids)
                                     AND s.document_id = ANY(%s)))
                """,
                (doc_ids, doc_ids, doc_ids),
            )
            r = cur.fetchone()
            summary["before"] = {
                "mentions": int(r[0] or 0),
                "statements": int(r[1] or 0),
                "assessments": int(r[2] or 0),
            }

            if dry_run:
                return summary

            if wipe:
                # Wipe assessments whose source statements were ALL in this
                # library (avoid clobbering cross-library assessments).
                cur.execute(
                    """
                    DELETE FROM assessments a
                     WHERE EXISTS (SELECT 1 FROM statements s
                                    WHERE s.statement_id = ANY(a.source_statement_ids)
                                      AND s.document_id = ANY(%s))
                       AND NOT EXISTS (SELECT 1 FROM statements s
                                        WHERE s.statement_id = ANY(a.source_statement_ids)
                                          AND (s.document_id IS NULL OR s.document_id <> ALL(%s)))
                    """,
                    (doc_ids, doc_ids),
                )
                summary["wiped_assessments"] = cur.rowcount or 0
                cur.execute("DELETE FROM statements WHERE document_id = ANY(%s)", (doc_ids,))
                summary["wiped_statements"] = cur.rowcount or 0
                cur.execute("DELETE FROM document_mentions WHERE document_id = ANY(%s)", (doc_ids,))
                summary["wiped_mentions"] = cur.rowcount or 0
            conn.commit()

    # Re-extract each doc through the collapse worker
    per_doc: list[dict] = []
    for doc_id in doc_ids:
        try:
            stats = process_document(doc_id, library_id, batch_size=8)
            per_doc.append({"document_id": doc_id, **(stats or {})})
            summary["docs_processed"] += 1
        except Exception as e:
            per_doc.append({"document_id": doc_id, "error": str(e)})

    # Snapshot "after" counts
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM document_mentions WHERE document_id = ANY(%s)),
                  (SELECT COUNT(*) FROM statements        WHERE document_id = ANY(%s)),
                  (SELECT COUNT(*) FROM assessments a
                    WHERE EXISTS (SELECT 1 FROM statements s
                                   WHERE s.statement_id = ANY(a.source_statement_ids)
                                     AND s.document_id = ANY(%s)))
                """,
                (doc_ids, doc_ids, doc_ids),
            )
            r = cur.fetchone()
            summary["after"] = {
                "mentions": int(r[0] or 0),
                "statements": int(r[1] or 0),
                "assessments": int(r[2] or 0),
            }
    summary["sample_docs"] = per_doc[:5]
    return summary


@router.post("/governance/merge-batch")
def execute_merge_batch(body: dict = None):
    """Auto-merge all duplicate-entity candidates above a confidence floor.
    Body:
      min_confidence: float (default 0.95 = identical canonical_name only)
      library_id: int (optional, scope to one library)
      dry_run: bool (default false; if true, returns what WOULD merge)
      limit: int (default 200, max candidates to process)
    Returns counts + a sample of merges performed.
    """
    from core.gdu.entity_merge import find_merge_candidates, merge_entities
    body = body or {}
    min_conf = float(body.get("min_confidence", 0.95))
    library_id = body.get("library_id")
    dry_run = bool(body.get("dry_run", False))
    limit = int(body.get("limit", 200))

    candidates = find_merge_candidates(
        library_id=int(library_id) if library_id is not None else None,
        limit=limit,
    )
    eligible = [c for c in candidates if c["confidence"] >= min_conf]

    if dry_run:
        return {
            "dry_run": True,
            "would_merge": len(eligible),
            "sample": eligible[:10],
        }

    merged = 0
    failed = 0
    log_rows: list[dict] = []
    for c in eligible:
        result = merge_entities(
            c["survivor_id"], c["duplicate_id"],
            reason=f"batch_merge: {c['rationale']}",
        )
        if result.get("ok"):
            merged += 1
        else:
            failed += 1
        log_rows.append({
            "survivor_id": c["survivor_id"], "duplicate_id": c["duplicate_id"],
            "ok": result.get("ok", False),
            "error": result.get("error"),
            "rationale": c["rationale"],
        })
    return {
        "considered": len(eligible),
        "merged": merged,
        "failed": failed,
        "log": log_rows[:20],
    }


# ─── Anaphora bindings (the substrate's "what does the house mean here") ──

@router.post("/bindings/scan")
def bindings_scan(body: dict = None):
    """Scan a library for resolvable placeholder→entity bindings. Writes
    bindings into context_bindings. Idempotent.

    Body: {"library_id": int, "llm_validate": bool=true}.
    When llm_validate=true (default), each candidate binding is confirmed
    by Gemma before write. Pass false for a fast heuristic-only run.
    """
    body = body or {}
    library_id = body.get("library_id")
    if library_id is None:
        raise HTTPException(400, "library_id required")
    return anaphora_resolver.scan_library(
        int(library_id),
        llm_validate=bool(body.get("llm_validate", True)),
    )


@router.get("/bindings")
def bindings_list(
    placeholder: Optional[str] = None,
    document_id: Optional[str] = None,
    limit: int = 100,
):
    """Browse learned bindings. Filterable by placeholder or document."""
    where: list[str] = []
    params: list = []
    if placeholder:
        where.append("placeholder = %s")
        params.append(placeholder.strip().lower())
    if document_id:
        where.append("context_value = %s")
        params.append(str(document_id))
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT b.binding_id, b.placeholder, b.context_scope, b.context_value,
                       b.resolved_entity_id, e.canonical_name, e.canonical_type,
                       b.learned_from, b.confidence, b.evidence_count,
                       b.created_at, b.updated_at
                  FROM context_bindings b
                  LEFT JOIN entities e ON e.entity_id = b.resolved_entity_id
                 {where_sql}
                 ORDER BY b.updated_at DESC
                 LIMIT %s
                """,
                params,
            )
            rows = []
            for r in cur.fetchall():
                rows.append({
                    "binding_id": r[0], "placeholder": r[1],
                    "context_scope": r[2], "context_value": r[3],
                    "resolved_entity_id": r[4], "resolved_name": r[5],
                    "resolved_type": r[6],
                    "learned_from": r[7], "confidence": float(r[8]),
                    "evidence_count": r[9],
                    "created_at": r[10].isoformat() if r[10] else None,
                    "updated_at": r[11].isoformat() if r[11] else None,
                })
    return {"bindings": rows, "count": len(rows)}


@router.post("/bindings/override")
def bindings_override(body: dict):
    """Human override — write/replace a binding at confidence 1.0.
    Body: {placeholder, context_scope ('document'|'library'|'topic'),
           context_value, entity_id, note?}.
    The override wins over any auto-bound resolution because of
    ORDER BY confidence DESC in the lookup."""
    required = ("placeholder", "context_scope", "context_value", "entity_id")
    missing = [k for k in required if k not in body]
    if missing:
        raise HTTPException(400, f"missing: {missing}")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO context_bindings
                    (placeholder, context_scope, context_value, resolved_entity_id,
                     learned_from, confidence, evidence_count, created_by)
                VALUES (%s, %s, %s, %s, 'human_override', 1.0, 1, 'operator')
                ON CONFLICT (tenant_id, placeholder, context_scope, context_value)
                DO UPDATE
                  SET resolved_entity_id = EXCLUDED.resolved_entity_id,
                      learned_from = 'human_override',
                      confidence = 1.0,
                      evidence_count = context_bindings.evidence_count + 1,
                      updated_at = NOW()
                RETURNING binding_id
                """,
                (
                    body["placeholder"].strip().lower(),
                    body["context_scope"], str(body["context_value"]),
                    int(body["entity_id"]),
                ),
            )
            bid = cur.fetchone()[0]
        conn.commit()
    return {"binding_id": bid, "status": "saved"}


@router.delete("/bindings/{binding_id}")
def bindings_delete(binding_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM context_bindings WHERE binding_id = %s", (binding_id,))
            n = cur.rowcount
        conn.commit()
    return {"deleted": n}


# ─── Library project runs ─────────────────────────────────────────────────

@router.post("/libraries/{library_id}/run")
def start_library_run(library_id: int, body: dict = None):
    """Queue a project run for this library. Manager picks it up within 30s."""
    from core.workers.library_manager import start_run
    body = body or {}
    return start_run(
        library_id,
        priority=int(body.get("priority", 100)),
        started_by=body.get("started_by", "operator"),
        note=body.get("note", ""),
    )


@router.post("/library-runs/{run_id}/pause")
def pause_library_run(run_id: int):
    from core.workers.library_manager import pause_run
    return pause_run(run_id)


@router.post("/library-runs/{run_id}/resume")
def resume_library_run(run_id: int):
    from core.workers.library_manager import resume_run
    return resume_run(run_id)


@router.post("/library-runs/{run_id}/cancel")
def cancel_library_run(run_id: int):
    from core.workers.library_manager import cancel_run
    return cancel_run(run_id)


@router.post("/library-runs/{run_id}/advance")
def advance_library_run(run_id: int):
    from core.workers.library_manager import advance_run
    return advance_run(run_id)


@router.get("/library-runs")
def list_library_runs(status: Optional[str] = None, limit: int = 20):
    """List runs filtered by status. status=None returns all."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            sql = """
                SELECT r.run_id, r.library_id, l.focal_term,
                       r.run_status, r.current_phase,
                       r.priority, r.queued_at, r.started_at,
                       r.phase_started_at, r.last_progress_at, r.completed_at,
                       r.metrics, r.started_by, r.note
                  FROM library_runs r
                  JOIN libraries l ON l.library_id = r.library_id
            """
            params: list[Any] = []
            if status:
                sql += " WHERE r.run_status = %s"
                params.append(status)
            sql += " ORDER BY r.queued_at DESC LIMIT %s"
            params.append(limit)
            cur.execute(sql, params)
            rows = []
            for r in cur.fetchall():
                rows.append({
                    "run_id": r[0], "library_id": r[1], "focal_term": r[2],
                    "run_status": r[3], "current_phase": r[4],
                    "priority": r[5],
                    "queued_at": r[6].isoformat() if r[6] else None,
                    "started_at": r[7].isoformat() if r[7] else None,
                    "phase_started_at": r[8].isoformat() if r[8] else None,
                    "last_progress_at": r[9].isoformat() if r[9] else None,
                    "completed_at": r[10].isoformat() if r[10] else None,
                    "metrics": r[11] or {},
                    "started_by": r[12], "note": r[13],
                })
    return {"runs": rows, "count": len(rows)}


# ─── Research a person/company (manual investigation trigger) ─────────────

@router.post("/entities/{entity_id}/research")
def trigger_entity_research(entity_id: int, body: dict = None):
    """Queue or synchronously run an investigation against this entity.

    Body params:
      force          bool, skip the 1-hour cooldown
      priority       int, lower = sooner (default 100, UI-live = 10)
      discovered_from text, audit trail (default 'manual_trigger')
      auto           bool, if true, returns {queued: false} silently when
                     a recent or active investigation exists (no error,
                     no new row). Used by auto-trigger-on-view.
      sync           bool, if true, run the investigation INLINE here
                     instead of queueing it for a worker. Returns the
                     complete result (findings, status). Use this on
                     user-initiated /entity/[id] views so the dossier
                     populates in seconds instead of waiting behind the
                     background queue. Added June 3 2026.
    """
    body = body or {}
    force = bool(body.get("force", False))
    auto = bool(body.get("auto", False))
    sync = bool(body.get("sync", False))
    priority = int(body.get("priority", 100))
    discovered_from = body.get("discovered_from") or (
        "auto_view" if auto else "manual_trigger"
    )
    with get_connection() as conn:
        # Follow redirects: if entity was merged into another, research
        # the canonical survivor with the better name.
        entity_id = _follow_entity_redirect(conn, entity_id)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT canonical_name, canonical_type FROM entities WHERE entity_id=%s",
                (entity_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="entity not found")
            name, ctype = row
            ctype = ctype or "person"

            # Cooldown — manual triggers wait 1h, auto-view waits 24h.
            # sync=true ALWAYS bypasses cooldown: user is in front of the
            # screen and an existing 'pending' row from a previous click
            # is exactly the situation that traps the dossier on
            # 'researching live…' forever. Running inline now gives them
            # findings in 5-15s and updates the pending row in place.
            cooldown_interval = "24 hours" if auto else "1 hour"
            if not force and not sync:
                cur.execute(
                    f"""
                    SELECT investigation_id, status
                      FROM entity_investigations
                     WHERE entity_id = %s
                       AND (status IN ('pending', 'running')
                            OR (status = 'complete'
                                AND completed_at > NOW() - INTERVAL '{cooldown_interval}'))
                     ORDER BY started_at DESC NULLS LAST
                     LIMIT 1
                    """,
                    (entity_id,),
                )
                existing = cur.fetchone()
                if existing:
                    return {
                        "queued": False,
                        "existing": True,
                        "investigation_id": existing[0],
                        "status": existing[1],
                    }

            # Synchronous mode: skip the queue and run the investigation
            # right here. The endpoint blocks until the run finishes (or
            # errors). Used by /entity/[id] click-through so users don't
            # wait behind the background queue of 90K+ pending items.
            if sync:
                # close this transaction first — run_investigation_sync
                # opens its own connections and we want lock-free access
                pass

        if sync:
            conn.commit()
            try:
                from core.workers.investigation import run_investigation_sync
                result = run_investigation_sync(
                    name=name,
                    entity_type=ctype,
                    entity_id=entity_id,
                )
                # Normalise the response shape so the UI can treat sync
                # and queued returns uniformly.
                return {
                    "queued": False,
                    "sync": True,
                    "investigation_id": result.get("investigation_id"),
                    "entity_name": name,
                    "entity_type": ctype,
                    "status": result.get("status", "complete"),
                    "findings_count": result.get("findings_count", 0),
                    "result": result,
                }
            except Exception as e:
                log.exception("synchronous entity research failed")
                raise HTTPException(status_code=500, detail=f"sync research failed: {e}")

        with conn.cursor() as cur:
            # ON CONFLICT against migration 119's partial unique index —
            # if a concurrent click already queued, return that row
            # instead of erroring.
            cur.execute(
                """
                INSERT INTO entity_investigations
                    (tenant_id, entity_id, seed_name, seed_type, status,
                     sources_requested, depth, discovered_from, priority)
                VALUES ('default', %s, %s, %s, 'pending', NULL, 0, %s, %s)
                ON CONFLICT (tenant_id, entity_id)
                   WHERE entity_id IS NOT NULL
                     AND status IN ('pending', 'running')
                   DO NOTHING
                RETURNING investigation_id
                """,
                (entity_id, name, ctype, discovered_from, priority),
            )
            row = cur.fetchone()
            if row:
                inv_id = row[0]
            else:
                cur.execute(
                    """
                    SELECT investigation_id FROM entity_investigations
                     WHERE tenant_id='default' AND entity_id=%s
                       AND status IN ('pending', 'running')
                     ORDER BY created_at DESC LIMIT 1
                    """,
                    (entity_id,),
                )
                inv_id = cur.fetchone()[0]
        conn.commit()
    return {"queued": True, "investigation_id": inv_id,
            "entity_name": name, "entity_type": ctype, "status": "pending",
            "priority": priority}


@router.get("/entities/{entity_id}/investigations")
def list_entity_investigations(entity_id: int, limit: int = 10,
                                include_noise: bool = False):
    """List past + current investigations for this entity + summary of findings."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT investigation_id, status, started_at, completed_at,
                       sources_requested, sources_completed,
                       findings_count, error, depth, discovered_from
                  FROM entity_investigations
                 WHERE entity_id = %s
                 ORDER BY started_at DESC NULLS LAST
                 LIMIT %s
                """,
                (entity_id, limit),
            )
            invs = []
            for r in cur.fetchall():
                invs.append({
                    "investigation_id": r[0],
                    "status": r[1],
                    "started_at": r[2].isoformat() if r[2] else None,
                    "completed_at": r[3].isoformat() if r[3] else None,
                    "sources_requested": r[4],
                    "sources_completed": r[5],
                    "findings_count": r[6],
                    "error": r[7],
                    "depth": r[8],
                    "discovered_from": r[9],
                })

            # Findings can be linked either directly (entity_id = seed) or
            # via their investigation. Pull both so the dossier sees every
            # row produced by any investigation seeded against this entity.
            # Same noise gate as /entities/{id}/story.
            min_relevance = 0.0 if include_noise else 0.3
            cur.execute(
                """
                SELECT f.source, f.finding_type, f.title,
                       f.data->>'url' AS source_url,
                       f.confidence, f.relevance_score
                  FROM investigation_findings f
                  JOIN entity_investigations i
                    ON i.investigation_id = f.investigation_id
                 WHERE (f.entity_id = %s OR i.entity_id = %s)
                   AND (f.relevance_score IS NULL OR f.relevance_score >= %s)
                 ORDER BY COALESCE(f.relevance_score, f.confidence) DESC NULLS LAST,
                          f.finding_id DESC
                 LIMIT 30
                """,
                (entity_id, entity_id, min_relevance),
            )
            findings = []
            for r in cur.fetchall():
                findings.append({
                    "source": r[0],
                    "finding_type": r[1],
                    "summary": r[2],         # column is `title` server-side
                    "source_url": r[3],
                    "confidence": r[4],
                    "relevance_score": r[5],
                })
    return {"investigations": invs, "findings": findings,
            "investigation_count": len(invs), "finding_count": len(findings)}


@router.get("/entities/{entity_id}/federation")
def get_entity_federation(entity_id: int):
    """Per-entity federation provenance for the entity dossier UI.

    Surfaces the cross-node trust story for one entity:
      • canonical_entity_key + which other peers have a row for it
      • mention breakdown by pulled_from_node_id (local vs federated)
      • envelopes that contributed mentions, with signed/unsigned status
      • analyst notes attached to this entity (subject_kind='entity',
        subject_ref=canonical_entity_key)

    Without this, an analyst sees "47 mentions" but has no idea which
    of those 47 came from node-A's local LLM vs node-B's federated pull.
    """
    with get_connection() as conn:
        entity_id = _follow_entity_redirect(conn, entity_id)
        with conn.cursor() as cur:
            # Canonical identity
            cur.execute(
                """SELECT canonical_entity_key, canonical_name, canonical_type,
                          base60_address, pulled_from_node_id, created_at
                     FROM entities WHERE entity_id = %s""",
                (entity_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, f"entity {entity_id} not found")
            (ckey, name, etype, b60, pulled_from, created_at) = row

            # Per-producer mention breakdown — locally extracted (no pull
            # provenance) vs federated (pulled_from_node_id IS NOT NULL).
            cur.execute(
                """
                SELECT COALESCE(pulled_from_node_id, '__local__') AS source,
                       COUNT(*) AS cnt
                  FROM entity_mentions
                 WHERE entity_id = %s
                 GROUP BY 1
                 ORDER BY cnt DESC
                """,
                (entity_id,),
            )
            mention_sources = [
                {"source": ("local" if r[0] == "__local__" else r[0]),
                 "count":  int(r[1])}
                for r in cur.fetchall()
            ]
            local_mention_count    = sum(m["count"] for m in mention_sources if m["source"] == "local")
            pulled_mention_count   = sum(m["count"] for m in mention_sources if m["source"] != "local")

            # Envelopes that touched this entity. Group by producer +
            # signed/unsigned so the analyst sees "47 mentions from 3
            # envelopes by 2 peers, 2 signed / 1 unsigned".
            cur.execute(
                """
                SELECT pulled_from_envelope_sha,
                       pulled_from_node_id,
                       COUNT(*)                          AS mentions_from_envelope,
                       MIN(em.created_at)                AS first_seen
                  FROM entity_mentions em
                 WHERE entity_id = %s
                   AND pulled_from_envelope_sha IS NOT NULL
                 GROUP BY pulled_from_envelope_sha, pulled_from_node_id
                 ORDER BY first_seen DESC
                 LIMIT 30
                """,
                (entity_id,),
            )
            envelopes = []
            for r in cur.fetchall():
                envelopes.append({
                    "envelope_sha":            r[0],
                    "producer_node_id":        r[1],
                    "mentions_from_envelope":  int(r[2]),
                    "first_seen":              r[3].isoformat() if r[3] else None,
                })

            # Notes about this entity (subject_kind='entity',
            # subject_ref=canonical_entity_key). Only returns if
            # ckey is set — entities without a name have no key.
            notes = []
            if ckey:
                cur.execute(
                    """
                    SELECT note_sha256, library_federation_id, library_id,
                           parent_note_sha, author_node_id, author_user_id,
                           author_display_name, body, body_format, visibility,
                           pulled_from_node_id, is_deletion_marker, created_at
                      FROM library_notes
                     WHERE subject_kind = 'entity'
                       AND subject_ref = %s
                       AND is_deletion_marker = FALSE
                     ORDER BY created_at DESC
                     LIMIT 50
                    """,
                    (ckey,),
                )
                for r in cur.fetchall():
                    notes.append({
                        "note_sha256":          r[0],
                        "library_federation_id": str(r[1]) if r[1] else None,
                        "library_id":           int(r[2]) if r[2] else None,
                        "parent_note_sha":      r[3],
                        "author_node_id":       r[4],
                        "author_user_id":       r[5],
                        "author_display_name":  r[6],
                        "body":                 r[7],
                        "body_format":          r[8],
                        "visibility":           r[9],
                        "pulled_from_node_id":  r[10],
                        "created_at":           r[12].isoformat() if r[12] else None,
                    })

    # Trust summary one-liner the UI can render up top
    total_mentions = local_mention_count + pulled_mention_count
    if total_mentions == 0:
        trust_story = "No mentions yet."
    elif pulled_mention_count == 0:
        trust_story = f"All {local_mention_count} mentions extracted locally."
    elif local_mention_count == 0:
        trust_story = (f"All {pulled_mention_count} mentions pulled from the "
                       f"federation ({len(mention_sources)} peer(s)).")
    else:
        peers = len(mention_sources) - 1  # minus the 'local' bucket
        trust_story = (f"{local_mention_count} local + {pulled_mention_count} federated "
                       f"mentions (from {peers} peer{'s' if peers != 1 else ''}).")

    return {
        "entity_id":              entity_id,
        "canonical_entity_key":   ckey,
        "canonical_name":         name,
        "canonical_type":         etype,
        "base60_address":         b60,
        "pulled_from_node_id":    pulled_from,
        "created_at":             created_at.isoformat() if created_at else None,
        "trust_story":            trust_story,
        "mention_total":          total_mentions,
        "local_mention_count":    local_mention_count,
        "pulled_mention_count":   pulled_mention_count,
        "mention_sources":        mention_sources,
        "envelopes":              envelopes,
        "envelope_count":         len(envelopes),
        "notes":                  notes,
        "note_count":             len(notes),
    }
