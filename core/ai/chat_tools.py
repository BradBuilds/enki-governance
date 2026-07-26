"""
Chat tools — the small set of named operations the chat LLM can call
when answering questions about the cyclical model.

The LLM doesn't get raw SQL or unrestricted code execution. It gets
this menu of typed, parameterized operations. Each tool:
  - takes a JSON dict of args
  - returns a JSON dict + a list of typed cards the UI can render

Tools live in this module so they're testable without the API layer.
The /api/chat/ask endpoint just dispatches to the right tool.

Card type names match ui/src/lib/components/cards2/ exports.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.ai.chat_tools")


# ─── Tool descriptors (the menu shown to the LLM) ────────────────────

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "entity_lookup",
        "description": "Find an entity by name (fuzzy) or base-60 address. Returns top match + entity card.",
        "args": {"query": "string — name fragment or base-60 address like 0.0.53725"},
    },
    {
        "name": "library_search",
        "description": "Find libraries matching a topic / focal term. Returns matching LibraryCards.",
        "args": {"query": "string — topic name fragment, or 'all'"},
    },
    {
        "name": "fact_search",
        "description": "Find assessments (facts/observations/claims) matching subject/predicate/object filters. Any arg optional.",
        "args": {
            "subject":   "string — fuzzy subject entity name (optional)",
            "predicate": "string — exact edge_type like 'communicated_with' (optional)",
            "object":    "string — fuzzy object entity name (optional)",
            "tier":      "string — fact|observation|claim (optional)",
        },
    },
    {
        "name": "event_search",
        "description": "Find events by participant entity name, time range, or location.",
        "args": {
            "participant": "string — entity name (optional)",
            "since":       "string — ISO date (optional)",
            "until":       "string — ISO date (optional)",
            "location":    "string — place name (optional)",
        },
    },
    {
        "name": "reference_search",
        "description": "Find bibliographic references by title, author, or ISBN/DOI.",
        "args": {"query": "string — title fragment, author, ISBN, or DOI"},
    },
    {
        "name": "address_resolve",
        "description": "Look up whatever lives at a specific base-60 address (entity, library, assessment, event, reference).",
        "args": {"address": "string — base-60 address like 13.3.1 or 7.0.971"},
    },
    {
        "name": "pipeline_status",
        "description": "Get the live pipeline depth (references catalogued, documents in flight, queue lengths). Use when user asks about system state or progress.",
        "args": {},
    },
    {
        "name": "recent_facts",
        "description": "List the most recently produced canonical facts/assessments. Use for 'what's new?' style questions.",
        "args": {"limit": "integer — how many (default 10)"},
    },
]


# ─── Result builder ───────────────────────────────────────────────────

def _rows_to_dicts(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    out = []
    for row in cur.fetchall():
        d = {}
        for col, val in zip(cols, row):
            if hasattr(val, "isoformat"):
                d[col] = val.isoformat()
            elif hasattr(val, "__float__") and not isinstance(val, (int, bool)):
                try:
                    d[col] = float(val)
                except Exception:
                    d[col] = val
            else:
                d[col] = val
        out.append(d)
    return out


def _card(card_type: str, data: dict) -> dict:
    return {"type": card_type, "data": data}


# ─── Tool implementations ────────────────────────────────────────────

def entity_lookup(args: dict) -> dict:
    query = (args.get("query") or "").strip()
    if not query:
        return {"text": "Please provide a name or address to look up.", "cards": []}
    # Try address first
    addr_match = re.match(r"^\d+\.\d+\.\d+$", query)
    if addr_match:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM entities WHERE base60_address = %s LIMIT 1", (query,))
                rows = _rows_to_dicts(cur)
        if rows:
            e = rows[0]
            return {
                "text": f"Address {query} → {e['canonical_name']} ({e['canonical_type']}, {e['entity_kind']}).",
                "cards": [_card("EntityCard", {"entity_id": e["entity_id"], "entity": e})],
            }
        return {"text": f"No entity at address {query}.", "cards": []}
    # Fuzzy name search
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT entity_id, canonical_name, canonical_type, entity_kind,
                       base60_address, identifiers, signal_count, aliases,
                       concept_entity_id
                  FROM entities
                 WHERE LOWER(canonical_name) LIKE LOWER(%s)
                    OR LOWER(normalized_name) LIKE LOWER(%s)
                 ORDER BY signal_count DESC NULLS LAST
                 LIMIT 5
                """,
                (f"%{query}%", f"%{query}%"),
            )
            rows = _rows_to_dicts(cur)
    if not rows:
        return {"text": f'No entities matching "{query}".', "cards": []}
    text = (f"Found {len(rows)} entit{'y' if len(rows) == 1 else 'ies'} matching "
            f'"{query}". Top match: {rows[0]["canonical_name"]} '
            f"({rows[0]['canonical_type']}, addr {rows[0]['base60_address']}).")
    cards = [_card("EntityCard", {"entity_id": e["entity_id"], "entity": e}) for e in rows[:3]]
    return {"text": text, "cards": cards}


def library_search(args: dict) -> dict:
    query = (args.get("query") or "all").strip()
    with get_connection() as conn:
        with conn.cursor() as cur:
            if query.lower() == "all":
                cur.execute(
                    """
                    SELECT library_id, base60_address, library_subtype,
                           canonical_name, focal_term,
                           reference_count, acquired_count, pending_count,
                           entity_count, fact_count, document_count,
                           last_refreshed_at, discovery_status
                      FROM libraries ORDER BY last_updated_at DESC LIMIT 10
                    """
                )
            else:
                cur.execute(
                    """
                    SELECT library_id, base60_address, library_subtype,
                           canonical_name, focal_term,
                           reference_count, acquired_count, pending_count,
                           entity_count, fact_count, document_count,
                           last_refreshed_at, discovery_status
                      FROM libraries
                     WHERE LOWER(focal_term) LIKE LOWER(%s)
                        OR LOWER(canonical_name) LIKE LOWER(%s)
                     ORDER BY last_updated_at DESC LIMIT 10
                    """,
                    (f"%{query}%", f"%{query}%"),
                )
            rows = _rows_to_dicts(cur)
    if not rows:
        return {"text": f'No libraries matching "{query}".', "cards": []}
    text = f"{len(rows)} librar{'y' if len(rows) == 1 else 'ies'}"
    if query.lower() != "all":
        text += f' matching "{query}"'
    text += ". " + ", ".join(r['focal_term'] for r in rows[:5]) + "."
    cards = [_card("LibraryCard", {"library_id": r["library_id"], "library": r}) for r in rows[:5]]
    return {"text": text, "cards": cards}


def fact_search(args: dict) -> dict:
    subj = (args.get("subject") or "").strip()
    pred = (args.get("predicate") or "").strip()
    obj  = (args.get("object") or "").strip()
    tier = (args.get("tier") or "").strip()

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
         WHERE 1=1
    """
    params: list[Any] = []
    if subj:
        sql += " AND LOWER(ses.canonical_name) LIKE LOWER(%s)"
        params.append(f"%{subj}%")
    if obj:
        sql += " AND (LOWER(oes.canonical_name) LIKE LOWER(%s) OR LOWER(a.obj_value) LIKE LOWER(%s))"
        params.extend([f"%{obj}%", f"%{obj}%"])
    if pred:
        sql += " AND a.predicate_edge_type = %s"
        params.append(pred)
    if tier:
        sql += " AND a.assessment_tier = %s"
        params.append(tier)
    sql += " ORDER BY a.corroborated_by_count DESC NULLS LAST, a.assessment_id LIMIT 10"

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = _rows_to_dicts(cur)
    if not rows:
        filters = ", ".join(f"{k}={v}" for k, v in {"subject":subj,"predicate":pred,"object":obj,"tier":tier}.items() if v)
        return {"text": f"No assessments matching ({filters or 'any'}).", "cards": []}
    n_fact = sum(1 for r in rows if r["tier"] == "fact")
    text = f"Found {len(rows)} assessment{'' if len(rows)==1 else 's'}"
    if n_fact:
        text += f" ({n_fact} fact tier)"
    text += "."
    cards = [_card("FactCard", {"assessment_id": r["assessment_id"], "assessment": r}) for r in rows[:5]]
    return {"text": text, "cards": cards}


def event_search(args: dict) -> dict:
    participant = (args.get("participant") or "").strip()
    since = args.get("since")
    until = args.get("until")
    location = (args.get("location") or "").strip()

    sql = """
        SELECT event_id, event_type, title, event_time, event_end_time,
               temporal_precision, latitude, longitude, location_entity_id,
               actor_entity_ids, target_entity_ids, confidence, base60_address
          FROM events
         WHERE base60_address IS NOT NULL
    """
    params: list[Any] = []
    if participant:
        # Resolve participant to entity_id, then filter
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT entity_id FROM entities "
                    " WHERE LOWER(canonical_name) LIKE LOWER(%s) "
                    " ORDER BY signal_count DESC NULLS LAST LIMIT 5",
                    (f"%{participant}%",),
                )
                eids = [r[0] for r in cur.fetchall()]
        if eids:
            sql += " AND (actor_entity_ids && %s::bigint[] OR target_entity_ids && %s::bigint[])"
            params.extend([eids, eids])
    if since:
        sql += " AND event_time >= %s"
        params.append(since)
    if until:
        sql += " AND event_time <= %s"
        params.append(until)
    sql += " ORDER BY event_time NULLS LAST LIMIT 10"

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = _rows_to_dicts(cur)
    if not rows:
        return {"text": "No events matching those filters.", "cards": []}
    text = f"Found {len(rows)} event{'' if len(rows)==1 else 's'}."
    cards = [_card("EventCard", {"event_id": r["event_id"], "event": r}) for r in rows[:5]]
    return {"text": text, "cards": cards}


def reference_search(args: dict) -> dict:
    query = (args.get("query") or "").strip()
    if not query:
        return {"text": "Please provide a title, author, or identifier.", "cards": []}
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT reference_id, base60_address, library_id, citation_text,
                       title, authors, publication_year, publisher,
                       reference_type, identifiers, discovered_via,
                       acquisition_status, purchase_links
                  FROM bibliographic_references
                 WHERE LOWER(title) LIKE LOWER(%s)
                    OR LOWER(citation_text) LIKE LOWER(%s)
                    OR identifiers->>'isbn' = %s
                    OR identifiers->>'doi' = %s
                 ORDER BY acquisition_status, reference_id LIMIT 10
                """,
                (f"%{query}%", f"%{query}%", query, query),
            )
            rows = _rows_to_dicts(cur)
    if not rows:
        return {"text": f'No references matching "{query}".', "cards": []}
    text = f"Found {len(rows)} reference{'' if len(rows)==1 else 's'} matching \"{query}\"."
    cards = [_card("RefCard", {"reference_id": r["reference_id"], "reference": r}) for r in rows[:5]]
    return {"text": text, "cards": cards}


def address_resolve(args: dict) -> dict:
    addr = (args.get("address") or "").strip()
    if not re.match(r"^\d+\.\d+\.\d+$", addr):
        return {"text": f'"{addr}" is not a valid base-60 address (expected N.N.N).', "cards": []}
    # The first digit identifies the category
    cat_digit = int(addr.split(".")[0])
    cat_to_table = {
        0: ("entities", "EntityCard", "entity_id"),
        1: ("entities", "EntityCard", "entity_id"),
        2: ("events", "EventCard", "event_id"),
        5: ("documents", "DocCard", "document_id"),  # documents OR bibliographic_references
        7: ("assessments", "FactCard", "assessment_id"),
        13: ("libraries", "LibraryCard", "library_id"),
    }
    # Try entities first (covers 0 + 1 places)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT entity_id, canonical_name FROM entities WHERE base60_address = %s LIMIT 1", (addr,))
            r = cur.fetchone()
            if r:
                return entity_lookup({"query": addr})
            cur.execute("SELECT event_id FROM events WHERE base60_address = %s LIMIT 1", (addr,))
            r = cur.fetchone()
            if r:
                return {"text": f"Address {addr} resolves to event {r[0]}.",
                        "cards": [_card("EventCard", {"event_id": r[0]})]}
            cur.execute("SELECT assessment_id, subj_entity_id FROM assessments WHERE base60_address = %s LIMIT 1", (addr,))
            r = cur.fetchone()
            if r:
                return {"text": f"Address {addr} resolves to assessment {r[0]}.",
                        "cards": [_card("FactCard", {"assessment_id": r[0]})]}
            cur.execute("SELECT library_id, canonical_name FROM libraries WHERE base60_address = %s LIMIT 1", (addr,))
            r = cur.fetchone()
            if r:
                return {"text": f"Address {addr} resolves to library {r[1]}.",
                        "cards": [_card("LibraryCard", {"library_id": r[0]})]}
            cur.execute("SELECT reference_id FROM bibliographic_references WHERE base60_address = %s LIMIT 1", (addr,))
            r = cur.fetchone()
            if r:
                return {"text": f"Address {addr} resolves to reference {r[0]}.",
                        "cards": [_card("RefCard", {"reference_id": r[0]})]}
    return {"text": f"Nothing currently lives at address {addr}.", "cards": []}


def pipeline_status(args: dict) -> dict:
    from services.api.src.routes.cards_api import system_depth
    depth = system_depth()
    text = (
        f"{depth['knowledge']['facts']} fact{'' if depth['knowledge']['facts']==1 else 's'} · "
        f"{depth['knowledge']['observations']} observations · "
        f"{depth['knowledge']['events']} events. "
        f"{depth['live_queue']['collapse_eligible']} docs awaiting collapse, "
        f"{depth['live_queue']['promotion_eligible']} statements awaiting promotion, "
        f"{depth['live_queue']['acquisition_eligible']} refs due for retry."
    )
    return {"text": text, "cards": [_card("PipelineCard", {})]}


def recent_facts(args: dict) -> dict:
    limit = int(args.get("limit") or 10)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.assessment_id, a.base60_address, a.assessment_tier AS tier,
                       a.subj_entity_id, ses.canonical_name AS subj_name,
                       a.predicate_edge_type,
                       a.obj_entity_id, oes.canonical_name AS obj_name,
                       a.obj_value, a.polarity,
                       a.corroborated_by_count, a.contradicted_by_count,
                       a.confidence, a.last_seen
                  FROM assessments a
                  LEFT JOIN entities ses ON ses.entity_id = a.subj_entity_id
                  LEFT JOIN entities oes ON oes.entity_id = a.obj_entity_id
                 ORDER BY a.last_seen DESC LIMIT %s
                """,
                (limit,),
            )
            rows = _rows_to_dicts(cur)
    if not rows:
        return {"text": "No assessments yet.", "cards": []}
    text = f"{len(rows)} most recent assessments."
    cards = [_card("FactCard", {"assessment_id": r["assessment_id"], "assessment": r}) for r in rows[:5]]
    return {"text": text, "cards": cards}


def _resolve_library(term):
    """Map a loose name ('mkultra', 'example_program', 106) to a library_id."""
    if term in (None, ""):
        return None, None
    s = str(term).strip()
    with get_connection() as conn:
        with conn.cursor() as cur:
            if s.isdigit():
                cur.execute("SELECT library_id, canonical_name FROM libraries WHERE library_id=%s", (int(s),))
            else:
                cur.execute(
                    """SELECT library_id, canonical_name FROM libraries
                        WHERE canonical_name ILIKE %s OR focal_term ILIKE %s
                        ORDER BY (SELECT count(*) FROM library_members lm
                                   WHERE lm.library_id = libraries.library_id
                                     AND lm.member_kind='document') DESC
                        LIMIT 1""", (f"%{s}%", f"%{s}%"))
            row = cur.fetchone()
    return (int(row[0]), row[1]) if row else (None, None)


def library_progress(args: dict) -> dict:
    """How is the reading going — per library, with growth since yesterday.

    Every count filters member_kind='document'. library_members is keyed
    (library_id, member_kind, member_id), so omitting it counts entity- and
    assessment-member rows as if they were documents.
    """
    lib_id, lib_name = _resolve_library(args.get("library"))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH d AS (
                  SELECT library_id, member_id AS document_id
                    FROM library_members
                   WHERE member_kind = 'document'
                     AND (%(lib)s IS NULL OR library_id = %(lib)s)
                )
                SELECT l.library_id, l.canonical_name,
                       count(DISTINCT d.document_id) docs,
                       (SELECT count(*) FROM document_mentions dm
                          JOIN d d2 ON d2.document_id = dm.document_id
                         WHERE d2.library_id = l.library_id) mentions,
                       (SELECT count(*) FROM statements s
                          JOIN d d2 ON d2.document_id = s.document_id
                         WHERE d2.library_id = l.library_id) statements,
                       (SELECT count(*) FROM bibliographic_references r
                         WHERE r.library_id = l.library_id
                           AND r.acquisition_status = 'pending') pending,
                       (SELECT count(*) FROM documents doc
                          JOIN d d2 ON d2.document_id = doc.document_id
                         WHERE d2.library_id = l.library_id
                           AND doc.created_at > now() - interval '24 hours') docs_24h
                  FROM libraries l JOIN d ON d.library_id = l.library_id
                 GROUP BY l.library_id, l.canonical_name
                 HAVING count(DISTINCT d.document_id) > 0
                 ORDER BY docs DESC LIMIT 12
                """, {"lib": lib_id})
            rows = cur.fetchall()
    if not rows:
        return {"text": f"No documents yet in {lib_name or 'any library'}.", "cards": []}
    lines = []
    for lid, name, docs, mentions, statements, pending, docs_24h in rows:
        short = (name or "").split("/")[-1]
        new = f", +{docs_24h} today" if docs_24h else ""
        lines.append(f"**{short}** (lib {lid}) — {docs} docs{new} · {mentions:,} mentions · "
                     f"{statements:,} statements · {pending} refs still to acquire")
    return {"text": "\n".join(lines), "cards": [_card("PipelineCard", {})]}


def corpus_answer(args: dict) -> dict:
    """Grounded answer about a subject, optionally scoped to one library.

    Reads what was actually collapsed and cites it. Never free recall — if the
    corpus has nothing, it says so rather than filling the gap.
    """
    from core.ai.corpus_answer import answer_about
    subject = (args.get("subject") or args.get("query") or "").strip()
    question = (args.get("question") or subject).strip()
    lib_id, lib_name = _resolve_library(args.get("library"))
    if not subject:
        return {"text": "Tell me who or what to look into.", "cards": []}
    res = answer_about(question, subject, library_id=lib_id)
    text = res.get("answer") or "Nothing found."
    if lib_name and res.get("grounded"):
        text = f"_Scoped to {lib_name.split('/')[-1]}._\n\n{text}"
    cards = []
    if res.get("entity_id"):
        cards.append(_card("EntityCard", {"entity_id": res["entity_id"]}))
    return {"text": text, "cards": cards}


def topic_timeline(args: dict) -> dict:
    """Dated events for a library or a subject, in chronological order.

    Reads statements that carry an event_time, so every row is a claim that was
    collapsed out of a document and can be traced back to it.
    """
    lib_id, lib_name = _resolve_library(args.get("library"))
    subject = (args.get("subject") or "").strip()
    limit = int(args.get("limit") or 20)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH d AS (
                  SELECT library_id, member_id AS document_id
                    FROM library_members
                   WHERE member_kind = 'document'
                     AND (%(lib)s IS NULL OR library_id = %(lib)s)
                )
                SELECT s.event_time::date, se.canonical_name, s.predicate,
                       COALESCE(oe.canonical_name, ''), left(s.raw_text, 190), s.document_id
                  FROM statements s
                  JOIN d ON d.document_id = s.document_id
             LEFT JOIN entities se ON se.entity_id = s.speaker_entity_id
             LEFT JOIN entities oe ON oe.entity_id = s.object_entity_id
                 WHERE s.event_time IS NOT NULL
                   AND (%(subj)s = '' OR se.canonical_name ILIKE %(pat)s
                                      OR oe.canonical_name ILIKE %(pat)s
                                      OR s.raw_text ILIKE %(pat)s)
                 ORDER BY s.event_time ASC
                 LIMIT %(lim)s
                """,
                {"lib": lib_id, "subj": subject, "pat": f"%{subject}%", "lim": limit},
            )
            rows = cur.fetchall()
    if not rows:
        where = f" in {lib_name.split('/')[-1]}" if lib_name else ""
        who = f" for “{subject}”" if subject else ""
        return {"text": f"No dated events{who}{where} yet. Events appear here once "
                        f"statements carrying a date have been collapsed.", "cards": []}
    head = f"**Timeline{(' — ' + lib_name.split('/')[-1]) if lib_name else ''}**"
    if subject:
        head += f" · {subject}"
    lines = [head, ""]
    for dt, subj, pred, obj, raw, doc in rows:
        who = subj or "—"
        what = " ".join(x for x in (pred, obj) if x).strip()
        lines.append(f"**{dt}** — {who} {what}".rstrip())
        if raw:
            lines.append(f"    “{raw.strip()}” (doc {doc})")
    return {"text": "\n".join(lines), "cards": [_card("TimelineCard", {"library_id": lib_id})]}


def entity_connections(args: dict) -> dict:
    """Who/what a subject is connected to, and on what evidence.

    Prefers governed assessment edges. Falls back to paragraph co-occurrence,
    which is weaker evidence — appearing in the same paragraph is not a claim —
    so it is labelled as such rather than presented as a fact.
    """
    subject = (args.get("subject") or args.get("query") or "").strip()
    if not subject:
        return {"text": "Tell me who or what to look up connections for.", "cards": []}
    lib_id, lib_name = _resolve_library(args.get("library"))
    limit = int(args.get("limit") or 12)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT entity_id, canonical_name FROM entities
                    WHERE canonical_name ILIKE %s
                      AND COALESCE(validation_status,'') <> 'rejected'
                    ORDER BY (SELECT count(*) FROM document_mentions dm
                               WHERE dm.entity_id = entities.entity_id) DESC
                    LIMIT 1""", (f"%{subject}%",))
            row = cur.fetchone()
            if not row:
                return {"text": f"I don't have an entity matching “{subject}”.", "cards": []}
            eid, ename = int(row[0]), row[1]

            cur.execute(
                """SELECT CASE WHEN a.subj_entity_id=%(e)s THEN oe.canonical_name
                               ELSE se.canonical_name END AS other,
                          a.predicate_edge_type, a.corroborated_by_count
                     FROM assessments a
                LEFT JOIN entities se ON se.entity_id=a.subj_entity_id
                LEFT JOIN entities oe ON oe.entity_id=a.obj_entity_id
                    WHERE a.demoted_at IS NULL
                      AND (a.subj_entity_id=%(e)s OR a.obj_entity_id=%(e)s)
                    ORDER BY a.corroborated_by_count DESC NULLS LAST
                    LIMIT %(lim)s""", {"e": eid, "lim": limit})
            edges = [r for r in cur.fetchall() if r[0]]

            # Co-occurrence fallback — same paragraph, scoped to the library.
            cur.execute(
                """WITH d AS (
                     SELECT member_id AS document_id FROM library_members
                      WHERE member_kind='document'
                        AND (%(lib)s IS NULL OR library_id = %(lib)s))
                   SELECT e.canonical_name, count(*) shared
                     FROM document_mentions m1
                     JOIN d ON d.document_id = m1.document_id
                     JOIN document_mentions m2 ON m2.paragraph_id = m1.paragraph_id
                                              AND m2.entity_id <> m1.entity_id
                     JOIN entities e ON e.entity_id = m2.entity_id
                    WHERE m1.entity_id = %(e)s
                      AND COALESCE(e.validation_status,'') <> 'rejected'
                    GROUP BY 1 ORDER BY 2 DESC LIMIT %(lim)s""",
                {"e": eid, "lib": lib_id, "lim": limit})
            cooc = cur.fetchall()

    scope = f" in {lib_name.split('/')[-1]}" if lib_name else ""
    out = [f"**{ename}**{scope}", ""]
    if edges:
        out.append("_Governed relationships (collapsed from documents):_")
        for other, pred, corr in edges:
            c = f" · {corr} source{'s' if (corr or 0) != 1 else ''}" if corr else ""
            out.append(f"  • {pred} → **{other}**{c}")
        out.append("")
    if cooc:
        out.append("_Appears alongside (same paragraph — co-occurrence, not a claim):_")
        for other, shared in cooc:
            out.append(f"  • {other} ({shared} shared paragraph{'s' if shared != 1 else ''})")
    if not edges and not cooc:
        out.append("No connections recorded yet.")
    return {"text": "\n".join(out), "cards": [_card("EntityCard", {"entity_id": eid})]}


# ─── Dispatch ─────────────────────────────────────────────────────────

TOOL_FUNCTIONS = {
    "entity_lookup":     entity_lookup,
    "library_search":    library_search,
    "fact_search":       fact_search,
    "event_search":      event_search,
    "reference_search":  reference_search,
    "address_resolve":   address_resolve,
    "pipeline_status":   pipeline_status,
    "recent_facts":      recent_facts,
    "library_progress":  library_progress,
    "corpus_answer":     corpus_answer,
    "topic_timeline":    topic_timeline,
    "entity_connections": entity_connections,
}


def execute_tool(name: str, args: dict) -> dict:
    fn = TOOL_FUNCTIONS.get(name)
    if not fn:
        return {"text": f"Unknown tool '{name}'.", "cards": []}
    try:
        return fn(args or {})
    except Exception as e:
        log.exception("tool %s failed", name)
        return {"text": f"Tool '{name}' failed: {e}", "cards": []}


def render_tool_menu_for_prompt() -> str:
    """Return a string description of the tools, suitable for a system
    prompt to the LLM."""
    lines: list[str] = ["You have these tools available. Return JSON: {\"tool\": \"name\", \"args\": {...}}.", ""]
    for t in TOOL_SCHEMAS:
        lines.append(f"  {t['name']}: {t['description']}")
        for arg, desc in (t.get('args') or {}).items():
            lines.append(f"      - {arg}: {desc}")
    return "\n".join(lines)
