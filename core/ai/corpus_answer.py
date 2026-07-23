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


def _resolve_entity(name: str, dsn: Optional[str] = None) -> Optional[tuple[int, str, int]]:
    """Best entity match for a name = the candidate with the most mentions
    (so 'vale' → the real Dana Vale, not a fragment). Returns
    (entity_id, canonical_name, mention_count)."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.entity_id, e.canonical_name,
                       (SELECT count(*) FROM document_mentions dm
                         WHERE dm.entity_id = e.entity_id) AS c
                  FROM (
                    SELECT entity_id, canonical_name FROM entities
                     WHERE canonical_name ILIKE %s
                     ORDER BY char_length(canonical_name) ASC
                     LIMIT 30
                  ) e
                 ORDER BY c DESC
                 LIMIT 1
                """,
                (f"%{name}%",),
            )
            row = cur.fetchone()
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
                 LIMIT 24
                """,
                (entity_id,),
            )
            for txt, doc_id, title, url in cur.fetchall():
                m = _source_marker(doc_id, title, url)
                passages.append({"text": " ".join((txt or "").split())[:500], "marker": m})

    return {"statements": statements, "passages": passages, "sources": sources}


def answer_about(question: str, name: str, tenant_id: str = "default",
                 dsn: Optional[str] = None) -> dict:
    """Synthesize a grounded answer about `name` from the collapsed corpus.

    Returns {answer, entity_id, canonical_name, mention_count, sources, grounded}.
    """
    name = (name or "").strip()
    if not name:
        return {"answer": "Tell me who or what to look into.", "sources": [], "grounded": False}

    resolved = _resolve_entity(name, dsn)
    if not resolved:
        return {"answer": f"I don't have anything in the files about “{name}” yet.",
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
        provider = get_tenant_provider(tenant_id)
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
