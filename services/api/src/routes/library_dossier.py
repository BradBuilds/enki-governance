"""
Library-as-dossier endpoint.

GET /libraries/{library_id}/dossier
  → unified dossier view bundling timeline + facts + network + leads
  + sources, all scoped to docs in this library.

A court-records library is the canonical use case: a few dozen case-file
PDFs → tens of thousands of mentions → hundreds of dated statements.
This route stitches those layers into one consumable JSON blob.

Noise filters applied:
  - FOIA boilerplate ("Approved For Release", "Sanitized Copy")
  - Legal headers ("Attorney Client", "Work Product", "Pro Hac Vice")
  - Paragraph starters ("And I", "In Re", "Case No")
  - URLs and web-page chatter where flagged
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException

from storage.postgres.db import get_connection

router = APIRouter(tags=["library_dossier"])
log = logging.getLogger("enki.routes.library_dossier")


# Surface forms that match common case-file boilerplate. Used to suppress
# in network + leads outputs (mentions still exist in the substrate; the
# dossier view just doesn't surface them).
_BOILERPLATE = {
    "approved for release", "sanitized copy approved", "public records request no",
    "attorney client", "work product", "pro hac vice", "case no", "in re",
    "and i", "but i", "entered by", "reported by", "incident report program",
    "protective order", "log privilege doc", "whether defendant",
    "lord god", "street n.", "security act",
    # Sentence-starter fragments matching the person-shape regex
    "on march", "on april", "on june", "on july", "on august",
    "on september", "on october", "on november", "on december",
    "on january", "on february", "on may",
    "the lady", "the decedent", "the defendant", "the petitioner",
    "the respondent", "the witness", "the deposition", "the investigation",
}

_BOILERPLATE_LIKE = (
    "%approved%", "%sanitized%", "%public records request%", "%attorney client%",
    "%work product%", "%pro hac vice%", "%incident report program%",
)

# Predicates that signal chat/blog noise rather than substantive case facts.
# These exist in the substrate because statement_extractor parses every verb;
# at the dossier-surface level we suppress them.
_CHAT_PREDICATES = {
    "see", "browse", "http", "view", "eat", "smoke", "vote for", "click",
    "miss", "try", "work", "watch", "full", "known", "release date",
    "forgetting", "around", "chose",
}

# Surface noise patterns in the raw_text — typical 2005-era blog/chat artefacts.
_CHAT_LIKE_PATTERNS = (
    "%click here%", "%release date:%", "%fuseaction=%", "%@%.com%",
    "%(cid:%", "%--page%", "%release date :%", "%page % of %",
    "%release date by%", "%release date - by%",
    "%release date - %", "%adventure collection%",
    "%home % browse % search%",
    # Airline PNR / GDS booking-record format. Such records use
    # SSR (Special Service Request), TKOK (ticket-confirmed), TKTL (ticket
    # time-limit), and inline 3-letter airport-code pairs like NYCEC,
    # EWRORY. Substrate keeps them; dossier surface drops them because
    # they read as gibberish to a human in a timeline view.
    "%SSR DOCS%", "%SSR SSR%", "%TKOK%", "%TKTL%", "%/PNR/%",
    "%CR-NYCEC%", "%CR-PAREC%", "%CR-LONEC%", "%CR-EWREC%",
    "%NYCEC0%", "%PAREC0%", "%LONEC0%", "%EWREC0%",
    "%EWRORY%", "%ORYEWR%", "%NYCFOR%", "%LONNYC%",
    "%(b) (6)%",
    "%ADT 005 Z/%", "%ADT 020 Z/%", "%ADT 018 Z/%",
    # Additional PNR / e-ticket fragments we missed on first pass
    "%RCVD- FD %", "%RCVD-FD%", "%E-TICKET ISSUED%", "%/POSVEGAUS%",
    "%/POS%/S/T%", "%LHRJFK%", "%JFKLHR%", "%LHREWR%", "%EWRLHR%",
    "%/AUTO/A%.%", "%MR(ADT%", "%HK1 1%", "%HK2 1%",
    # PNR fare/segment fragments — sequence numbers + route codes
    "% FA PAX %", "% FHE PAX %", "% C/F FO %",
    # Sanitized CIA cover sheets appearing as "events"
    "%Declassified in Part - Sanitized Copy%",
)

# Predicates that look like English verbs but are actually airline-format
# token leakage from PNR records. Used by the dossier timeline filter
# below to suppress these rows entirely rather than relying on
# raw_text pattern matching alone.
_AIRLINE_TOKEN_PREDICATES = {
    "mr", "mrs", "ms", "fa", "su", "tk", "tkok", "tktl",
    "adt", "chd", "inf", "p2", "p2s1", "ssr", "ec", "hk1",
    "fo", "ro", "ds", "cr", "et", "etec",
}


def _library_meta(library_id: int) -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT canonical_name, description, document_count,
                          entity_count, fact_count, focal_term,
                          discovery_status
                     FROM libraries WHERE library_id = %s""",
                (library_id,),
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(404, "library_not_found")
    return {
        "library_id": library_id,
        "name": row[0], "description": row[1],
        "document_count": row[2], "entity_count": row[3],
        "fact_count": row[4], "focal_term": row[5],
        "discovery_status": row[6],
    }


def _timeline(library_id: int, limit: int = 200) -> list[dict]:
    """Dated statements from library docs, deduped by raw_text+date, sorted
    chronologically. Filters chat/blog/PDF noise, suppresses chat-style
    predicates, requires a substantive text length.

    Also drops:
      - Airline PNR / GDS-format records (raw substrate stays for forensics;
        not shown in the human-readable timeline)
      - Predicates that are airline-format tokens leaked through extraction
      - Pre-1900 "dates" that are almost always numeric-substring parsing
        bugs from sequence numbers in PNR records
    """
    suppressed_predicates = tuple(_CHAT_PREDICATES | _AIRLINE_TOKEN_PREDICATES)
    with get_connection() as conn:
        with conn.cursor() as cur:
            # FOCUS: an event belongs on the TOPIC timeline only if it involves
            # one of the topic's core cast (the ~30 most-recurring persons /
            # organizations). This is what turns the unfocused "every dated
            # sentence in the corpus" firehose (unrelated names, tangents, junk
            # like "Red Pepper") into an Acme-about timeline, without a
            # cross-library scope change. The master timeline (unfiltered) is
            # the place to see everything.
            cur.execute(
                """WITH sig AS (
                        SELECT dm.entity_id
                          FROM document_mentions dm
                          JOIN library_members lm
                            ON lm.member_id = dm.document_id
                           AND lm.member_kind = 'document'
                           AND lm.library_id = %s
                          JOIN entities e ON e.entity_id = dm.entity_id
                         WHERE dm.entity_id IS NOT NULL
                           AND e.canonical_type IN ('person','organization')
                           AND NOT (e.envelope ? 'reclassified_to')
                         GROUP BY dm.entity_id
                        ORDER BY COUNT(DISTINCT dm.document_id) DESC
                        LIMIT 30
                   )
                   SELECT DISTINCT ON (s.event_time::date, LEFT(coalesce(s.summarized_text, s.raw_text), 70))
                          s.event_time::date AS date,
                          s.predicate,
                          coalesce(s.summarized_text, s.raw_text) AS text,
                          s.document_id, s.page_id, s.paragraph_id,
                          s.confidence
                     FROM statements s
                     JOIN library_members lm
                       ON lm.member_id = s.document_id
                      AND lm.member_kind = 'document'
                    WHERE lm.library_id = %s
                      AND s.event_time IS NOT NULL
                      AND s.event_time::date >= DATE '1900-01-01'
                      AND s.predicate NOT IN %s
                      AND s.raw_text NOT ILIKE ALL(%s::text[])
                      AND char_length(s.raw_text) >= 50
                      AND (s.speaker_entity_id IN (SELECT entity_id FROM sig)
                           OR s.object_entity_id IN (SELECT entity_id FROM sig))
                    ORDER BY s.event_time::date,
                             LEFT(coalesce(s.summarized_text, s.raw_text), 70),
                             s.confidence DESC NULLS LAST
                    LIMIT %s""",
                (library_id, library_id, suppressed_predicates,
                 list(_CHAT_LIKE_PATTERNS), limit),
            )
            rows = cur.fetchall()
    return [
        {
            "date": r[0].isoformat() if r[0] else None,
            "predicate": r[1],
            "text": (r[2] or "").strip(),
            "document_id": r[3],
            "page_id": r[4],
            "paragraph_id": r[5],
            "confidence": float(r[6]) if r[6] is not None else None,
        }
        for r in rows
    ]


def _facts(library_id: int, limit: int = 100) -> list[dict]:
    """High-signal typed claims — assessments of the form
    'subject [predicate] object', with corroboration counts and entity
    links. Pulls from the assessments table (clean structured triples)
    instead of raw statement text, so the Key Facts panel reads like a
    fact sheet ("Robin Vance met_with Alex Acme")
    rather than a search hit list.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            # FOCUS (same significant-entity rule as the timeline): a Key Fact
            # must involve one of the topic's core cast (top ~30 persons/orgs).
            # Drops boilerplate/lexicon junk ("Red Pepper relate Green Pepper",
            # "Mail browse Home") and one-off tangents whose subject isn't a
            # figure in the topic.
            cur.execute(
                """WITH sig AS (
                        SELECT dm.entity_id
                          FROM document_mentions dm
                          JOIN library_members lm
                            ON lm.member_id = dm.document_id
                           AND lm.member_kind = 'document'
                           AND lm.library_id = %s
                          JOIN entities e ON e.entity_id = dm.entity_id
                         WHERE dm.entity_id IS NOT NULL
                           AND e.canonical_type IN ('person','organization')
                           AND NOT (e.envelope ? 'reclassified_to')
                         GROUP BY dm.entity_id
                        ORDER BY COUNT(DISTINCT dm.document_id) DESC
                        LIMIT 30
                   )
                   SELECT a.assessment_id, a.predicate_edge_type,
                          a.subj_entity_id, ses.canonical_name AS subj_name,
                          a.obj_entity_id,  oes.canonical_name AS obj_name,
                          a.obj_value,
                          a.corroborated_by_count,
                          a.contradicted_by_count,
                          a.confidence,
                          a.source_statement_ids
                     FROM assessments a
                     JOIN library_members lm
                       ON lm.member_id = a.assessment_id
                      AND lm.member_kind = 'assessment'
                     LEFT JOIN entities ses ON ses.entity_id = a.subj_entity_id
                     LEFT JOIN entities oes ON oes.entity_id = a.obj_entity_id
                    WHERE lm.library_id = %s
                      AND a.subj_entity_id IS NOT NULL
                      AND (a.obj_entity_id IS NOT NULL OR
                           (a.obj_value IS NOT NULL AND char_length(a.obj_value) >= 2))
                      AND a.subj_entity_id != COALESCE(a.obj_entity_id, -1)
                      AND (a.subj_entity_id IN (SELECT entity_id FROM sig)
                           OR a.obj_entity_id IN (SELECT entity_id FROM sig))
                    ORDER BY a.corroborated_by_count DESC,
                             a.confidence DESC NULLS LAST,
                             a.assessment_id DESC
                    LIMIT %s""",
                (library_id, library_id, limit),
            )
            rows = cur.fetchall()

    # Pull one supporting raw_text per assessment so the UI has hover/quote.
    stmt_ids_pool: list[int] = []
    for r in rows:
        if r[10]:
            stmt_ids_pool.extend(r[10][:1])  # first source statement is enough
    quote_by_stmt: dict[int, tuple] = {}
    if stmt_ids_pool:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT statement_id, coalesce(summarized_text, raw_text),
                              document_id, page_id, paragraph_id
                         FROM statements
                        WHERE statement_id = ANY(%s)""",
                    (stmt_ids_pool,),
                )
                for sr in cur.fetchall():
                    quote_by_stmt[sr[0]] = sr

    facts = []
    for r in rows:
        (aid, pred, sid, sname, oid, oname, oval,
         corrob, contra, conf, src_ids) = r
        subj = sname or "?"
        obj = oname or oval or "?"
        pred_phrase = (pred or "").replace("_", " ")
        text = f"{subj} {pred_phrase} {obj}"
        quote = None
        doc_id = page_id = para_id = None
        if src_ids:
            sr = quote_by_stmt.get(src_ids[0])
            if sr:
                quote = (sr[1] or "").strip() or None
                doc_id, page_id, para_id = sr[2], sr[3], sr[4]
        facts.append({
            "assessment_id": aid,
            "predicate": pred_phrase,
            "subj_name": subj,
            "subj_entity_id": sid,
            "obj_name": obj,
            "obj_entity_id": oid,
            "text": text,
            "quote": quote,
            "document_id": doc_id,
            "page_id": page_id,
            "paragraph_id": para_id,
            "corroborated_by_count": corrob or 0,
            "contradicted_by_count": contra or 0,
            "confidence": float(conf) if conf is not None else None,
        })
    # On a deterministic-collapse library the assessment promotion layer may
    # be empty (statements never promoted to assessments). Fall back to the
    # resolved statements themselves so Key Facts still reads as a fact sheet
    # with verbatim receipts.
    if not facts:
        facts = _facts_from_statements(library_id, limit)
    return facts


def _facts_from_statements(library_id: int, limit: int = 100) -> list[dict]:
    """Facts built directly from resolved statements (speaker → predicate →
    object) when no assessments exist. raw_text is the verbatim receipt."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """WITH sig AS (
                        SELECT dm.entity_id
                          FROM document_mentions dm
                          JOIN library_members lm
                            ON lm.member_id = dm.document_id
                           AND lm.member_kind = 'document'
                           AND lm.library_id = %s
                          JOIN entities e ON e.entity_id = dm.entity_id
                         WHERE dm.entity_id IS NOT NULL
                           AND e.canonical_type IN ('person','organization')
                           AND NOT (e.envelope ? 'reclassified_to')
                         GROUP BY dm.entity_id
                        ORDER BY COUNT(DISTINCT dm.document_id) DESC
                        LIMIT 30
                   )
                   SELECT s.statement_id, s.predicate,
                          s.speaker_entity_id, ses.canonical_name AS subj_name,
                          s.object_entity_id,  oes.canonical_name AS obj_name,
                          coalesce(s.summarized_text, s.raw_text) AS quote,
                          s.corroborated_by_count, s.contradicted_by_count,
                          s.confidence, s.document_id, s.page_id, s.paragraph_id,
                          dp.validation_score,
                          coalesce(dp.metadata->>'quality_band', 'unknown') AS quality_band
                     FROM statements s
                     JOIN library_members lm
                       ON lm.member_id = s.document_id
                      AND lm.member_kind = 'document'
                     LEFT JOIN entities ses ON ses.entity_id = s.speaker_entity_id
                     LEFT JOIN entities oes ON oes.entity_id = s.object_entity_id
                     LEFT JOIN document_paragraphs dp ON dp.paragraph_id = s.paragraph_id
                    WHERE lm.library_id = %s
                      AND s.predicate IS NOT NULL
                      AND (s.speaker_entity_id IS NOT NULL
                           OR s.object_entity_id IS NOT NULL)
                      AND s.speaker_entity_id IS DISTINCT FROM s.object_entity_id
                      AND (s.speaker_entity_id IN (SELECT entity_id FROM sig)
                           OR s.object_entity_id IN (SELECT entity_id FROM sig))
                    ORDER BY (s.speaker_entity_id IS NOT NULL
                              AND s.object_entity_id IS NOT NULL) DESC,
                             -- clean receipts first: pass > flag/record > drop
                             (coalesce(dp.metadata->>'quality_band','') = 'pass') DESC,
                             dp.validation_score DESC NULLS LAST,
                             s.corroborated_by_count DESC NULLS LAST,
                             s.confidence DESC NULLS LAST,
                             s.statement_id DESC
                    LIMIT %s""",
                (library_id, library_id, limit),
            )
            rows = cur.fetchall()

    facts = []
    for r in rows:
        (stmt_id, pred, sid, sname, oid, oname, quote,
         corrob, contra, conf, doc_id, page_id, para_id,
         qual_score, qual_band) = r
        subj = sname or "?"
        obj = oname or ""
        pred_phrase = (pred or "").replace("_", " ")
        text = " ".join(p for p in (subj, pred_phrase, obj) if p).strip()
        facts.append({
            "assessment_id": None,
            "statement_id": stmt_id,
            "predicate": pred_phrase,
            "subj_name": subj,
            "subj_entity_id": sid,
            "obj_name": obj or "?",
            "obj_entity_id": oid,
            "text": text,
            "quote": (quote or "").strip() or None,
            "document_id": doc_id,
            "page_id": page_id,
            "paragraph_id": para_id,
            "corroborated_by_count": corrob or 0,
            "contradicted_by_count": contra or 0,
            "confidence": float(conf) if conf is not None else None,
            "quality_band": qual_band,
            "quality_score": float(qual_score) if qual_score is not None else None,
        })
    return facts


def _network(library_id: int, limit: int = 50) -> list[dict]:
    """Top co-occurring entities scored by mention volume × document spread.

    Joins through entity_mentions → entities so the panel shows canonical
    names ("Dana Vale", "Robin Vance") instead of raw
    surface-form fragments ("Acme", "Alex", "EWRORY"). Same-entity
    case variations and string fragments collapse into one row.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Rank top figures by how many INDEPENDENT documents feature them
            # (in_docs), not raw mention volume. Mention count is dominated by
            # ledger/manifest docs — a single donation dump or flight log repeats
            # one name thousands of times — which floated an aircraft (Acme
            # One) and a one-ledger name (Helen Marsh) above the actual
            # central figures. Ranking by document breadth mirrors the
            # corroboration metric used for edges. mentions kept as tiebreak.
            cur.execute(
                """WITH per_entity AS (
                     SELECT dm.entity_id,
                            e.canonical_name,
                            e.canonical_type,
                            count(*) AS mentions,
                            count(DISTINCT dm.document_id) AS in_docs
                       FROM document_mentions dm
                       JOIN entities e ON e.entity_id = dm.entity_id
                       JOIN library_members lm
                         ON lm.member_id = dm.document_id
                        AND lm.member_kind = 'document'
                      WHERE lm.library_id = %s
                        AND dm.entity_id IS NOT NULL
                        AND e.canonical_name IS NOT NULL
                        AND length(e.canonical_name) >= 3
                        AND NOT (e.envelope ? 'reclassified_to')
                      GROUP BY dm.entity_id, e.canonical_name, e.canonical_type
                   )
                   SELECT entity_id, canonical_name, mentions, in_docs,
                          canonical_type,
                          in_docs AS score
                     FROM per_entity
                    WHERE mentions >= 3
                    ORDER BY in_docs DESC, mentions DESC
                    LIMIT %s""",
                (library_id, limit),
            )
            rows = cur.fetchall()
    return [
        {"entity_id": r[0], "name": r[1], "mentions": r[2], "in_docs": r[3],
         "type": r[4], "score": r[5]}
        for r in rows
    ]


def _leads(library_id: int, limit: int = 30) -> list[dict]:
    """Canonical people with modest mention counts (3-50) in 2+ docs —
    names worth investigating that aren't already top-of-network. Joins
    through entity_mentions so we return real entities, not surface-form
    fragments."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """WITH per_entity AS (
                     SELECT dm.entity_id, e.canonical_name,
                            count(*) AS mentions,
                            count(DISTINCT dm.document_id) AS in_docs
                       FROM document_mentions dm
                       JOIN entities e ON e.entity_id = dm.entity_id
                       JOIN library_members lm
                         ON lm.member_id = dm.document_id
                        AND lm.member_kind = 'document'
                      WHERE lm.library_id = %s
                        AND dm.entity_id IS NOT NULL
                        AND e.canonical_type = 'person'
                        AND length(e.canonical_name) BETWEEN 4 AND 60
                        AND NOT (e.envelope ? 'reclassified_to')
                      GROUP BY dm.entity_id, e.canonical_name
                   )
                   SELECT entity_id, canonical_name, mentions, in_docs
                     FROM per_entity
                    WHERE in_docs >= 2
                      AND mentions BETWEEN 3 AND 50
                    ORDER BY in_docs DESC, mentions DESC
                    LIMIT %s""",
                (library_id, limit),
            )
            rows = cur.fetchall()
    return [
        {"entity_id": r[0], "name": r[1], "mentions": r[2], "in_docs": r[3]}
        for r in rows
    ]


def _sources(library_id: int) -> list[dict]:
    """Documents in this library, ordered by extraction richness."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT d.document_id, d.title, d.source_url,
                          (SELECT count(*) FROM document_paragraphs dp
                            WHERE dp.document_id = d.document_id) AS paras,
                          (SELECT count(*) FROM document_mentions dm
                            WHERE dm.document_id = d.document_id) AS mentions,
                          (SELECT count(*) FROM statements s
                            WHERE s.document_id = d.document_id) AS statements
                     FROM documents d
                     JOIN library_members lm
                       ON lm.member_id = d.document_id
                      AND lm.member_kind = 'document'
                    WHERE lm.library_id = %s
                      AND d.content_sha256 IS NOT NULL
                    ORDER BY mentions DESC""",
                (library_id,),
            )
            rows = cur.fetchall()
    return [
        {
            "document_id": r[0], "title": r[1], "source_url": r[2],
            "paragraphs": r[3], "mentions": r[4], "statements": r[5],
        }
        for r in rows
    ]


@router.get("/libraries/{library_id}/dossier")
async def get_library_dossier(
    library_id: int,
    timeline_limit: int = 200,
    facts_limit: int = 100,
    network_limit: int = 50,
    leads_limit: int = 30,
):
    """Bundle every extracted layer for one library into one response."""
    meta = _library_meta(library_id)
    return {
        "meta": meta,
        "timeline": _timeline(library_id, limit=timeline_limit),
        "facts": _facts(library_id, limit=facts_limit),
        "network": _network(library_id, limit=network_limit),
        "leads": _leads(library_id, limit=leads_limit),
        "sources": _sources(library_id),
    }


# ── Library mirroring (Batch A of federated library sharing) ───────────────
# These are subscriber-side endpoints. When an operator wants to mirror a
# library hosted on another node, they call POST /libraries/mirror with the
# upstream details. This endpoint:
#   1. Creates a NEW local libraries row (a "mirror" copy with subtype set
#      to 'mirror_library') that the library_subscriber worker will populate
#   2. Creates a library_mirrors row tracking the upstream
#   3. Calls the upstream's POST /fleet/libraries/{id}/subscribe to register
#      this node as a subscriber on the publisher side
#   4. Returns the local library_id so the UI can deep-link to it
# The library_subscriber.py worker takes over from there.


@router.post("/library-mirrors")
async def mirror_upstream_library(body: dict):
    """Operator-initiated: subscribe to and start mirroring an upstream
    library hosted on a different node.

    Request body:
        {
          "upstream_node_id":    "node:abc123...",
          "upstream_library_id": 1,
          "upstream_url":        "https://localhost",
          "canonical_name":      "topic_library/Alex Acme"  (optional;
                                    fetched from upstream if absent)
        }
    """
    import httpx
    import logging

    from storage.postgres.db import get_connection

    log = logging.getLogger("enki.routes.library_mirror")

    upstream_node_id = (body.get("upstream_node_id") or "").strip()
    upstream_library_id = body.get("upstream_library_id")
    upstream_url = (body.get("upstream_url") or "").strip().rstrip("/")
    canonical_name = body.get("canonical_name") or ""

    if not upstream_node_id or not upstream_library_id or not upstream_url:
        raise HTTPException(400,
            "upstream_node_id, upstream_library_id, and upstream_url are required")

    # 1. Check we're not already mirroring this. Idempotent — returns
    #    existing mirror if found.
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT mirror_id, local_library_id FROM library_mirrors
                    WHERE upstream_node_id = %s AND upstream_library_id = %s""",
                (upstream_node_id, upstream_library_id),
            )
            existing = cur.fetchone()
            if existing:
                return {
                    "status": "already_mirrored",
                    "mirror_id": existing[0],
                    "local_library_id": existing[1],
                }

    # 2. Fetch upstream library metadata (gives us a canonical name + a
    #    sanity check that the upstream is actually reachable)
    if not canonical_name:
        try:
            with httpx.Client(timeout=10) as client:
                r = client.get(f"{upstream_url}/libraries/{upstream_library_id}")
                if r.status_code == 200:
                    canonical_name = r.json().get("canonical_name") or f"mirror_library/{upstream_library_id}"
                else:
                    canonical_name = f"mirror_library/{upstream_library_id}"
        except Exception as e:
            log.warning("failed to fetch upstream library metadata: %s", e)
            canonical_name = f"mirror_library/{upstream_library_id}"

    # 3. Create local library row + library_mirrors row in one tx.
    #    Use library_subtype from canonical_name prefix when available
    #    (e.g. "topic_library/Alex Acme" → topic_library); fall back
    #    to topic_library since that's the most common mirror case. The
    #    library_subtype CHECK constraint only allows: entity_library,
    #    relationship_library, event_library, topic_library, corpus_library.
    valid_subtypes = {
        "entity_library", "relationship_library", "event_library",
        "topic_library", "corpus_library",
    }
    local_subtype = "topic_library"
    if "/" in canonical_name:
        prefix = canonical_name.split("/", 1)[0]
        if prefix in valid_subtypes:
            local_subtype = prefix
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO libraries (
                    base60_address, library_subtype, canonical_name,
                    focal_term, description, tenant_id, discovery_status
                ) VALUES (
                    '', %s, %s,
                    %s,
                    %s,
                    'default', 'active'
                )
                RETURNING library_id
                """,
                (
                    local_subtype,
                    canonical_name,
                    canonical_name.split("/", 1)[1] if "/" in canonical_name else canonical_name,
                    f"Mirror of {canonical_name} from {upstream_url}",
                ),
            )
            local_library_id = cur.fetchone()[0]

            cur.execute(
                """
                INSERT INTO library_mirrors (
                    local_library_id, upstream_node_id, upstream_library_id,
                    upstream_url, status
                ) VALUES (%s, %s, %s, %s, 'active')
                RETURNING mirror_id
                """,
                (local_library_id, upstream_node_id, upstream_library_id, upstream_url),
            )
            mirror_id = cur.fetchone()[0]
        conn.commit()

    # 4. Call the upstream's subscribe endpoint so the publisher knows we
    #    exist + assigns us to work-distribution (best-effort; failure here
    #    doesn't roll back the local mirror — the operator can still pull
    #    via the manifest without being on the publisher's subscriber list)
    upstream_subscribe_ok = False
    upstream_error = None
    try:
        from core.federation.phase4 import NODE_ID
        with httpx.Client(timeout=15) as client:
            r = client.post(
                f"{upstream_url}/fleet/libraries/{upstream_library_id}/subscribe",
                json={"node_id": NODE_ID},
            )
            upstream_subscribe_ok = r.status_code in (200, 201)
            if not upstream_subscribe_ok:
                upstream_error = f"{r.status_code}: {r.text[:200]}"
    except Exception as e:
        upstream_error = str(e)[:300]

    return {
        "status": "mirror_created",
        "mirror_id": mirror_id,
        "local_library_id": local_library_id,
        "upstream_node_id": upstream_node_id,
        "upstream_library_id": upstream_library_id,
        "upstream_subscribe_ok": upstream_subscribe_ok,
        "upstream_error": upstream_error,
        "message": (
            "Mirror created. The library_subscriber worker will fetch the "
            "manifest and start pulling content on its next tick "
            "(~15 minutes). Open the local library at "
            f"/library/{local_library_id} to watch progress."
        ),
    }


@router.delete("/library-mirrors/{mirror_id}")
async def unmirror_library(mirror_id: int):
    """Stop mirroring a library. Sets status='revoked'; the subscriber
    worker will skip it on its next pass. Does NOT delete local content
    that was already pulled — that stays in the local library."""
    from storage.postgres.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE library_mirrors SET status = 'revoked'
                    WHERE mirror_id = %s RETURNING local_library_id""",
                (mirror_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "mirror not found")
        conn.commit()

    return {"status": "revoked", "mirror_id": mirror_id, "local_library_id": row[0]}


@router.get("/library-mirrors")
async def list_mirrors():
    """List all library mirrors on this node — what we're subscribed to
    and the pull progress for each. Powers the /library page sidebar."""
    from storage.postgres.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.mirror_id, m.local_library_id, m.upstream_node_id,
                       m.upstream_library_id, m.upstream_url, m.status,
                       m.manifest_hash, m.last_synced_at, m.last_sync_error,
                       m.documents_pulled, m.documents_failed, m.bytes_pulled,
                       m.created_at,
                       l.canonical_name, l.focal_term
                  FROM library_mirrors m
                  LEFT JOIN libraries l ON l.library_id = m.local_library_id
                 ORDER BY m.created_at DESC
                """
            )
            rows = cur.fetchall()

    items = [
        {
            "mirror_id":           int(r[0]),
            "local_library_id":    int(r[1]),
            "upstream_node_id":    r[2],
            "upstream_library_id": int(r[3]),
            "upstream_url":        r[4],
            "status":              r[5],
            "manifest_hash":       r[6],
            "last_synced_at":      r[7].isoformat() if r[7] else None,
            "last_sync_error":     r[8],
            "documents_pulled":    int(r[9] or 0),
            "documents_failed":    int(r[10] or 0),
            "bytes_pulled":        int(r[11] or 0),
            "created_at":          r[12].isoformat() if r[12] else None,
            "canonical_name":      r[13],
            "focal_term":          r[14],
        }
        for r in rows
    ]
    return {"mirrors": items, "count": len(items)}
