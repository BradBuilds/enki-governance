"""
Corpus answer — "what do the files say about X".

The query tools (chat_tools) return navigation CARDS. This instead READS the
actual evidence Enki collapsed about an entity — the statements plus the source
document paragraphs where it's mentioned — and has the model SYNTHESIZE a
grounded, cited answer. It never uses the model's free recall: every claim is
backed by a numbered source the user can open.

Entry point:
    answer_about(question, name, tenant_id) -> dict
"""

from __future__ import annotations

import logging
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.ai.corpus_answer")


def _resolve_entity(name: str, dsn: Optional[str] = None,
                    library_id: Optional[int] = None) -> Optional[tuple[int, str, int]]:
    """Best entity match for a name = the candidate with the most mentions
    (so a bare surname resolves to the prominent figure, not a fragment
    entity that happens to share it). Returns
    (entity_id, canonical_name, mention_count).

    When `library_id` is given, both the ranking and the mention count are
    restricted to documents in that library. Without it a topic-scoped assistant
    silently answers from the whole corpus — ask a question about one topic and
    the best-ranked name can come from a different investigation entirely.

    NOTE the member_kind filter: library_members is keyed
    (library_id, member_kind, member_id), so entity 366 and document 366 are
    different objects that share an integer. Joining document_id against
    library_members without member_kind='document' matches unrelated rows by
    coincidence and invents cross-corpus links.
    """
    scoped = library_id is not None
    mention_count_sql = """
                       (SELECT count(*) FROM document_mentions dm
                         WHERE dm.entity_id = e.entity_id) AS c"""
    if scoped:
        mention_count_sql = """
                       (SELECT count(*) FROM document_mentions dm
                          JOIN library_members lm
                            ON lm.member_id = dm.document_id
                           AND lm.library_id = %(lib)s
                           AND lm.member_kind = 'document'
                         WHERE dm.entity_id = e.entity_id) AS c"""
    # Rank the FULL match set by mentions. An earlier version pre-filtered to the
    # 30 shortest canonical names before ranking, which silently defeated the
    # mention ranking on any common surname: a widely-mentioned figure whose
    # canonical name is longer than the 30th-shortest match never entered the
    # pool, so the query resolved to a minor namesake instead. Length is now only
    # a tiebreak between equally-mentioned candidates.
    #
    # Aliases are matched too: canonical names may be stored surname-first
    # ("LAST, FIRST M."), so a natural-order query matches nothing on
    # canonical_name alone — that form lives in aliases[].
    sql = f"""
                SELECT e.entity_id, e.canonical_name,
                       {mention_count_sql}
                  FROM entities e
                 WHERE (e.canonical_name ILIKE %(pat)s
                        OR EXISTS (SELECT 1 FROM unnest(e.aliases) a
                                    WHERE a ILIKE %(pat)s))
                   AND COALESCE(e.validation_status, '') <> 'rejected'
                 ORDER BY c DESC, char_length(e.canonical_name) ASC
                 LIMIT 1
    """
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"pat": f"%{name}%", "lib": library_id})
            row = cur.fetchone()
    # A name that exists globally but has zero mentions inside the library is
    # not an answer for a topic-scoped question — abstain rather than reach out
    # of scope.
    if scoped and row and int(row[2] or 0) == 0:
        return None
    if not row or not row[0]:
        return None
    return int(row[0]), row[1], int(row[2] or 0)


def _gather_evidence(entity_id: int, dsn: Optional[str] = None) -> dict:
    """Pull the real collapsed evidence: statements + the source paragraphs
    where the entity is mentioned, grouped into numbered sources."""
    statements: list[dict] = []
    passages: list[dict] = []
    sources: list[dict] = []
    doc_marker: dict[int, int] = {}

    def _source_marker(doc_id, title, url) -> int:
        if doc_id not in doc_marker:
            doc_marker[doc_id] = len(sources) + 1
            sources.append({"marker": doc_marker[doc_id], "document_id": doc_id,
                            "title": title, "source_url": url})
        return doc_marker[doc_id]

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Statements where the entity is subject or object.
            cur.execute(
                """
                SELECT COALESCE(NULLIF(s.summarized_text,''), s.raw_text) AS txt,
                       s.predicate, s.document_id, d.title, d.source_url
                  FROM statements s
                  LEFT JOIN documents d ON d.document_id = s.document_id
                 WHERE (s.object_entity_id = %s OR s.speaker_entity_id = %s)
                   AND COALESCE(s.summarized_text, s.raw_text) IS NOT NULL
                   AND COALESCE(s.validation_status,'') <> 'rejected'
                   AND COALESCE(d.relevance_status,'') <> 'quarantined'
                 ORDER BY s.confidence DESC NULLS LAST
                 LIMIT 20
                """,
                (entity_id, entity_id),
            )
            for txt, predicate, doc_id, title, url in cur.fetchall():
                m = _source_marker(doc_id, title, url) if doc_id else None
                statements.append({"text": (txt or "").strip()[:400],
                                   "predicate": predicate, "marker": m})

            # Source paragraphs where the entity is mentioned.
            cur.execute(
                """
                SELECT DISTINCT ON (p.paragraph_id)
                       substr(p.raw_text, 1, 500) AS txt,
                       d.document_id, d.title, d.source_url
                  FROM document_mentions dm
                  JOIN document_paragraphs p ON p.paragraph_id = dm.paragraph_id
                  JOIN documents d ON d.document_id = dm.document_id
                 WHERE dm.entity_id = %s
                   AND p.raw_text IS NOT NULL AND length(p.raw_text) > 40
                   -- Quarantined documents stay on disk as evidence but must never
                   -- be quoted back as "what the files say". A document removed from
                   -- a corpus for being out of scope is still readable by an operator;
                   -- it is not a source the assistant may cite.
                   AND COALESCE(d.relevance_status,'') <> 'quarantined'
                 LIMIT 24
                """,
                (entity_id,),
            )
            for txt, doc_id, title, url in cur.fetchall():
                m = _source_marker(doc_id, title, url)
                passages.append({"text": " ".join((txt or "").split())[:500], "marker": m})

    return {"statements": statements, "passages": passages, "sources": sources}


def answer_about(question: str, name: str, tenant_id: str = "default",
                 dsn: Optional[str] = None, library_id: Optional[int] = None) -> dict:
    """Synthesize a grounded answer about `name` from the collapsed corpus.

    Returns {answer, entity_id, canonical_name, mention_count, sources, grounded}.
    """
    name = (name or "").strip()
    if not name:
        return {"answer": "Tell me who or what to look into.", "sources": [], "grounded": False}

    resolved = _resolve_entity(name, dsn, library_id=library_id)
    if not resolved:
        scope = f" in this library" if library_id is not None else ""
        return {"answer": f"I don't have anything in the files about “{name}”{scope} yet.",
                "sources": [], "grounded": False}
    entity_id, canonical_name, mention_count = resolved

    ev = _gather_evidence(entity_id, dsn)
    if not ev["statements"] and not ev["passages"]:
        return {"answer": (f"**{canonical_name}** appears in the corpus ({mention_count} "
                           f"mention{'s' if mention_count != 1 else ''}) but nothing has been "
                           f"collapsed into readable statements yet, so I can't summarize what "
                           f"the files say. Try running collapse on its library."),
                "entity_id": entity_id, "canonical_name": canonical_name,
                "mention_count": mention_count, "sources": [], "grounded": False}

    # Build a grounded prompt — every piece of evidence carries its [n] source.
    lines: list[str] = []
    for s in ev["statements"]:
        tag = f" [{s['marker']}]" if s.get("marker") else ""
        lines.append(f"- (statement) {s['text']}{tag}")
    for p in ev["passages"]:
        lines.append(f"- (source text) {p['text']} [{p['marker']}]")
    evidence_block = "\n".join(lines[:38])

    prompt = (
        f"Question: {question or ('What do the files say about ' + canonical_name + '?')}\n\n"
        f"Answer ONLY from the evidence below — do not use outside knowledge. Write a clear, "
        f"specific summary of what the files actually say about {canonical_name}. Cite sources "
        f"inline as [n]. If the evidence is thin or one-sided, say so plainly.\n\n"
        f"EVIDENCE:\n{evidence_block}"
    )

    answer = ""
    try:
        from core.ai.config import get_tenant_provider
        from core.ai.provider import LLMMessage
        provider = get_tenant_provider(tenant_id, role="chat")
        if provider:
            resp = provider.complete([LLMMessage(role="user", content=prompt)],
                                     max_tokens=1000, temperature=0.2)
            answer = (resp.content or "").strip()
    except Exception as e:
        log.debug("corpus answer synthesis failed: %s", e)

    if not answer:
        # Degrade gracefully to the raw evidence rather than nothing.
        answer = (f"Here's what the files contain about **{canonical_name}** "
                  f"({mention_count} mentions):\n\n" +
                  "\n".join(f"- {p['text']} [{p['marker']}]" for p in ev["passages"][:8]))

    return {
        "answer": answer, "entity_id": entity_id, "canonical_name": canonical_name,
        "mention_count": mention_count, "sources": ev["sources"], "grounded": True,
    }
