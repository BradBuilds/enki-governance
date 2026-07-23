"""
Batched LLM extraction — send N paragraphs per call, get back arrays
of mention/statement results.

This is the Phase 3B speed unlock. The previous per-paragraph pattern
had ~30s LLM latency per paragraph; for a 1500-paragraph PDF that's
12 hours of wall time. Batching 8-10 paragraphs per call drops it by
~5-8x without changing the model, the parsing logic, or the
persistence path.

Strategy:
  1. Walk document_paragraphs in chunks of N (default 8).
  2. Build a single prompt that asks the LLM to emit a JSON array
     where each element is the result for one paragraph in order.
  3. Parse the array; persist each paragraph's results normally.
  4. If the LLM returns the wrong cardinality, fall back to per-
     paragraph extraction for that batch (graceful degrade).

Provides batched mention + statement extraction with the same
public surface as the single-paragraph functions in
core.documents.extractor / statement_extractor.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional

from core.documents.mentions import persist_mention, normalize_surface
from core.documents.statement_extractor import (
    _persist_statement,
    _find_mention_for_surface,
    _parse_date_iso,
    _lemmatize_verb,
)
from storage.postgres.db import get_connection

log = logging.getLogger("enki.documents.batched_extractor")


_ACRONYM_RE = re.compile(r"^[A-Z]{2,6}$")


def plausible_entity_surface(surface: str) -> bool:
    """Is this LLM 'mention' a plausible named entity, or extraction junk?

    Even on clean prose the LLM emits lone initials ('H W', 'J E'), exhibit
    codes ('JE3', 'WB3', 'S-1'), and punctuation fragments ('B.M', '"on') as if
    they were entities. Keep real all-caps acronyms (FBI, CIA, IRS) and any
    surface carrying a genuine word token (≥3 letters, essentially alphabetic);
    reject the rest. Shared by the persistence gate and the cleanup pass so the
    live rule and the retro-clean rule can never drift.
    """
    s = (surface or "").strip()
    if sum(c.isalpha() for c in s) < 2:
        return False
    # Proper nouns are capitalized. A lowercase surface ("book", "door",
    # "food", "jets") is a common word the LLM mislabeled as an entity, not a
    # name. Require the first letter to be uppercase (acronyms are all-caps).
    first = next((c for c in s if c.isalpha()), "")
    if not first.isupper():
        return False
    if _ACRONYM_RE.match(s):                      # FBI, CIA, NASA — pure caps
        return True
    for tok in s.split():                          # needs one real word token
        a = sum(c.isalpha() for c in tok)
        if a >= 3 and a >= len(tok) - 1:
            return True
    return False


_BATCH_MENTION_PROMPT = """\
You are an entity-mention extractor for an intelligence system.

Below are {n} numbered paragraphs from a real document. For EACH
paragraph, return the proper-noun mentions exactly as written, with
character offsets within THAT paragraph.

RULES:
- Be conservative — only emit named entities (person, organization,
  place, vessel, aircraft, vehicle, facility, product).
- Pronouns are not mentions.
- Skip court header text, page numbers, "EXHIBIT NO.", "CASE NO.", etc.
- char_start/char_end must be valid offsets within the paragraph
  (verify substring matches).

Return ONE JSON object of the shape:
  {{"results": [
     {{"paragraph_index": 1, "mentions": [{{...}}, ...]}},
     {{"paragraph_index": 2, "mentions": [...]}},
     ...
  ]}}

Each mention is {{"surface_form": "...", "char_start": N, "char_end": N,
                  "suggested_type": "...", "confidence": 0.0..1.0}}.

You MUST return exactly {n} entries in `results`, in paragraph order.

PARAGRAPHS:
{paragraphs_block}
"""


_BATCH_STATEMENT_PROMPT = """\
You are a fact extractor for an intelligence system.

Below are {n} numbered paragraphs with their already-identified
proper-noun mentions. For EACH paragraph, return every subject-
predicate-object claim it makes.

RULES:
- Only emit claims EXPLICITLY in the paragraph.
- BOTH subject AND object are REQUIRED. Subject must reference one of
  the listed mentions. Object must reference a listed mention OR be a
  concrete value (a date, a place name, a dollar amount, an
  identifier). Drop the claim if you cannot produce an object.
- predicate_verb must be a BASE-FORM ACTION VERB describing what
  the subject DID (own, fly, meet, pay, communicate, travel, sign,
  arrest, attend, found, donate). One word, lowercase.
- FORBIDDEN predicates (do NOT emit these — skip the claim instead):
    be, is, are, was, were, been, being, am
    have, has, had
    do, does, did
    will, would, can, could, may, might, shall, should, must
    seem, appear, exist
  These are copulas/auxiliaries. "X is Y" is not a fact for this
  system — find a real action verb or skip the claim.
- date_iso = ISO 8601 if stated, else null.
- location_text = place name as text, else null.
- modality ∈ asserted/hedged/quoted/denied.
- polarity ∈ affirmative/negative.

Return ONE JSON object:
  {{"results": [
     {{"paragraph_index": 1, "statements": [{{...}}, ...]}},
     {{"paragraph_index": 2, "statements": [...]}},
     ...
  ]}}

Each statement is {{"subject":"...", "predicate_verb":"...",
                    "object":"...", "raw_text":"...",
                    "date_iso":null, "location_text":null,
                    "modality":"asserted", "polarity":"affirmative",
                    "confidence":0.0..1.0}}.

If a paragraph contains no extractable subject-action-object claim,
return an empty statements list for it.

You MUST return exactly {n} entries in `results`, in paragraph order.

PARAGRAPHS:
{paragraphs_block}
"""


_BATCH_PROPERTY_PROMPT = """\
You are a property extractor for an intelligence system.

Below are {n} numbered paragraphs with their already-identified entity
mentions. For EACH paragraph, extract PROPERTY ASSERTIONS — statements
that DESCRIBE what an entity IS (its rank, residence, occupation,
affiliation, color, identifier, etc.), distinct from what an entity DID
(which goes elsewhere as subject-verb-object statements).

CORE PROPERTY KEYS (use these literal strings):
  rank             Major, General, Lieutenant Colonel, Senator, President
  occupation       intelligence officer, sculptor, financier, journalist
  residence        Houma Louisiana, Harbor House, Springfield, Manhattan
  affiliation      509th Composite Bomb Group, USAF, First National Bank
  origin           born in, manufactured in, country of origin
  color            red, candy red, Pantone 199C, RGB(220,20,60)
  material         steel, aluminum, foil-like
  identifier       passport #, MMSI, CIK, registration tail number
  language         English, en-US, French
  status           alive, deceased, active, decommissioned, retired
  owned_by         (subject) is owned by (value entity)
  manufactured_by  (subject) was manufactured by (value entity)
  operated_by      (subject) is operated by (value entity)
  located_at       (subject) is at (place name)
  name             (subject) full name, formal style, e.g. "Nathan Farragut Twining"
  alias            (subject) also known as

PRECISION TIERS (be precise — this controls how the substrate merges values):
  named       common-language token ("red", "Major", "Texas")
  qualified   modified token ("candy red", "Major General", "Texas AG")
  formal      coded value with standard ("Pantone 199C", "RGB(220,20,60)",
              "ICAO N123AA", "CIK 0000037996", "ISO en-US")
  spectral    physical measurement ("33.6406°N", "λ=620nm", "75 kg ±0.5")
  enumerated  fixed closed list ("en-US" as BCP 47, "active" as enum)

RULES:
- Only emit properties EXPLICITLY in the paragraph. No inference.
- The entity_surface must be one of the listed mentions for that paragraph.
- Drop generic copula assertions ("X is good", "Y is important") —
  emit only if the VALUE is a specific descriptive token.
- NEVER emit a property where the value equals the entity surface
  ("Soviet" → affiliation → "Soviet" is forbidden; "Roswell" → residence
  → "Roswell" is forbidden). The value must be a DIFFERENT, meaningful
  descriptor than the entity's name.
- NEVER use 'name' as the property unless the value is a longer formal
  version of the entity's name (e.g. entity="Twining", value="Nathan
  Farragut Twining"). Do not just repeat the surface form back.
- Skip if the entity surface is a common noun ("office", "project",
  "report", "document", "system") — those aren't named entities; the
  upstream extractor's mistake, drop the assertion.
- If the entity is a TITLE word like "General" or "Major" appearing
  alone, skip — those are honorifics, not entities.
- Each entity-property pair should appear at most ONCE per paragraph.
- If the same property is asserted at two precision tiers in the same
  paragraph (e.g. "Major Marcel" and "Maj. Jesse A. Marcel"), emit both
  separately — the substrate's collapse operator handles the refinement.

Return ONE JSON object:
  {{"results": [
     {{"paragraph_index": 1, "properties": [{{...}}, ...]}},
     ...
  ]}}

Each property is {{"entity_surface": "...", "property_key": "...",
                   "value": "...", "precision_tier": "...",
                   "confidence": 0.0..1.0}}.

If a paragraph contains no extractable properties, return an empty
properties list.

You MUST return exactly {n} entries in `results`, in paragraph order.

PARAGRAPHS:
{paragraphs_block}
"""


# Copulas, auxiliaries, and modal verbs the LLM sometimes emits despite
# the prompt. We hard-drop statements with these as predicate_verb to
# guarantee the assessment promoter never sees "X is Y" style noise.
_FORBIDDEN_PREDICATES = frozenset({
    "be", "is", "are", "was", "were", "been", "being", "am",
    "have", "has", "had",
    "do", "does", "did",
    "will", "would", "can", "could", "may", "might",
    "shall", "should", "must", "ought",
    "seem", "seems", "seemed", "appear", "appears", "appeared",
    "exist", "exists", "existed",
    "say", "says", "said",  # too generic — reportive verbs, drop unless
                            # promoted to communicate/state via dictionary
    "no", "not",            # negators leaking as predicates
    "i", "we", "they",      # pronouns leaking from broken parses
})


_MAX_PARAGRAPH_CHARS_BATCH = 2000
_DEFAULT_BATCH_SIZE = 8


def _build_paragraphs_block(paragraphs: list[tuple[int, str]]) -> str:
    out_lines = []
    for idx, (_pid, text) in enumerate(paragraphs, start=1):
        trunc = (text or "")[:_MAX_PARAGRAPH_CHARS_BATCH]
        out_lines.append(f"--- PARAGRAPH {idx} ---\n{trunc}\n")
    return "\n".join(out_lines)


def _build_paragraphs_block_with_mentions(
    paragraphs: list[tuple[int, str, list[dict]]],
) -> str:
    out_lines = []
    for idx, (_pid, text, mentions) in enumerate(paragraphs, start=1):
        trunc = (text or "")[:_MAX_PARAGRAPH_CHARS_BATCH]
        mentions_block = "(no mentions)"
        if mentions:
            mentions_block = "; ".join(
                f"{m['surface_form']} ({m.get('suggested_type','unknown')})"
                for m in mentions[:20]
            )
        out_lines.append(f"--- PARAGRAPH {idx} ---\n"
                          f"mentions: {mentions_block}\n"
                          f"text: {trunc}\n")
    return "\n".join(out_lines)


def _call_llm_for_json(llm: Any, prompt: str, max_tokens: int = 4000) -> Optional[dict]:
    """Send prompt; parse JSON; return dict or None."""
    try:
        from core.ai.provider import LLMMessage
        messages = [
            LLMMessage(role="system",
                       content="You return only valid JSON. No commentary, no markdown fences."),
            LLMMessage(role="user", content=prompt),
        ]
        response = llm.complete(messages, max_tokens=max_tokens, temperature=0.0)
    except Exception as e:
        log.debug("LLM call failed: %s", e)
        return None
    if response is None or getattr(response, "error", None):
        return None
    raw = (response.content or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    if raw.startswith("json"):
        raw = raw[4:].lstrip()
    try:
        return json.loads(raw)
    except Exception:
        try:
            s, e = raw.index("{"), raw.rindex("}") + 1
            return json.loads(raw[s:e])
        except Exception:
            return None


# ─── Batched mention extraction ───────────────────────────────────────

def extract_mentions_batched(
    *, tenant_id: str = "default",
    document_id: int,
    llm: Any,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    skip_boilerplate: bool = True,
) -> dict:
    """Run mention extraction over a document in batches. Returns stats."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            sql = """
                SELECT paragraph_id, page_id, raw_text
                  FROM document_paragraphs
                 WHERE document_id = %s
                """
            if skip_boilerplate:
                sql += " AND COALESCE(is_boilerplate, FALSE) = FALSE"
            # Only extract entity mentions from prose paragraphs. Records,
            # bibliography, headers, captions, fragments, index entries all
            # get routed to specialized extractors elsewhere — sending them
            # to the prose LLM wastes Gemma cycles and produces noise.
            sql += " AND paragraph_type = 'prose'"
            # Quality gate: never hand garbage/low-confidence OCR text to the
            # LLM. It will dutifully label OCR fragments ("U W", "J r", "K U")
            # as people/orgs, flooding the graph with junk entities. Only real
            # prose (quality_band pass/record) becomes entities; the flag/drop
            # bands stay raw searchable evidence. Unflagged docs default to pass.
            sql += " AND COALESCE(metadata->>'quality_band', 'pass') IN ('pass', 'record')"
            sql += " ORDER BY page_id NULLS LAST, order_index"
            cur.execute(sql, (document_id,))
            rows = [(r[0], r[1], r[2]) for r in cur.fetchall() if r[2] and len(r[2]) >= 40]

    stats = {"paragraphs": len(rows), "mentions_persisted": 0,
             "batches": 0, "batch_failures": 0, "llm_calls": 0,
             "started_at": time.time(),
             "filter": "paragraph_type=prose"}

    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        para_pairs = [(pid, text) for pid, _page, text in batch]
        block = _build_paragraphs_block(para_pairs)
        prompt = (_BATCH_MENTION_PROMPT
                  .replace("{n}", str(len(batch)))
                  .replace("{paragraphs_block}", block))
        stats["batches"] += 1
        stats["llm_calls"] += 1
        parsed = _call_llm_for_json(llm, prompt, max_tokens=4000)
        if not parsed or "results" not in parsed:
            stats["batch_failures"] += 1
            log.warning("batch %d failed; skipping", stats["batches"])
            continue
        results = parsed["results"]
        if not isinstance(results, list):
            stats["batch_failures"] += 1
            continue

        for ridx, result in enumerate(results):
            if ridx >= len(batch):
                break
            paragraph_id, page_id, raw_text = batch[ridx]
            mentions = result.get("mentions") or []
            if not isinstance(mentions, list):
                continue
            for m in mentions:
                if not isinstance(m, dict):
                    continue
                surface = (m.get("surface_form") or "").strip()
                if not surface or len(surface) < 2:
                    continue
                if not plausible_entity_surface(surface):   # drop initials/codes/fragments
                    continue
                cs = m.get("char_start"); ce = m.get("char_end")
                if not (isinstance(cs, int) and isinstance(ce, int) and 0 <= cs < ce):
                    # fall back: locate substring
                    idx = (raw_text or "").find(surface)
                    if idx < 0:
                        continue
                    cs, ce = idx, idx + len(surface)
                try:
                    persist_mention(
                        tenant_id=tenant_id, document_id=document_id,
                        page_id=page_id, paragraph_id=paragraph_id,
                        surface_form=surface,
                        char_start=cs, char_end=ce,
                        suggested_type=(m.get("suggested_type") or "unknown").lower(),
                        confidence=float(m.get("confidence") or 0.6),
                        extraction_method="llm_batched_v1",
                    )
                    stats["mentions_persisted"] += 1
                except Exception:
                    log.exception("persist failed for paragraph %d", paragraph_id)
    stats["elapsed_s"] = round(time.time() - stats.pop("started_at"), 1)
    return stats


# ─── Batched statement extraction ─────────────────────────────────────

_SENT_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


def _canonical_span(para_text: str, subject: str, obj: str, llm_quote: str) -> str:
    """Deterministic source span for a statement (Fix 1: kills quote-boundary
    variance). Instead of the LLM's hand-picked quote (which wobbles run-to-run
    for the same fact), anchor to the SENTENCE in the paragraph that contains the
    statement's subject (and object). Same claim → same sentence → reproducible
    raw_text. Fallbacks: max-overlap sentence vs the LLM quote, then the paragraph."""
    if not para_text:
        return (llm_quote or "")[:2000]
    sents = [s.strip() for s in _SENT_BOUNDARY.split(para_text) if s.strip()]
    if not sents:
        return para_text[:2000]
    subj_l, obj_l = (subject or "").lower(), (obj or "").lower()
    if subj_l and obj_l:
        for s in sents:
            sl = s.lower()
            if subj_l in sl and obj_l in sl:
                return s[:2000]
    if subj_l:
        for s in sents:
            if subj_l in s.lower():
                return s[:2000]
    if llm_quote:
        q = set(re.findall(r"\w+", llm_quote.lower()))
        if q:
            return max(sents, key=lambda s: len(q & set(re.findall(r"\w+", s.lower()))))[:2000]
    return para_text[:2000]


def _call_statement_batch(batch, mentions_by_paragraph, llm, retries: int = 2):
    """LLM statement call for a batch, with retry (Fix 2: kills dropped batches
    from transient LLM/parse failures) WITHOUT changing the batch composition —
    retrying the SAME batch keeps the LLM's context constant, so the output stays
    deterministic run-to-run. Returns the results list or None."""
    triples = [(pid, text, mentions_by_paragraph.get(pid, []))
               for pid, _page, text in batch]
    block = _build_paragraphs_block_with_mentions(triples)
    prompt = (_BATCH_STATEMENT_PROMPT
              .replace("{n}", str(len(batch)))
              .replace("{paragraphs_block}", block))
    # 8000 (not 4000): an 8-paragraph batch's combined statement output can overflow
    # 4000 tokens → truncated JSON → the whole batch fails and its statements were
    # dropped (the coverage loss). More room lets the whole batch succeed without
    # changing batch composition, so determinism is preserved and coverage recovered.
    for _ in range(retries + 1):
        parsed = _call_llm_for_json(llm, prompt, max_tokens=8000)
        if parsed and isinstance(parsed.get("results"), list):
            return parsed["results"]
    return None


def _persist_statements_from_results(results, batch, document_id, tenant_id, stats):
    """Persist one batch's parsed results. raw_text is the CANONICAL source span
    (Fix 1), not the LLM's quote."""
    for ridx, result in enumerate(results):
        if ridx >= len(batch):
            break
        paragraph_id, page_id, para_text = batch[ridx]
        stmts = result.get("statements") or []
        if not isinstance(stmts, list):
            continue
        for s in stmts:
            if not isinstance(s, dict):
                continue
            subject = (s.get("subject") or "").strip()
            verb = (s.get("predicate_verb") or "").strip().lower()
            obj = str(s.get("object") or "").strip()
            if not subject or not verb:
                continue
            if verb in _FORBIDDEN_PREDICATES:
                stats["dropped_copula"] = stats.get("dropped_copula", 0) + 1
                continue
            if not obj:
                stats["dropped_no_object"] = stats.get("dropped_no_object", 0) + 1
                continue
            subj_ref = _find_mention_for_surface(paragraph_id, subject)
            if not subj_ref:
                continue
            obj_ref = _find_mention_for_surface(paragraph_id, obj) if obj else None
            obj_mention_id, obj_entity_id = (obj_ref or (None, None))
            has_date = bool(s.get("date_iso"))
            has_loc = bool(s.get("location_text"))
            if obj_mention_id is None and not has_date and not has_loc:
                stats["dropped_object_unresolved"] = stats.get("dropped_object_unresolved", 0) + 1
                continue
            try:
                _persist_statement(
                    tenant_id=tenant_id, document_id=document_id,
                    page_id=page_id, paragraph_id=paragraph_id,
                    speaker_mention_id=subj_ref[0], speaker_entity_id=subj_ref[1],
                    predicate=verb,
                    object_mention_id=obj_mention_id, object_entity_id=obj_entity_id,
                    raw_text=_canonical_span(para_text, subject, obj, s.get("raw_text")),
                    event_time=_parse_date_iso(s.get("date_iso")),
                    location_text=s.get("location_text"),
                    modality=s.get("modality") or "asserted",
                    polarity=s.get("polarity") or "affirmative",
                    confidence=float(s.get("confidence") or 0.6),
                    predicate_verb_surface=verb,
                    predicate_lemma=_lemmatize_verb(verb),
                )
                stats["statements_persisted"] += 1
            except Exception:
                log.exception("persist failed for paragraph %d", paragraph_id)


def extract_statements_batched(
    *, tenant_id: str = "default",
    document_id: int,
    llm: Any,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    skip_boilerplate: bool = True,
) -> dict:
    """Run statement extraction in batches. Requires mentions to be in
    document_mentions already (extract_mentions_batched first)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            sql = """
                SELECT dp.paragraph_id, dp.page_id, dp.raw_text
                  FROM document_paragraphs dp
                 WHERE dp.document_id = %s
                """
            if skip_boilerplate:
                sql += " AND COALESCE(dp.is_boilerplate, FALSE) = FALSE"
            # Prose only — records/bibliography/headers/etc. are not S-P-O
            # claims and the LLM produces noise on them.
            sql += " AND dp.paragraph_type = 'prose'"
            # Quality gate (see extract_mentions_batched): garbage OCR text
            # yields garbage statements. Only real prose is claim-bearing.
            sql += " AND COALESCE(dp.metadata->>'quality_band', 'pass') IN ('pass', 'record')"
            sql += " ORDER BY dp.page_id NULLS LAST, dp.order_index"
            cur.execute(sql, (document_id,))
            from core.documents.text_quality import chunk_quality
            paragraphs = []
            skipped_garbage = 0
            for r in cur.fetchall():
                pid, page, txt = r[0], r[1], r[2]
                if not txt or len(txt) < 40:
                    continue
                # Fix 3: garbage gate — skip OCR-mangled / non-sentence paragraphs
                # so statements aren't built on noise (and don't vary run-to-run).
                try:
                    if not chunk_quality(cur, txt).get("is_real_sentence", True):
                        skipped_garbage += 1
                        continue
                except Exception:
                    pass
                paragraphs.append((pid, page, txt))

            # Bulk-fetch mentions for all paragraphs
            cur.execute(
                """
                SELECT paragraph_id, surface_form, suggested_type
                  FROM document_mentions
                 WHERE document_id = %s
                """,
                (document_id,),
            )
            mentions_by_paragraph: dict[int, list[dict]] = {}
            for pid, surface, stype in cur.fetchall():
                mentions_by_paragraph.setdefault(pid, []).append(
                    {"surface_form": surface, "suggested_type": stype}
                )

    stats = {"paragraphs": len(paragraphs), "statements_persisted": 0,
             "batches": 0, "batch_failures": 0, "llm_calls": 0,
             "started_at": time.time()}

    for i in range(0, len(paragraphs), batch_size):
        batch = paragraphs[i:i + batch_size]
        stats["batches"] += 1
        stats["llm_calls"] += 1
        results = _call_statement_batch(batch, mentions_by_paragraph, llm)
        if results is not None:
            _persist_statements_from_results(results, batch, document_id, tenant_id, stats)
        else:
            # Do NOT split on failure: splitting changes the LLM's context
            # (whole-batch vs per-paragraph) and, because failures are transient,
            # that made statements non-deterministic run-to-run. Keep the batch
            # composition CONSTANT and rely on the in-call retries to ride out
            # transient failures. A batch that persistently fails drops
            # identically in every run → no divergence.
            stats["batch_failures"] += 1
    stats["skipped_garbage"] = skipped_garbage
    stats["elapsed_s"] = round(time.time() - stats.pop("started_at"), 1)
    return stats


# Valid property keys — match propertyvocab.properties
_VALID_PROPERTY_KEYS = frozenset({
    "name", "identifier", "alias", "color", "material", "shape", "size",
    "weight", "length", "location", "origin", "destination", "residence",
    "time", "start_time", "end_time", "owned_by", "manufactured_by",
    "operated_by", "occupation", "rank", "affiliation", "status",
    "polarity", "certainty", "count", "currency_amount", "language",
    "source_kind", "category", "located_at",  # located_at maps to location
})

# Valid precision tiers — must match precisionvocab.tiers
_VALID_PRECISION_TIERS = frozenset({
    "named", "qualified", "formal", "spectral", "enumerated",
})


def extract_properties_batched(
    *, tenant_id: str = "default",
    document_id: int,
    llm: Any,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    skip_boilerplate: bool = True,
) -> dict:
    """Run property extraction in batches. Gemma tags each property with
    a precision tier; the collapse operator handles refinement/conflict.

    Requires mentions to already be resolved (extract_mentions_batched
    must have run + entity resolution must have linked surface forms to
    canonical entity_ids).
    """
    from core.substrate.collapse import admit_assertion
    from core.substrate.properties import PropertyTypeError

    with get_connection() as conn:
        with conn.cursor() as cur:
            sql = """
                SELECT dp.paragraph_id, dp.page_id, dp.raw_text
                  FROM document_paragraphs dp
                 WHERE dp.document_id = %s
                """
            if skip_boilerplate:
                sql += " AND COALESCE(dp.is_boilerplate, FALSE) = FALSE"
            sql += " AND dp.paragraph_type = 'prose'"
            # Quality gate (see extract_mentions_batched): no properties from
            # garbage OCR text.
            sql += " AND COALESCE(dp.metadata->>'quality_band', 'pass') IN ('pass', 'record')"
            sql += " ORDER BY dp.page_id NULLS LAST, dp.order_index"
            cur.execute(sql, (document_id,))
            paragraphs = [(r[0], r[1], r[2]) for r in cur.fetchall()
                          if r[2] and len(r[2]) >= 40]

            # Mentions: surface_form → (mention_id, entity_id) per paragraph
            cur.execute(
                """
                SELECT paragraph_id, surface_form, suggested_type, entity_id
                  FROM document_mentions
                 WHERE document_id = %s
                """,
                (document_id,),
            )
            surface_to_entity: dict[tuple[int, str], int] = {}
            mentions_by_paragraph: dict[int, list[dict]] = {}
            for pid, surface, stype, eid in cur.fetchall():
                mentions_by_paragraph.setdefault(pid, []).append(
                    {"surface_form": surface, "suggested_type": stype}
                )
                if surface and eid:
                    surface_to_entity[(pid, surface)] = eid

    stats = {"paragraphs": len(paragraphs), "llm_calls": 0,
             "batches": 0, "batch_failures": 0,
             "props_emitted": 0, "props_admitted": 0,
             "props_dropped_unknown_property": 0,
             "props_dropped_unknown_precision": 0,
             "props_dropped_entity_unresolved": 0,
             "props_dropped_typed_error": 0,
             "collapse_actions": {},
             "started_at": time.time()}

    for i in range(0, len(paragraphs), batch_size):
        batch = paragraphs[i:i + batch_size]
        triples = [(pid, text, mentions_by_paragraph.get(pid, []))
                   for pid, _page, text in batch]
        block = _build_paragraphs_block_with_mentions(triples)
        prompt = (_BATCH_PROPERTY_PROMPT
                  .replace("{n}", str(len(batch)))
                  .replace("{paragraphs_block}", block))
        stats["batches"] += 1
        stats["llm_calls"] += 1
        parsed = _call_llm_for_json(llm, prompt, max_tokens=4000)
        if not parsed or "results" not in parsed:
            stats["batch_failures"] += 1
            continue
        results = parsed["results"]
        if not isinstance(results, list):
            stats["batch_failures"] += 1
            continue

        for ridx, result in enumerate(results):
            if ridx >= len(batch):
                break
            paragraph_id, _page_id, _raw_text = batch[ridx]
            props = result.get("properties") or []
            if not isinstance(props, list):
                continue
            for p in props:
                if not isinstance(p, dict):
                    continue
                stats["props_emitted"] += 1
                surface = (p.get("entity_surface") or "").strip()
                pkey = (p.get("property_key") or "").strip().lower()
                value = (p.get("value") or "").strip()
                tier = (p.get("precision_tier") or "").strip().lower()
                conf = float(p.get("confidence") or 0.7)

                # Normalize aliases: 'located_at' is the edge term;
                # the property concept is 'location'.
                if pkey == "located_at":
                    pkey = "location"

                if pkey not in _VALID_PROPERTY_KEYS:
                    stats["props_dropped_unknown_property"] += 1
                    continue
                if tier not in _VALID_PRECISION_TIERS:
                    stats["props_dropped_unknown_precision"] += 1
                    continue
                if not surface or not value:
                    continue
                # Defense-in-depth against Gemma's circular-assertion habit:
                # if the value is the same string as the surface form (or a
                # case-only variant), this is just the LLM repeating the
                # entity name back. Drop.
                if value.strip().lower() == surface.strip().lower():
                    stats.setdefault("props_dropped_circular", 0)
                    stats["props_dropped_circular"] = stats.get("props_dropped_circular", 0) + 1
                    continue
                # Block common-noun "entities" that the upstream mention
                # extractor over-emitted. These are never proper nouns.
                if surface.lower() in {
                    "office", "project", "report", "document", "system",
                    "agency", "company", "general", "major", "colonel",
                    "captain", "secretary", "president", "director",
                    "the", "this", "that",
                }:
                    stats.setdefault("props_dropped_common_noun", 0)
                    stats["props_dropped_common_noun"] += 1
                    continue
                entity_id = surface_to_entity.get((paragraph_id, surface))
                if not entity_id:
                    # Try a lowercase-normalized lookup before giving up
                    for (pid, sf), eid in surface_to_entity.items():
                        if pid == paragraph_id and sf.lower() == surface.lower():
                            entity_id = eid
                            break
                if not entity_id:
                    stats["props_dropped_entity_unresolved"] += 1
                    continue

                try:
                    res = admit_assertion(
                        subj_entity_id=entity_id,
                        property_key=pkey,
                        value_label=value[:250],
                        precision_tier=tier,
                        confidence=conf,
                        tenant_id=tenant_id,
                    )
                    stats["props_admitted"] += 1
                    action = res.action
                    stats["collapse_actions"][action] = (
                        stats["collapse_actions"].get(action, 0) + 1
                    )
                except PropertyTypeError as e:
                    stats["props_dropped_typed_error"] += 1
                    log.debug("property type-error %s.%s: %s", surface, pkey, e)
                except Exception:
                    log.exception("admit_assertion failed for paragraph %d, "
                                  "entity %d, prop %s",
                                  paragraph_id, entity_id, pkey)

    stats["elapsed_s"] = round(time.time() - stats.pop("started_at"), 1)
    return stats


if __name__ == "__main__":
    import argparse, sys
    sys.path.insert(0, "/app")
    from core.ai.config import get_tenant_provider
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("document_id", type=int)
    p.add_argument("--mode", default="mentions", choices=["mentions", "statements", "both"])
    p.add_argument("--batch-size", type=int, default=_DEFAULT_BATCH_SIZE)
    args = p.parse_args()
    llm = get_tenant_provider("default")
    if args.mode in ("mentions", "both"):
        print(json.dumps(extract_mentions_batched(
            document_id=args.document_id, llm=llm,
            batch_size=args.batch_size), indent=2))
    if args.mode in ("statements", "both"):
        print(json.dumps(extract_statements_batched(
            document_id=args.document_id, llm=llm,
            batch_size=args.batch_size), indent=2))
