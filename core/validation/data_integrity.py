"""
Data integrity checks across the derived extraction tables.

The validators in this module run read-only queries to surface
inconsistencies in the data we've built up from the source documents:

  - Statements that lost their source attribution (NULL document_id,
    NULL char_start)
  - Statements with impossible event_time values (future-dated past
    a reasonable horizon, pre-modern era)
  - Mentions still unresolved to entities (the resolver hasn't bound
    them, OR they're inherently un-resolvable)
  - Entities with duplicate canonical_names (dedup candidates)
  - Knowledge edges with self-loops, NULL endpoints
  - Library counters that drift from the actual member count
  - Promoted statements whose speaker / object entity ids don't exist
    in the entities table

Each check returns a `Finding` with:

  name        — short identifier
  severity    — info | warn | error | critical
  count       — how many rows match
  sample      — up to 5 example rows for human inspection
  description — what the check looks for
  fix_hint    — one-line suggestion of how to remediate
  query_ms    — wall time for the check

Nothing is mutated. The caller (an endpoint or a CLI) decides what to
do with the findings.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable

from storage.postgres.db import get_connection


@dataclass
class Finding:
    name: str
    severity: str           # info | warn | error | critical
    count: int
    sample: list[dict]
    description: str
    fix_hint: str
    query_ms: int = 0


def _run(check: Callable[[], Finding]) -> Finding:
    t = time.time()
    out = check()
    out.query_ms = int((time.time() - t) * 1000)
    return out


def _scalar(cur, sql: str, params: tuple = ()) -> int:
    cur.execute(sql, params)
    r = cur.fetchone()
    return int(r[0]) if r and r[0] is not None else 0


def _rows(cur, sql: str, params: tuple = (), limit: int = 5) -> list[dict]:
    cur.execute(sql + f" LIMIT {limit}", params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ─── STATEMENTS ────────────────────────────────────────────────────────


def check_statements_without_doc() -> Finding:
    """A statement with no document_id has no source attribution. We can't
    click through to evidence. This is fine for signal-feed extractions
    (which use signal_rid instead), so we exclude those."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            n = _scalar(cur, """
                SELECT COUNT(*) FROM statements
                 WHERE document_id IS NULL AND signal_rid IS NULL
            """)
            sample = _rows(cur, """
                SELECT statement_id, predicate, validation_status,
                       extraction_method, LEFT(raw_text, 80) AS raw_text
                  FROM statements
                 WHERE document_id IS NULL AND signal_rid IS NULL
                 ORDER BY statement_id DESC
            """)
    return Finding(
        name="statements_no_source",
        severity="error" if n > 0 else "info",
        count=n,
        sample=sample,
        description="Statements with neither document_id nor signal_rid — no traceable origin",
        fix_hint="Quarantine these (validation_status='rejected') or backfill source if known",
    )


def check_statements_without_span() -> Finding:
    """A statement without char_start/char_end can't be highlighted in
    the document viewer. Older extractors didn't track spans; new ones
    should. We're not going to fix the historical ones — just measure
    so the count stops growing."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            n = _scalar(cur, """
                SELECT COUNT(*) FROM statements
                 WHERE document_id IS NOT NULL AND char_start IS NULL
            """)
            sample = _rows(cur, """
                SELECT extraction_method, COUNT(*) AS n
                  FROM statements
                 WHERE document_id IS NOT NULL AND char_start IS NULL
                 GROUP BY extraction_method ORDER BY n DESC
            """)
    return Finding(
        name="statements_no_span",
        severity="warn" if n > 0 else "info",
        count=n,
        sample=sample,
        description="Statements that have a document_id but lost their char_start span — UI can't jump to source",
        fix_hint="Track by extraction_method; ensure new extractors emit spans. Historical ones survive but lose click-to-source",
    )


def check_promoted_without_speaker_entity() -> Finding:
    """A promoted statement should resolve to a real speaker entity.
    NULL speaker_entity_id on a promoted statement means the resolver
    hasn't bound it yet OR the surface form didn't match any entity."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            n = _scalar(cur, """
                SELECT COUNT(*) FROM statements
                 WHERE validation_status = 'promoted'
                   AND speaker_entity_id IS NULL
                   AND document_id IS NOT NULL
            """)
            sample = _rows(cur, """
                SELECT statement_id, document_id, predicate, extraction_method,
                       LEFT(raw_text, 80) AS raw_text
                  FROM statements
                 WHERE validation_status = 'promoted'
                   AND speaker_entity_id IS NULL
                   AND document_id IS NOT NULL
                 ORDER BY statement_id DESC
            """)
    return Finding(
        name="promoted_statements_unresolved_speaker",
        severity="warn" if n > 0 else "info",
        count=n,
        sample=sample,
        description="Promoted statements whose speaker hasn't been bound to an entity yet",
        fix_hint="Run the entity resolver worker; or trigger /api/entities/bulk-resolve",
    )


def check_temporal_impossibilities() -> Finding:
    """event_time that's clearly wrong: way-future or pre-modern. We
    don't care that '2027 Mercedes' is in a news article — the doc
    DATE is the relevant timestamp. event_time should describe when
    the event happened, not what year the article mentions."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            n = _scalar(cur, """
                SELECT COUNT(*) FROM statements
                 WHERE event_time IS NOT NULL
                   AND (event_time > NOW() + INTERVAL '1 year'
                        OR event_time < '1900-01-01')
                   AND validation_status = 'promoted'
            """)
            sample = _rows(cur, """
                SELECT statement_id, event_time, validation_status,
                       extraction_method, LEFT(raw_text, 80) AS raw_text
                  FROM statements
                 WHERE event_time IS NOT NULL
                   AND (event_time > NOW() + INTERVAL '1 year'
                        OR event_time < '1900-01-01')
                   AND validation_status = 'promoted'
                 ORDER BY event_time DESC
            """)
    return Finding(
        name="temporal_impossibilities_promoted",
        severity="error" if n > 0 else "info",
        count=n,
        sample=sample,
        description="Promoted statements with event_time > 1 year future OR before 1900",
        fix_hint="These should be demoted; the extractor confused a year-mentioned-in-text with the event time",
    )


def check_temporal_impossibilities_all() -> Finding:
    """Same as above but counting rejected ones too — those don't pollute
    the dossier (they're hidden) but they tell us where extractors are
    confused."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            future = _scalar(cur, """
                SELECT COUNT(*) FROM statements
                 WHERE event_time > NOW() + INTERVAL '1 year'
            """)
            pre1900 = _scalar(cur, """
                SELECT COUNT(*) FROM statements
                 WHERE event_time < '1900-01-01'
            """)
    return Finding(
        name="temporal_impossibilities_total",
        severity="info",
        count=future + pre1900,
        sample=[{"future_dated": future, "pre_1900": pre1900}],
        description="All statements (incl rejected) with impossible event_time — extractor-quality signal",
        fix_hint="Tune the LLM extractor's date prompt to bind event_time to documented event, not text-mentioned years",
    )


# ─── MENTIONS ──────────────────────────────────────────────────────────


def check_mentions_unresolved_by_type() -> Finding:
    """Most unresolved mentions are fine — they'll bind on the next
    resolver pass. But if a high-confidence person mention has been
    sitting unresolved for a while, that's a resolver gap."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            n = _scalar(cur, """
                SELECT COUNT(*) FROM document_mentions
                 WHERE entity_id IS NULL
                   AND suggested_type IN ('person','organization','place','facility')
                   AND COALESCE(confidence, 0) >= 0.8
                   AND created_at < NOW() - INTERVAL '1 day'
            """)
            sample = _rows(cur, """
                SELECT mention_id, document_id, surface_form, suggested_type,
                       confidence, created_at::DATE
                  FROM document_mentions
                 WHERE entity_id IS NULL
                   AND suggested_type IN ('person','organization','place','facility')
                   AND COALESCE(confidence, 0) >= 0.8
                   AND created_at < NOW() - INTERVAL '1 day'
                 ORDER BY confidence DESC
            """)
    return Finding(
        name="high_confidence_mentions_stuck_unresolved",
        severity="warn" if n > 0 else "info",
        count=n,
        sample=sample,
        description="High-confidence (>=0.8) person/org/place mentions that haven't been bound to entities after 24h",
        fix_hint="Run entity resolver; check if the canonical entity exists and the normaliser matches its key",
    )


# ─── ENTITIES ──────────────────────────────────────────────────────────


def check_duplicate_canonical_names() -> Finding:
    """Two entities with the same canonical_name + canonical_type are
    likely duplicates that the resolver missed."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            n = _scalar(cur, """
                SELECT COUNT(*) FROM (
                    SELECT canonical_name, canonical_type, COUNT(*) AS c
                      FROM entities
                     WHERE canonical_name IS NOT NULL
                     GROUP BY canonical_name, canonical_type
                    HAVING COUNT(*) > 1
                ) d
            """)
            sample = _rows(cur, """
                SELECT canonical_name, canonical_type, COUNT(*) AS n,
                       array_agg(entity_id ORDER BY signal_count DESC) AS entity_ids
                  FROM entities
                 WHERE canonical_name IS NOT NULL
                 GROUP BY canonical_name, canonical_type
                HAVING COUNT(*) > 1
                ORDER BY n DESC
            """)
    return Finding(
        name="duplicate_canonical_names",
        severity="warn" if n > 0 else "info",
        count=n,
        sample=sample,
        description="Entities sharing the same (canonical_name, canonical_type) — likely missed dedup",
        fix_hint="Merge via entity_redirects: pick highest-signal_count as canonical, redirect the others",
    )


def check_entities_zero_signal() -> Finding:
    """Entities with signal_count == 0 either never had a mention or
    had their mentions wiped. They're orphans in the entity catalog.

    Excludes rows tombstoned by cleanup scripts (canonical_name
    suffixed with '[merged→N]', '[needs_review]', or '[orphan]') —
    those are intentional zero-signal markers, not failures."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            n = _scalar(cur, """
                SELECT COUNT(*) FROM entities
                 WHERE COALESCE(signal_count, 0) = 0
                   AND COALESCE(canonical_name, '') NOT LIKE '%%[merged%%'
                   AND COALESCE(canonical_name, '') NOT LIKE '%%[needs_review]%%'
                   AND COALESCE(canonical_name, '') NOT LIKE '%%[orphan]%%'
                   AND COALESCE(canonical_name, '') NOT LIKE '%%[noise]%%'
            """)
            sample = _rows(cur, """
                SELECT entity_id, canonical_name, canonical_type, created_at::DATE
                  FROM entities
                 WHERE COALESCE(signal_count, 0) = 0
                   AND COALESCE(canonical_name, '') NOT LIKE '%%[merged%%'
                   AND COALESCE(canonical_name, '') NOT LIKE '%%[needs_review]%%'
                   AND COALESCE(canonical_name, '') NOT LIKE '%%[orphan]%%'
                   AND COALESCE(canonical_name, '') NOT LIKE '%%[noise]%%'
                 ORDER BY created_at DESC
            """)
    return Finding(
        name="entities_zero_signal",
        severity="warn" if n > 0 else "info",
        count=n,
        sample=sample,
        description="Entities with signal_count = 0 — orphans, never mentioned or mention-stripped",
        fix_hint="If recently created, give resolver a pass. If old, candidate for deletion or merge",
    )


# ─── KNOWLEDGE EDGES ───────────────────────────────────────────────────


def check_edges_self_loops() -> Finding:
    """A relationship from X to X is almost always a bug (the
    co-occurrence sweep didn't filter A != B)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            n = _scalar(cur, "SELECT COUNT(*) FROM knowledge_edges WHERE source_node_id = target_node_id")
            sample = _rows(cur, """
                SELECT edge_id, source_node_id, relationship, created_at::DATE
                  FROM knowledge_edges
                 WHERE source_node_id = target_node_id
                 ORDER BY edge_id DESC
            """)
    return Finding(
        name="edges_self_loops",
        severity="error" if n > 0 else "info",
        count=n,
        sample=sample,
        description="Edges where source == target — bug in the co-occurrence / extraction sweep",
        fix_hint="DELETE FROM knowledge_edges WHERE source_node_id = target_node_id",
    )


def check_edges_dangling_endpoints() -> Finding:
    """Edges where one endpoint's namespaced ID can't be resolved to a
    real row. node_ids are formatted as '<kind>:<id>' (entity:312,
    document:sig_4, event:8faa..., assessment:71ca...). Each kind has a
    different home table; we only validate the entity: prefix here since
    that's the bulk of bound edges. document:/event:/assessment: live
    in other tables outside this check's scope."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Limit to entity:N edges so we don't false-positive on
            # document/event/assessment edges (which use string-keyed
            # nodes that aren't in `entities`).
            n = _scalar(cur, """
                SELECT COUNT(*) FROM knowledge_edges ke
                 WHERE (ke.source_node_id LIKE 'entity:%%'
                       AND NOT EXISTS (
                           SELECT 1 FROM entities e
                            WHERE 'entity:' || e.entity_id::TEXT = ke.source_node_id
                       ))
                    OR (ke.target_node_id LIKE 'entity:%%'
                       AND NOT EXISTS (
                           SELECT 1 FROM entities e
                            WHERE 'entity:' || e.entity_id::TEXT = ke.target_node_id
                       ))
            """)
            sample = _rows(cur, """
                SELECT edge_id, source_node_id, target_node_id, relationship
                  FROM knowledge_edges ke
                 WHERE (ke.source_node_id LIKE 'entity:%%'
                       AND NOT EXISTS (
                           SELECT 1 FROM entities e
                            WHERE 'entity:' || e.entity_id::TEXT = ke.source_node_id
                       ))
                    OR (ke.target_node_id LIKE 'entity:%%'
                       AND NOT EXISTS (
                           SELECT 1 FROM entities e
                            WHERE 'entity:' || e.entity_id::TEXT = ke.target_node_id
                       ))
            """)
    return Finding(
        name="edges_dangling_entity_endpoints",
        severity="error" if n > 0 else "info",
        count=n,
        sample=sample,
        description="Edges where an entity:N endpoint references a non-existent entity_id (excludes document/event/assessment edges)",
        fix_hint="Likely from a merge that didn't follow entity_redirects; delete or re-resolve via the redirect chain",
    )


# ─── DOCUMENTS ─────────────────────────────────────────────────────────


# Predicate type-violation check uses the GDU category hierarchy: a
# canonical_type like 'person' or 'city' is a subtype of one of these
# top-level GDU categories. When a constraint says
# source_types={entity}, any subtype rolled up to `entity` is valid.
#
# Built from nodevocab so it stays in sync with the pack as new subtypes
# are added.
_CATEGORY_OF_TYPE: dict[str, str] = {
    # entity category — agents, organizations, assets
    "person":             "entity",
    "organization":       "entity",
    "government_agency":  "entity",
    "mass_media":         "entity",
    "university":         "entity",
    "research_institute": "entity",
    "company":            "entity",
    "vessel":             "entity",
    "aircraft":           "entity",
    "airliner":           "entity",
    "spacecraft":         "entity",
    "vehicle":            "entity",
    "facility":           "entity",
    "product":            "entity",
    "weapon":             "entity",
    "weapon_system":      "entity",
    "military_unit":      "entity",
    "account":            "entity",
    "device":             "entity",
    "unit":               "entity",
    "power_plant":        "entity",
    "org":                "entity",
    # place category — geographic
    "country":            "place",
    "admin_region":       "place",
    "region":             "place",
    "city":               "place",
    "place":              "place",
    "poi":                "place",
    "facility_location":  "place",
    "named_area":         "place",
    "maritime_area":      "place",
    "airspace":           "place",
    "airport":            "place",
    "railway_station":    "place",
    "river":              "place",
    "sea":                "place",
    "ocean":              "place",
    "strait":             "place",
    "lake":               "place",
    "island":             "place",
    "archipelago":        "place",
    "peninsula":          "place",
    "continent":          "place",
    "bridge":             "place",
    "canal":              "place",
    # event, topic, etc — keep mapping minimal until needed
    "event":              "event",
    "concept":            "topic",
    "date":               "chronicle",
}


def _type_matches(canonical_type: str, allowed: list[str]) -> bool:
    """True if canonical_type matches any of allowed — either directly
    (city in allowed) or via category rollup (city → place → match)."""
    if canonical_type is None:
        return True  # be permissive on unknown types
    if canonical_type in allowed:
        return True
    cat = _CATEGORY_OF_TYPE.get(canonical_type)
    if cat and cat in allowed:
        return True
    return False


def check_predicate_type_violations() -> Finding:
    """An assessment whose (subject, predicate, object) types violate
    the predicate's declared source_types / target_types from
    gdu_type_constraints. The classic case in any corpus:

      `traveled_to` declares target_types={place} but an extraction
      pipeline binds the object slot to a person — same dossier ends
      up with "X traveled_to Y" where Y is a person.

      `said_about` declares source_types={event} but extractors fill
      the subject slot with a person.

    Generic + works on any predicate vocabulary."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Pull all directional assessments + their constraint shape.
            # Apply the category-rollup test in Python so we can use the
            # same _CATEGORY_OF_TYPE map that future widening will share.
            cur.execute("""
                SELECT a.assessment_id, a.predicate_edge_type,
                       ses.canonical_name AS subj_name, ses.canonical_type AS subj_type,
                       oes.canonical_name AS obj_name,  oes.canonical_type AS obj_type,
                       c.source_types, c.target_types
                  FROM assessments a
                  JOIN gdu_type_constraints c ON c.edge_type = a.predicate_edge_type
                  LEFT JOIN entities ses ON ses.entity_id = a.subj_entity_id
                  LEFT JOIN entities oes ON oes.entity_id = a.obj_entity_id
                 WHERE a.demoted_at IS NULL
                   AND a.subj_entity_id IS NOT NULL
                   AND a.obj_entity_id IS NOT NULL
                   AND c.is_symmetric = FALSE
            """)
            rows = cur.fetchall()

    violations = []
    for r in rows:
        (aid, pred, subj_name, subj_type, obj_name, obj_type, src_types, tgt_types) = r
        subj_ok = _type_matches(subj_type, list(src_types or []))
        obj_ok  = _type_matches(obj_type,  list(tgt_types or []))
        if not (subj_ok and obj_ok):
            violations.append({
                "assessment_id":      aid,
                "predicate":          pred,
                "subj":               subj_name,
                "subj_type":          subj_type,
                "obj":                obj_name,
                "obj_type":           obj_type,
                "expected_subj_types": list(src_types or []),
                "expected_obj_types":  list(tgt_types or []),
                "subj_ok":            subj_ok,
                "obj_ok":             obj_ok,
            })

    return Finding(
        name="predicate_type_violations",
        severity="warn" if violations else "info",
        count=len(violations),
        sample=violations[:5],
        description="Assessments whose subj/obj entity types don't match the predicate's declared source/target types (after GDU category rollup)",
        fix_hint="Demote (likely extraction error) OR widen type_constraints if the binding is actually valid",
    )


def check_predicate_same_predicate_swap() -> Finding:
    """Both (X P Y) and (Y P X) asserted as facts for the SAME predicate
    that isn't declared symmetric. One direction is backwards.

    For example, if `employed` is non-symmetric and we have both
    (Acme employed Vance) and (Vance employed Acme), the
    catalog is contradicting itself — pick the one with lower confidence
    to demote (or queue for review when both are high-confidence).

    Generic: works for any non-symmetric predicate. Complements
    check_predicate_inverse_contradictions which detects the
    cross-predicate version (X employed Y + X worked_for Y)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM assessments a
                  JOIN gdu_type_constraints c
                    ON c.edge_type = a.predicate_edge_type
                   AND c.is_symmetric = FALSE
                  JOIN assessments b
                    ON b.predicate_edge_type = a.predicate_edge_type
                   AND b.subj_entity_id = a.obj_entity_id
                   AND b.obj_entity_id  = a.subj_entity_id
                   AND b.assessment_id != a.assessment_id
                 WHERE a.demoted_at IS NULL AND b.demoted_at IS NULL
                   AND a.subj_entity_id IS NOT NULL AND a.obj_entity_id IS NOT NULL
                   AND a.assessment_id < b.assessment_id  -- count each pair once
            """)
            n = int((cur.fetchone() or [0])[0])
            sample = _rows(cur, """
                SELECT a.assessment_id AS a1, a.confidence AS conf1,
                       b.assessment_id AS a2, b.confidence AS conf2,
                       a.predicate_edge_type AS pred,
                       e1.canonical_name AS x,
                       e2.canonical_name AS y
                  FROM assessments a
                  JOIN gdu_type_constraints c
                    ON c.edge_type = a.predicate_edge_type
                   AND c.is_symmetric = FALSE
                  JOIN assessments b
                    ON b.predicate_edge_type = a.predicate_edge_type
                   AND b.subj_entity_id = a.obj_entity_id
                   AND b.obj_entity_id  = a.subj_entity_id
                   AND b.assessment_id != a.assessment_id
                  LEFT JOIN entities e1 ON e1.entity_id = a.subj_entity_id
                  LEFT JOIN entities e2 ON e2.entity_id = a.obj_entity_id
                 WHERE a.demoted_at IS NULL AND b.demoted_at IS NULL
                   AND a.subj_entity_id IS NOT NULL AND a.obj_entity_id IS NOT NULL
                   AND a.assessment_id < b.assessment_id
                 ORDER BY a.assessment_id DESC
            """)
    return Finding(
        name="predicate_same_predicate_swap",
        severity="error" if n > 0 else "info",
        count=n,
        sample=sample,
        description="Same non-symmetric predicate asserted in both directions on the same entity pair — one is backwards",
        fix_hint="Demote the lower-confidence assertion; if both are high-confidence, queue for operator review",
    )


def check_predicate_signal_weight_asymmetry() -> Finding:
    """Directional predicate where the OBJECT has much higher
    signal_count than the SUBJECT — often a direction error.

    Heuristic for verbs of employment/ownership/control: the actor
    (employer, owner, controller) tends to have HIGHER signal_count
    than the patient (employee, owned thing, controlled thing). When
    we see 'Vance employed Acme' with Vance.signal_count=70 and
    Acme.signal_count=413, the imbalance is a signal of direction
    error.

    This is statistical — flags suspects, doesn't auto-demote. Operator
    confirms by checking the source text."""
    DIRECTIONAL_PREDICATES = (
        'employed', 'owns', 'controls', 'founded', 'officer_of',
        'appointed', 'paid', 'invested_in', 'acquired',
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT a.assessment_id, a.predicate_edge_type,
                       a.confidence,
                       ses.entity_id AS subj_id, ses.canonical_name AS subj,
                       COALESCE(ses.signal_count, 0) AS subj_sig,
                       oes.entity_id AS obj_id, oes.canonical_name AS obj,
                       COALESCE(oes.signal_count, 0) AS obj_sig
                  FROM assessments a
                  JOIN entities ses ON ses.entity_id = a.subj_entity_id
                  JOIN entities oes ON oes.entity_id = a.obj_entity_id
                 WHERE a.demoted_at IS NULL
                   AND a.predicate_edge_type = ANY(%s)
                   AND COALESCE(oes.signal_count, 0) >= 3 * COALESCE(ses.signal_count, 1)
                   AND COALESCE(oes.signal_count, 0) >= 20
                 ORDER BY (COALESCE(oes.signal_count,0)::float
                           / GREATEST(COALESCE(ses.signal_count,1), 1)::float) DESC
            """, (list(DIRECTIONAL_PREDICATES),))
            rows = cur.fetchall()

    suspects = [
        {
            "assessment_id":  r[0],
            "predicate":      r[1],
            "confidence":     float(r[2] or 0),
            "subj":           r[3] and {"id": r[3], "name": r[4], "sig": r[5]},
            "obj":            r[6] and {"id": r[6], "name": r[7], "sig": r[8]},
            "ratio_obj_over_subj":  round((r[8] or 0) / max(r[5] or 1, 1), 1),
        }
        for r in rows
    ]
    return Finding(
        name="predicate_signal_weight_asymmetry",
        severity="warn" if suspects else "info",
        count=len(suspects),
        sample=suspects[:8],
        description="Directional predicates (employed/owns/controls/...) where object has 3x+ more signals than subject — often a direction error",
        fix_hint="Operator confirms by checking source text. If backwards, demote and re-assert with swapped subj/obj",
    )


def check_predicate_inverse_contradictions() -> Finding:
    """Both (X P Y) and (X P-inverse Y) asserted as facts on the same
    entity pair — one direction is backwards. For example:

      (Acme employed Vance)   ← correct
      (Acme worked_for Vance) ← wrong (subj/obj swapped)

    Both assert a relationship between the same pair with predicates
    that gdu_type_constraints declares as inverses. The validator
    flags the contradiction; an operator (or a future auto-flip
    worker) demotes the lower-confidence side.

    Generic: works for owns/owned_by, parent_of/child_of, paid/
    received_from, accused/accused_by, founded/founded_by, employed/
    worked_for — anywhere a directional pair is declared."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM assessments a
                  JOIN gdu_type_constraints c
                    ON c.edge_type = a.predicate_edge_type
                   AND c.inverse_edge_type IS NOT NULL
                  JOIN assessments b
                    ON b.predicate_edge_type = c.inverse_edge_type
                   AND b.subj_entity_id = a.subj_entity_id
                   AND b.obj_entity_id  = a.obj_entity_id
                   AND b.assessment_id != a.assessment_id
                 WHERE a.demoted_at IS NULL AND b.demoted_at IS NULL
                   AND a.subj_entity_id IS NOT NULL AND a.obj_entity_id IS NOT NULL
            """)
            n = int((cur.fetchone() or [0])[0])
            sample = _rows(cur, """
                SELECT a.assessment_id AS a1, a.predicate_edge_type AS pred1,
                       a.confidence AS conf1,
                       b.assessment_id AS a2, b.predicate_edge_type AS pred2,
                       b.confidence AS conf2,
                       e1.canonical_name AS subj, e2.canonical_name AS obj
                  FROM assessments a
                  JOIN gdu_type_constraints c
                    ON c.edge_type = a.predicate_edge_type
                   AND c.inverse_edge_type IS NOT NULL
                  JOIN assessments b
                    ON b.predicate_edge_type = c.inverse_edge_type
                   AND b.subj_entity_id = a.subj_entity_id
                   AND b.obj_entity_id  = a.obj_entity_id
                   AND b.assessment_id != a.assessment_id
                  LEFT JOIN entities e1 ON e1.entity_id = a.subj_entity_id
                  LEFT JOIN entities e2 ON e2.entity_id = a.obj_entity_id
                 WHERE a.demoted_at IS NULL AND b.demoted_at IS NULL
                   AND a.subj_entity_id IS NOT NULL AND a.obj_entity_id IS NOT NULL
                 ORDER BY a.assessment_id DESC
            """)
    return Finding(
        name="predicate_inverse_contradictions",
        severity="error" if n > 0 else "info",
        count=n,
        sample=sample,
        description="Same entity pair asserted with both a predicate AND its declared inverse — one direction is backwards",
        fix_hint="Demote the lower-confidence side; or if both have high confidence, queue for operator review",
    )


def check_self_referential_facts() -> Finding:
    """An assessment with subj_entity_id == obj_entity_id (e.g. 'X
    participated in X') is an extraction artifact — the LLM split a
    single mention into both slots of a predicate. These should be
    demoted (set demoted_at) so they don't surface on dossier views."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            n = _scalar(cur, """
                SELECT COUNT(*) FROM assessments
                 WHERE subj_entity_id = obj_entity_id
                   AND subj_entity_id IS NOT NULL
                   AND demoted_at IS NULL
            """)
            sample = _rows(cur, """
                SELECT a.assessment_id, a.predicate_edge_type,
                       a.subj_entity_id, e.canonical_name AS entity_name
                  FROM assessments a
                  LEFT JOIN entities e ON e.entity_id = a.subj_entity_id
                 WHERE a.subj_entity_id = a.obj_entity_id
                   AND a.subj_entity_id IS NOT NULL
                   AND a.demoted_at IS NULL
                 ORDER BY a.assessment_id DESC
            """)
    return Finding(
        name="self_referential_facts",
        severity="error" if n > 0 else "info",
        count=n,
        sample=sample,
        description="Assessments where subj_entity_id == obj_entity_id — extraction artifact, never makes sense",
        fix_hint="Demote in bulk: UPDATE assessments SET demoted_at = NOW() WHERE subj_entity_id = obj_entity_id",
    )


def check_assessments_touching_needs_review_entities() -> Finding:
    """An assessment binding to an entity flagged [needs_review] is
    suspect — that entity got marked as a known-bad binding (compound
    name, wrong type, name collision). Should be demoted."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            n = _scalar(cur, """
                SELECT COUNT(*) FROM assessments a
                 WHERE a.demoted_at IS NULL
                   AND EXISTS (
                     SELECT 1 FROM entities e
                      WHERE (e.entity_id = a.subj_entity_id
                          OR e.entity_id = a.obj_entity_id)
                        AND e.canonical_name LIKE '%%[needs_review]%%'
                   )
            """)
            sample = _rows(cur, """
                SELECT a.assessment_id, a.predicate_edge_type,
                       e.canonical_name AS bad_entity_name
                  FROM assessments a
                  JOIN entities e
                    ON (e.entity_id = a.subj_entity_id
                     OR e.entity_id = a.obj_entity_id)
                 WHERE a.demoted_at IS NULL
                   AND e.canonical_name LIKE '%%[needs_review]%%'
                 ORDER BY a.assessment_id DESC
            """)
    return Finding(
        name="assessments_touching_needs_review_entities",
        severity="warn" if n > 0 else "info",
        count=n,
        sample=sample,
        description="Assessments bound to entities flagged [needs_review] (compound merges, wrong types)",
        fix_hint="Demote these and triage the underlying entities; consider entity split or rebinding to canonical",
    )


def check_docs_with_mentions_no_statements() -> Finding:
    """A doc with mentions but no statements means extraction stopped
    halfway — the LLM/parser surfaced entity spans but didn't synthesize
    any facts about them."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            n = _scalar(cur, """
                SELECT COUNT(*) FROM documents d
                 WHERE EXISTS (SELECT 1 FROM document_mentions dm WHERE dm.document_id = d.document_id)
                   AND NOT EXISTS (SELECT 1 FROM statements s WHERE s.document_id = d.document_id)
            """)
            sample = _rows(cur, """
                SELECT d.document_id, d.title, d.document_class,
                       (SELECT COUNT(*) FROM document_mentions WHERE document_id = d.document_id) AS mention_count
                  FROM documents d
                 WHERE EXISTS (SELECT 1 FROM document_mentions dm WHERE dm.document_id = d.document_id)
                   AND NOT EXISTS (SELECT 1 FROM statements s WHERE s.document_id = d.document_id)
                 ORDER BY mention_count DESC
            """)
    return Finding(
        name="docs_with_mentions_no_statements",
        severity="warn" if n > 0 else "info",
        count=n,
        sample=sample,
        description="Documents with extracted mentions but zero synthesized statements — extraction halted",
        fix_hint="Trigger re-extraction; check extractor logs for that doc_id",
    )


# ─── Collapse-correctness checks ─────────────────────────────────────────
# Added 2026-06-01 alongside the 15-commit collapse-pipeline cleanup.
# These verify the substrate invariants the gates enforce:
#   * entities.canonical_type stays in the entityvocab whitelist
#     (or the row is properly tombstoned via envelope.reclassification)
#   * entity_ldu_p doesn't accumulate forever — pending rows older
#     than a day mean either the worker died or the vocab can't catch them
#   * signal_extractions get llm_processed=true on a sane horizon
#   * knowledge_nodes with gdu_canonical=TRUE always carry a base60
#   * entity_redirects target rows actually exist


def _admittable_entity_types() -> tuple[str, ...]:
    """The admittable canonical_type whitelist — sourced from the entityvocab
    pack via subtype_validator (the single source of truth), NOT hardcoded.
    Mirrors exactly what the entities_canonical_type_check constraint enforces,
    so this integrity check and the storage constraint can never drift."""
    from core.ldu.subtype_validator import valid_entity_subtypes
    return tuple(valid_entity_subtypes())


def check_entities_with_ungated_type() -> Finding:
    """entities with canonical_type outside the entityvocab whitelist AND
    no reclassification tombstone. Should be 0 — every grandfathered row
    is supposed to be marked.

    Non-zero means either (a) a new write site is bypassing the
    subtype_validator gates, or (b) the grandfathered cleanup left gaps.
    """
    admittable = _admittable_entity_types()
    with get_connection() as conn:
        with conn.cursor() as cur:
            count = _scalar(cur, """
                SELECT COUNT(*) FROM entities
                 WHERE canonical_type IS NOT NULL
                   AND canonical_type NOT IN %s
                   AND NOT (COALESCE(envelope, '{}'::jsonb) ? 'reclassification')
            """, (admittable,))
            sample = _rows(cur, """
                SELECT entity_id, canonical_type, canonical_name, created_at
                  FROM entities
                 WHERE canonical_type IS NOT NULL
                   AND canonical_type NOT IN %s
                   AND NOT (COALESCE(envelope, '{}'::jsonb) ? 'reclassification')
                 ORDER BY created_at DESC
            """, (admittable,))
    return Finding(
        name="entities_with_ungated_type",
        severity="error" if count > 0 else "info",
        count=count,
        sample=sample,
        description=(
            "entities.canonical_type not in entityvocab whitelist AND not "
            "tombstoned via envelope.reclassification"
        ),
        fix_hint=(
            "Either a write path bypasses the subtype_validator gate "
            "(check what populated these rows), or run "
            "migrate_grandfathered_entities.py to mark them"
        ),
    )


def check_documents_content_sha256_coverage() -> Finding:
    """Documents with a NULL content_sha256 are invisible to the federation
    extraction-sharing path — each node re-runs its own (non-deterministic) LLM
    pass on them instead of sharing one extraction, which diverges graphs. This
    surfaces the gap loudly so coverage is never silently assumed."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            missing = _scalar(cur, """
                SELECT COUNT(*) FROM documents
                 WHERE content_sha256 IS NULL OR content_sha256 = ''
            """)
            total = _scalar(cur, "SELECT COUNT(*) FROM documents")
            sample = _rows(cur, """
                SELECT document_id, hash, title FROM documents
                 WHERE content_sha256 IS NULL OR content_sha256 = ''
                 ORDER BY document_id DESC
            """)
    return Finding(
        name="documents_content_sha256_coverage",
        severity="warn" if missing > 0 else "info",
        count=missing,
        sample=[{"missing": missing, "total": total}] + sample,
        description="documents missing content_sha256 (unshareable → per-node re-extraction → divergence)",
        fix_hint="run scripts/backfill_content_sha256.py; new ingests set it automatically",
    )


def check_type_overrides_in_pack() -> Finding:
    """Local entityvocab_overrides are a node-local divergence source — a peer
    without the same overrides classifies types differently, so their collapsed
    graphs won't match. They should be promoted into the versioned (federated)
    pack. Flag any that exist so the divergence is visible, not silent."""
    from core.ldu.subtype_validator import override_count, classification_fingerprint
    n = override_count()
    return Finding(
        name="type_overrides_in_pack",
        severity="warn" if n > 0 else "info",
        count=n,
        sample=[{"local_overrides": n,
                 "classification_fingerprint": classification_fingerprint()[:16]}],
        description="local entityvocab_overrides not in the versioned pack (federation divergence risk)",
        fix_hint=("Promote operator type overrides into the entityvocab pack and "
                  "bump its version so peers stay in classification lockstep"),
    )


def check_ckey_python_sql_agree() -> Finding:
    """The cross-node identity key must be computed identically by Python
    (core.gdu.identity.canonical_entity_key) and by the SQL trigger
    function (compute_canonical_entity_key, migration 124). If they drift,
    a node stamps a column the publisher/G8 won't reproduce → entities silently
    fail to federate. Probe both over edge-case inputs and assert equality.
    """
    import json as _json
    from core.gdu.identity import canonical_entity_key
    probes = [
        ("United States of America", "country", {}),
        ("  Lockheed   Martin  ", "ORGANIZATION", {}),
        ("Dana J. Vale", "person", {}),
        ("", "person", {}),
        ("Kuwait", "", {}),
        ("Côte d'Ivoire", "country", {}),
        ("Acme", "organization", {"cik": "111"}),
        ("Acme", "organization", {"cik": "222"}),       # must differ from 111
        ("Boeing", "organization", {"qid": "Q66", "cik": "12927"}),  # qid wins
        ("X", "person", {"cik": "", "qid": "Q1"}),      # empty cik skipped
    ]
    mismatches: list[dict] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            for name, etype, idents in probes:
                py = canonical_entity_key(name, etype, idents)
                try:
                    cur.execute(
                        "SELECT compute_canonical_entity_key(%s, %s, %s::jsonb)",
                        (name, etype, _json.dumps(idents)))
                    sql = cur.fetchone()[0]
                except Exception as e:
                    return Finding(
                        name="ckey_python_sql_agree", severity="warn", count=-1,
                        sample=[{"error": f"SQL fn missing/failed: {e!s}"}],
                        description="compute_canonical_entity_key() not callable (migration 124 not applied?)",
                        fix_hint="Apply migration 124_canonical_entity_key_trigger.sql",
                    )
                if py != sql:
                    mismatches.append({"name": name, "type": etype,
                                       "python": py[:16], "sql": sql[:16]})
    return Finding(
        name="ckey_python_sql_agree",
        severity="critical" if mismatches else "info",
        count=len(mismatches),
        sample=mismatches,
        description="Python and SQL canonical_entity_key must be byte-identical",
        fix_hint=(
            "core/federation/identity.py and migration 124's "
            "compute_canonical_entity_key() have drifted — reconcile the formula"
        ),
    )


def check_constraint_matches_pack() -> Finding:
    """The entities_canonical_type_check storage constraint MUST allow exactly
    the canonical types the entityvocab pack declares (valid_entity_subtypes()).

    This is the drift guard added with migration 123. The original bug (116 vs
    118) was a hand-written constraint silently diverging from the pack — a
    place type the pack accepts but the constraint rejects crash-loops the
    validators. This check makes that divergence a loud, visible finding
    instead of a mystery restart loop.

    Parses the live CHECK definition out of pg_constraint and diffs the
    IN (...) literal list against the pack.
    """
    import re
    from core.ldu.subtype_validator import valid_entity_subtypes

    pack_types = set(valid_entity_subtypes())
    missing: list[str] = []      # pack accepts, constraint rejects → crash risk
    extra: list[str] = []        # constraint allows, pack no longer does → drift
    constraint_def = ""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT pg_get_constraintdef(oid) FROM pg_constraint
                 WHERE conname = 'entities_canonical_type_check'
            """)
            r = cur.fetchone()
            constraint_def = r[0] if r else ""

    if constraint_def:
        # Pull the single-quoted tokens out of the IN (...) list.
        constraint_types = set(re.findall(r"'([a-z_]+)'::", constraint_def)) \
            or set(re.findall(r"'([a-z_]+)'", constraint_def))
        # Drop the envelope marker keys that also appear quoted.
        constraint_types -= {"reclassification", "normalized_from"}
        missing = sorted(pack_types - constraint_types)
        extra = sorted(constraint_types - pack_types)

    count = len(missing) + len(extra)
    return Finding(
        name="constraint_matches_pack",
        severity="critical" if missing else ("warn" if extra else "info"),
        count=count,
        sample=[{
            "missing_from_constraint": missing,   # pack accepts but DB rejects
            "extra_in_constraint": extra,         # DB allows but pack dropped
            "constraint_found": bool(constraint_def),
        }],
        description=(
            "entities_canonical_type_check must match entityvocab "
            "valid_entity_subtypes() exactly (migration 123 invariant)"
        ),
        fix_hint=(
            "Regenerate the constraint from the pack: write a new migration "
            "mirroring valid_entity_subtypes(). 'missing' types crash-loop "
            "validators — fix immediately. 'extra' types are stale pack drift."
        ),
    )


def check_stale_pending_ldup() -> Finding:
    """entity_ldu_p rows older than 24h still status='pending'. Should
    drain at ~200/tick × 5min ticks = 2,400/hour. Persistent pending
    means the worker stopped or all rows are stuck in ldu_unknown."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            count = _scalar(cur, """
                SELECT COUNT(*) FROM entity_ldu_p
                 WHERE status = 'pending'
                   AND created_at < NOW() - INTERVAL '24 hours'
            """)
            sample = _rows(cur, """
                SELECT ldu_p_id, raw_name, suspected_type, rejection_reason, created_at
                  FROM entity_ldu_p
                 WHERE status = 'pending'
                   AND created_at < NOW() - INTERVAL '24 hours'
                 ORDER BY created_at ASC
            """)
    sev = "warn" if count > 1000 else ("info" if count > 0 else "info")
    return Finding(
        name="stale_pending_ldup",
        severity=sev,
        count=count,
        sample=sample,
        description="entity_ldu_p rows pending >24h — worker stuck or all ldu_unknown",
        fix_hint=(
            "Check ldup_promoter worker is running (it polls every 5m). "
            "If running, inspect rejection_reason distribution — large "
            "ldu_unknown buckets mean vocab needs extending"
        ),
    )


def check_stuck_signal_extractions() -> Finding:
    """signal_extractions older than 24h with llm_processed=false. Should
    be near zero — LLM extraction runs on capture. Stuck rows mean the
    LLM provider was down (Ollama unreachable, etc.)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            count = _scalar(cur, """
                SELECT COUNT(*) FROM signal_extractions
                 WHERE NOT llm_processed
                   AND ingested_at < NOW() - INTERVAL '24 hours'
            """)
            sample = _rows(cur, """
                SELECT extraction_id, signal_rid, signal_type, ingested_at
                  FROM signal_extractions
                 WHERE NOT llm_processed
                   AND ingested_at < NOW() - INTERVAL '24 hours'
                 ORDER BY ingested_at ASC
            """)
    sev = "error" if count > 5000 else ("warn" if count > 100 else "info")
    return Finding(
        name="stuck_signal_extractions",
        severity=sev,
        count=count,
        sample=sample,
        description="signal_extractions unprocessed >24h — LLM gap",
        fix_hint=(
            "Check OLLAMA_BASE_URL resolves from api container. Run "
            "scripts/backfill_stuck_extractions.py to recover the backlog"
        ),
    )


def check_ungoverned_canonical_nodes() -> Finding:
    """knowledge_nodes with gdu_canonical=TRUE but no base60_address.
    The enforce_governance() trigger should block these at INSERT time —
    if any exist, either a row pre-dated the trigger or an UPDATE
    bypassed it. Should be 0."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            count = _scalar(cur, """
                SELECT COUNT(*) FROM knowledge_nodes
                 WHERE gdu_canonical = TRUE AND base60_address IS NULL
            """)
            sample = _rows(cur, """
                SELECT node_id, node_type, subtype, title, created_at
                  FROM knowledge_nodes
                 WHERE gdu_canonical = TRUE AND base60_address IS NULL
                 ORDER BY created_at DESC
            """)
    return Finding(
        name="ungoverned_canonical_nodes",
        severity="error" if count > 0 else "info",
        count=count,
        sample=sample,
        description="knowledge_nodes claim gdu_canonical=TRUE but lack base60_address",
        fix_hint=(
            "Either set gdu_canonical=FALSE (materialized-view pattern) "
            "or route through gateway.admit_node to get an address"
        ),
    )


def check_orphan_entity_redirects() -> Finding:
    """entity_redirects pointing at new_entity_id values that don't
    exist in entities. Should be 0 — the FK has ON DELETE CASCADE, but
    a check confirms no stale paths."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            count = _scalar(cur, """
                SELECT COUNT(*) FROM entity_redirects r
                 WHERE NOT EXISTS (
                   SELECT 1 FROM entities e WHERE e.entity_id = r.new_entity_id
                 )
            """)
            sample = _rows(cur, """
                SELECT r.old_entity_id, r.new_entity_id, r.merged_at, r.merge_reason
                  FROM entity_redirects r
                 WHERE NOT EXISTS (
                   SELECT 1 FROM entities e WHERE e.entity_id = r.new_entity_id
                 )
                 ORDER BY r.merged_at DESC
            """)
    return Finding(
        name="orphan_entity_redirects",
        severity="warn" if count > 0 else "info",
        count=count,
        sample=sample,
        description="entity_redirects.new_entity_id doesn't exist (FK + CASCADE should prevent this)",
        fix_hint="DELETE FROM entity_redirects WHERE new_entity_id NOT IN (SELECT entity_id FROM entities)",
    )


# ─── Runner ────────────────────────────────────────────────────────────


_ALL_CHECKS: list[Callable[[], Finding]] = [
    check_statements_without_doc,
    check_statements_without_span,
    check_promoted_without_speaker_entity,
    check_temporal_impossibilities,
    check_temporal_impossibilities_all,
    check_mentions_unresolved_by_type,
    check_duplicate_canonical_names,
    check_entities_zero_signal,
    check_edges_self_loops,
    check_edges_dangling_endpoints,
    check_self_referential_facts,
    check_assessments_touching_needs_review_entities,
    check_predicate_type_violations,
    check_predicate_inverse_contradictions,
    check_predicate_same_predicate_swap,
    check_predicate_signal_weight_asymmetry,
    check_docs_with_mentions_no_statements,
    # Collapse-correctness checks (2026-06-01)
    check_entities_with_ungated_type,
    check_constraint_matches_pack,
    check_ckey_python_sql_agree,
    check_type_overrides_in_pack,
    check_documents_content_sha256_coverage,
    check_stale_pending_ldup,
    check_stuck_signal_extractions,
    check_ungoverned_canonical_nodes,
    check_orphan_entity_redirects,
]


def run_all() -> dict:
    """Run every check and return a structured report."""
    findings: list[Finding] = []
    for check in _ALL_CHECKS:
        try:
            findings.append(_run(check))
        except Exception as e:
            findings.append(Finding(
                name=check.__name__,
                severity="error",
                count=-1,
                sample=[{"error": str(e)}],
                description=f"Check raised: {e!s}",
                fix_hint="Investigate the check itself; may be a schema mismatch",
            ))
    from collections import Counter
    by_severity = Counter(f.severity for f in findings)
    issues = sum(1 for f in findings if f.severity in ("warn", "error", "critical") and f.count > 0)
    return {
        "findings":         [asdict(f) for f in findings],
        "summary": {
            "total_checks":      len(findings),
            "by_severity":       dict(by_severity),
            "checks_with_issues": issues,
            "clean_checks":       len(findings) - issues,
        },
    }
