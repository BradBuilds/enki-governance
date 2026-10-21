"""
Statement extractor — LLM-driven subject-predicate-object extraction with
spatial-temporal qualifiers.

Reads a paragraph + the mentions already extracted in it, then calls the
LLM with a prompt asking for ALL factual claims:

    (subject_mention, predicate_verb, object_mention, raw_text,
     char_start, char_end, modality, polarity, confidence,
     date_iso, location_text)

Persists each as a `statements` row. Resolution to canonical entities,
validation, and collapse to assessments happens in a separate pass
(assessment_promoter.py).

This is the bridge between mentions (surface text) and assessments
(canonical facts). Without it the cyclical model has structure but no
content. With it the model accumulates evidence.

Public surface:
    extract_from_paragraph(paragraph_id, llm=...) -> dict
    extract_from_document(document_id, llm=...)  -> dict
    extract_from_corpus(dataset_id, llm=...)     -> dict
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.documents.statement_extractor")


_STATEMENT_PROMPT_TEMPLATE = """\
You are a meticulous fact extractor for an intelligence analysis system.

A paragraph from a real document is given below, along with the proper-noun
mentions already identified in it. Your task is to extract every factual
claim the paragraph makes — subject-predicate-object propositions about
what happened, when, where, and by whom.

CRITICAL RULES:
- Only emit claims that are EXPLICITLY supported by the paragraph text.
- Do NOT speculate or infer beyond the text.
- Each claim must reference real mentions from the list provided (use their surface_form).
- The predicate must be a SINGLE VERB IN BASE FORM (own, fly, meet, pay, accuse, ...).
  Do not use phrasal verbs like "flew with"; just "fly".
- For each claim extract:
    * subject — surface_form of one mention from the list (the actor)
    * predicate_verb — the base-form verb that asserts the action
    * object — surface_form of another mention OR a literal value (date, amount, etc.)
    * raw_text — the exact sentence containing the claim
    * date_iso — when the asserted action happened (ISO 8601 if available, null if not stated)
    * location_text — where the action occurred (place name as text, null if not stated)
    * modality — "asserted" | "hedged" | "quoted" | "denied"
    * polarity — "affirmative" | "negative". ONLY about whether the sentence
      asserts the predicate HOLDS between subject and object, or asserts that it
      does NOT. Not about negative words appearing anywhere, and not about the
      verb's meaning:
        "The court denied the motion."         -> deny,  AFFIRMATIVE (it did deny)
        "Respondent has not filed since 2015." -> file,  NEGATIVE    (no filing)
        "The author wrote, 'this isn't much.'" -> write, AFFIRMATIVE (they did write)
      If the negation is not attached to the predicate you extracted, polarity
      is affirmative.
    * confidence — 0.0..1.0 (your own confidence the claim is in the text)

Return ONLY a JSON object of the shape:
  {"statements": [
     {"subject": "...", "predicate_verb": "...", "object": "...",
      "raw_text": "...", "date_iso": null, "location_text": null,
      "modality": "asserted", "polarity": "affirmative", "confidence": 0.9}
  ]}

If the paragraph contains no clear factual claims, return {"statements": []}.

Mentions found in this paragraph (use exact surface_form when referring):
<mentions>
{mentions_block}
</mentions>

Paragraph text:
<text>
{text}
</text>
"""

_MAX_PARAGRAPH_CHARS = 4000


# ─── LLM extraction ───────────────────────────────────────────────────

def _build_mentions_block(mentions: list[dict]) -> str:
    if not mentions:
        return "(no proper-noun mentions identified in this paragraph)"
    return "\n".join(
        f"- {m['surface_form']} ({m.get('suggested_type','unknown')})"
        for m in mentions[:30]
    )


def _parse_date_iso(s: Optional[str]) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if not s:
        return None
    fmts = [
        "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y-%m", "%Y",
    ]
    for fmt in fmts:
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            continue
    return None


def _grounded_event_time(date_iso: Optional[str], source_text: str) -> Optional[datetime]:
    """Parse date_iso but keep it only if its YEAR appears verbatim in the
    source text. The LLM sometimes asserts a year the document never states —
    inferred from a fax-transmit header ("DEC-22-2446 ..."), an OCR-broken year
    ("March 24, 20 I 0"), or by defaulting to the document's release year. A
    wrong date pollutes the timeline worse than no date, so abstain (return
    None) when the year isn't grounded in the text. This is the same
    verbatim-receipt discipline the rest of the collapse uses."""
    dt = _parse_date_iso(date_iso)
    if dt is None:
        return None
    text = source_text or ""
    # Grounded if the full 4-digit year is present, OR the 2-digit year appears
    # in a date-separator context (MM/DD/YY, DD-MM-YY) — common in these docs.
    # The separator + no-trailing-digit guard avoids matching a bare day ("March
    # 24" -> not "/24") or a run inside another number (fax "…-2446" -> the "-24"
    # is followed by "4", so it's rejected).
    yy = f"{dt.year % 100:02d}"
    if str(dt.year) in text or re.search(r"[/.\-]" + yy + r"(?!\d)", text):
        return dt
    return None


def _llm_extract_statements(text: str, mentions: list[dict], llm: Any) -> list[dict]:
    if not text or not llm:
        return []
    truncated = text[:_MAX_PARAGRAPH_CHARS]
    prompt = (
        _STATEMENT_PROMPT_TEMPLATE
        .replace("{mentions_block}", _build_mentions_block(mentions))
        .replace("{text}", truncated)
    )
    try:
        from core.ai.provider import LLMMessage
        messages = [
            LLMMessage(role="system",
                       content="You return only valid JSON. No commentary, no markdown fences."),
            LLMMessage(role="user", content=prompt),
        ]
        response = llm.complete(messages, max_tokens=1500, temperature=0.0)
    except Exception as e:
        log.debug("LLM call failed: %s", e)
        return []
    if response is None or getattr(response, "error", None):
        return []
    raw = (response.content or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    if raw.startswith("json"):
        raw = raw[4:].lstrip()
    try:
        parsed = json.loads(raw)
    except Exception:
        try:
            s, e = raw.index("{"), raw.rindex("}") + 1
            parsed = json.loads(raw[s:e])
        except Exception:
            return []
    if not isinstance(parsed, dict):
        return []
    stmts = parsed.get("statements") or []
    if not isinstance(stmts, list):
        return []

    out: list[dict] = []
    for s in stmts:
        if not isinstance(s, dict):
            continue
        subject = (s.get("subject") or "").strip()
        verb    = (s.get("predicate_verb") or "").strip().lower()
        obj     = s.get("object")
        obj     = (str(obj).strip() if obj is not None else "")
        if not subject or not verb:
            continue
        out.append({
            "subject":     subject,
            "verb":        verb,
            "object":      obj,
            "raw_text":    (s.get("raw_text") or "")[:2000],
            "date_iso":    s.get("date_iso"),
            "location_text": s.get("location_text"),
            "modality":    s.get("modality") or "asserted",
            "polarity":    s.get("polarity") or "affirmative",
            "confidence":  float(s.get("confidence") or 0.6),
        })
    return out


# ─── Mention resolution ───────────────────────────────────────────────

def _find_mention_for_surface(
    paragraph_id: int, surface: str
) -> Optional[tuple[int, int]]:
    """Return (mention_id, entity_id) for the mention whose surface_form
    most closely matches `surface` in this paragraph. None if no match.

    Resolution by exact case-insensitive equality first, then by
    substring containment.
    """
    if not surface:
        return None
    surf_n = surface.strip().upper()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT mention_id, entity_id, surface_form_norm
                  FROM document_mentions
                 WHERE paragraph_id = %s
                """,
                (paragraph_id,),
            )
            rows = cur.fetchall()
    if not rows:
        return None
    # exact
    for mid, eid, norm in rows:
        if norm and norm.strip().upper() == surf_n:
            return (mid, eid)
    # substring (surface appears in norm or vice-versa)
    for mid, eid, norm in rows:
        if not norm:
            continue
        if surf_n in norm.upper() or norm.upper() in surf_n:
            return (mid, eid)
    return None


# ─── Persistence ──────────────────────────────────────────────────────

def _persist_statement(
    *,
    tenant_id: str,
    document_id: int,
    page_id: Optional[int],
    paragraph_id: int,
    speaker_mention_id: Optional[int],
    speaker_entity_id: Optional[int],
    predicate: str,
    object_mention_id: Optional[int],
    object_entity_id: Optional[int],
    raw_text: Optional[str],
    event_time: Optional[datetime],
    location_text: Optional[str],
    modality: str,
    polarity: str,
    confidence: float,
    predicate_verb_surface: str,
    predicate_lemma: str,
    extraction_profile_id: Optional[str] = None,
    object_text: Optional[str] = None,
) -> int:
    """Persist one statement.

    `location_text` goes in its OWN column. It used to be passed into
    `surrounding_text` — a field named for sentence context — which stranded
    3,256 place names, left `event_location_entity_id` NULL on every row, and
    made the UI render a bare place name as if it were the surrounding
    sentence. `object_text` is kept even when the object resolved to no
    mention, so the claim can be re-resolved when the substrate improves.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            location_entity_id = None
            if location_text:
                try:
                    from core.documents.place_observations import agreed_entity_id
                    location_entity_id, _reason = agreed_entity_id(cur, location_text)
                except Exception:
                    log.exception("place observation failed for %r", location_text)
            cur.execute(
                """
                INSERT INTO statements
                    (tenant_id, document_id, page_id, paragraph_id,
                     speaker_entity_id, speaker_mention_id,
                     predicate, object_entity_id, object_mention_id,
                     raw_text, summarized_text,
                     modality, confidence, polarity,
                     extraction_method, event_time,
                     predicate_verb_surface, predicate_lemma,
                     location_text, event_location_entity_id,
                     object_text, validation_status,
                     extraction_profile_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, 'llm_v1', %s, %s, %s, %s, %s, %s,
                        'pending', %s)
                RETURNING statement_id
                """,
                (tenant_id, document_id, page_id, paragraph_id,
                 speaker_entity_id, speaker_mention_id,
                 predicate, object_entity_id, object_mention_id,
                 raw_text, raw_text,
                 modality, confidence, polarity,
                 event_time,
                 predicate_verb_surface, predicate_lemma,
                 location_text, location_entity_id,
                 object_text,
                 extraction_profile_id),
            )
            stmt_id = cur.fetchone()[0]
        conn.commit()
    return stmt_id


def _lemmatize_verb(surface: str) -> str:
    """Light verb-form normalizer.

    The promoter's _variants() does aggressive morphological expansion
    at dictionary-lookup time (-ed/-ing/-ied/-s/-irregular pairs), so
    this function only needs to do safe, unambiguous normalization:
    lowercase, trim, and preserve short copulas/auxiliaries that the
    naive -s rule would chop to nonsense.

    Storing the lightly-normalized surface means _variants() can later
    try ['described', 'describ', 'describe', 'describe d'-form, etc.]
    and pick whichever matches the dictionary, instead of being stuck
    with a guess we made at write time.
    """
    if not surface:
        return ""
    return surface.strip().lower()


# ─── Entry points ─────────────────────────────────────────────────────

def extract_from_paragraph(
    *, tenant_id: str = "default", document_id: int, page_id: Optional[int],
    paragraph_id: int, text: str, llm: Any
) -> dict:
    """Extract statements from one paragraph. Returns counts."""
    # Get the mentions already extracted in this paragraph (LLM uses them
    # as the legal set of subjects/objects).
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT surface_form, suggested_type, mention_id, entity_id
                  FROM document_mentions
                 WHERE paragraph_id = %s
                 ORDER BY char_start
                """,
                (paragraph_id,),
            )
            mentions = [
                {"surface_form": r[0], "suggested_type": r[1],
                 "mention_id": r[2], "entity_id": r[3]}
                for r in cur.fetchall()
            ]

    candidates = _llm_extract_statements(text, mentions, llm)
    # Derive once per paragraph so federation publisher can discover
    # these rows. Falls back to 'legacy_v1' if registry is unavailable.
    try:
        from core.extraction.profile_registry import derive_profile_from_llm
        _profile_id = derive_profile_from_llm(
            llm, prompt_template="statement_extraction_v1", prompt_version="v1",
        )
    except Exception:
        _profile_id = "legacy_v1"
    persisted = 0
    skipped = {"no_subject_mention": 0, "no_object_mention": 0, "error": 0,
               "self_reference": 0, "fragment": 0, "table_noise": 0}

    for c in candidates:
        try:
            # Drop noise BEFORE entity resolution so we don't waste lookups.
            if _is_fragment_or_table(c.get("raw_text") or ""):
                skipped["fragment"] += 1
                continue

            subj_ref = _find_mention_for_surface(paragraph_id, c["subject"])
            if not subj_ref:
                skipped["no_subject_mention"] += 1
                continue
            obj_ref = _find_mention_for_surface(paragraph_id, c["object"]) if c["object"] else None
            # Object may be a literal value (date, sum, country) when no mention matches
            obj_mention_id, obj_entity_id = (obj_ref or (None, None))

            # Drop self-references — entity X relating to itself produces no
            # learning signal and pollutes the assessment layer.
            if (subj_ref[1] is not None and obj_entity_id is not None
                    and subj_ref[1] == obj_entity_id):
                skipped["self_reference"] += 1
                continue

            _persist_statement(
                tenant_id=tenant_id,
                document_id=document_id,
                page_id=page_id,
                paragraph_id=paragraph_id,
                speaker_mention_id=subj_ref[0],
                speaker_entity_id=subj_ref[1],
                predicate=c["verb"],
                object_mention_id=obj_mention_id,
                object_entity_id=obj_entity_id,
                raw_text=c["raw_text"],
                event_time=_grounded_event_time(c.get("date_iso"), text),
                location_text=c.get("location_text"),
                modality=c["modality"],
                polarity=c["polarity"],
                confidence=c["confidence"],
                predicate_verb_surface=c["verb"],
                predicate_lemma=_lemmatize_verb(c["verb"]),
                extraction_profile_id=_profile_id,
            )
            persisted += 1
        except Exception:
            log.exception("statement persist failed")
            skipped["error"] += 1

    return {"persisted": persisted, "skipped": skipped, "candidates": len(candidates)}


def _is_fragment_or_table(raw_text: str) -> bool:
    """Detect PDF-table / OCR / citation-fragment text that shouldn't form a
    statement. Filters at the raw_text level before we waste cycles resolving
    entities or calling the promoter on it.

    Examples this catches:
      - "Stone said."                       (length-based)
      - "Time 26-27"                        (citation OCR pattern)
      - "Stone, clay, glass, and concrete"  (CSV table row)
      - " — Fig. 34. "                       (figure caption)
    """
    if not raw_text:
        return True
    s = raw_text.strip()
    n = len(s)
    if n < 30:
        return True
    # Citation OCR like "Time 26-27" or "Smith 2020"
    import re as _re
    if _re.match(r'^\w+\s+\d+-?\d*$', s):
        return True
    # CSV-style table row: multiple comma-separated short words
    if n < 120 and _re.search(r',\s*[A-Z][a-z]+\s*,', s):
        commas = s.count(',')
        if commas >= 2:
            return True
    # Figure / table caption fragments
    if _re.match(r'^\s*(Fig\.|Figure|Table|Tab\.)\s*\d+', s, _re.IGNORECASE):
        return True
    return False


def extract_from_document(
    *, tenant_id: str = "default", document_id: int, llm: Any,
    skip_boilerplate: bool = True
) -> dict:
    """Extract statements from every paragraph in a document."""
    totals = {"paragraphs": 0, "statements_persisted": 0, "candidates": 0,
              "errors": 0}
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT dp.paragraph_id, dp.page_id, dp.raw_text, p.page_number
                  FROM document_paragraphs dp
                  LEFT JOIN document_pages p ON p.page_id = dp.page_id
                 WHERE dp.document_id = %s
                """ + (" AND COALESCE(dp.is_boilerplate, FALSE) = FALSE" if skip_boilerplate else "") + """
                 ORDER BY p.page_number NULLS LAST, dp.order_index
                """,
                (document_id,),
            )
            paragraphs = cur.fetchall()

    for paragraph_id, page_id, raw_text, _pg in paragraphs:
        if not raw_text or len(raw_text) < 40:
            continue
        totals["paragraphs"] += 1
        try:
            r = extract_from_paragraph(
                tenant_id=tenant_id,
                document_id=document_id, page_id=page_id,
                paragraph_id=paragraph_id, text=raw_text, llm=llm,
            )
            totals["statements_persisted"] += r["persisted"]
            totals["candidates"] += r["candidates"]
        except Exception:
            log.exception("paragraph %d failed", paragraph_id)
            totals["errors"] += 1
    return totals


def extract_from_corpus(
    dataset_id: str, *, llm: Any, tenant_id: str = "default",
    skip_boilerplate: bool = True, limit_docs: Optional[int] = None,
) -> dict:
    """Walk every document in a dataset and extract statements."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT document_id, title, page_count FROM documents
                 WHERE dataset_id = %s ORDER BY document_id
                """ + (f" LIMIT {limit_docs}" if limit_docs else ""),
                (dataset_id,),
            )
            docs = cur.fetchall()

    totals = {"dataset_id": dataset_id, "docs": len(docs),
              "statements_persisted": 0, "candidates": 0, "errors": 0,
              "per_doc": []}
    for doc_id, title, page_count in docs:
        log.info("[doc %d] %s — %d pages", doc_id, title, page_count or 0)
        try:
            r = extract_from_document(
                tenant_id=tenant_id, document_id=doc_id, llm=llm,
                skip_boilerplate=skip_boilerplate,
            )
            totals["statements_persisted"] += r["statements_persisted"]
            totals["candidates"]            += r["candidates"]
            totals["per_doc"].append({
                "doc_id": doc_id, "title": title,
                "persisted": r["statements_persisted"],
                "candidates": r["candidates"],
            })
        except Exception:
            log.exception("document %d failed", doc_id)
            totals["errors"] += 1
    return totals
