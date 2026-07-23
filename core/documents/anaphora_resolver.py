"""
Anaphora resolver — learnable placeholder → entity binding by document context.

Reads:  statements + entity_mentions + entities + anaphora_placeholders
Writes: context_bindings + context_binding_applications

The contract:

  For each assessment whose subject or object resolves to a placeholder
  entity ("House", "Ranch", "the island", ...), find the specific entity
  that placeholder refers to *in the document the underlying statement
  came from*. If there's exactly ONE strong candidate in the document of
  the right type, write a binding. If there are 0 or >1, do nothing — high
  precision over high recall.

Read at display time via lookup_binding() — the runtime resolver in
cards_api applies them to timeline responses.

Public surface:

  scan_library(library_id) -> dict
      One-shot: for every assessment in a library, try to bind any
      placeholder subject or object. Returns counts. Safe to re-run.

  lookup_binding(placeholder, document_id) -> (entity_id, confidence, binding_id) | None
      The runtime query.

  record_application(binding_id, assessment_id, statement_id) -> None
      Audit entry every time a binding gets surfaced.
"""

from __future__ import annotations

import logging
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.documents.anaphora_resolver")


# ─── LLM validation gate ─────────────────────────────────────────────────
#
# Before writing a candidate binding to context_bindings, we can optionally
# ask Gemma to confirm or reject it. The validator sees:
#   - the placeholder string ("the house", "Al")
#   - the source paragraph text (a few examples from the doc)
#   - the candidate resolution name + type
# Gemma returns a single token: YES, NO, or UNCERTAIN.
# YES → write at confidence floor 0.85; NO → reject; UNCERTAIN → still
# write at lower confidence so a human can review.
#
# Disabled when Ollama is unreachable — the resolver falls back to the
# heuristic-only path.

_LLM_VALIDATOR_PROMPT = """You are validating an anaphora resolution.

A document contains the phrase {placeholder!r}.
A heuristic suggests this refers to: {candidate_name!r} ({candidate_type}).

Source sentences from the same document:
{sample_sentences}

In the context of these sentences, is {placeholder!r} most likely referring \
to {candidate_name!r}?

Answer with exactly one token:
  YES        — the resolution is correct
  NO         — the resolution is wrong; reject the binding
  UNCERTAIN  — cannot tell from this context

One token only. No explanation.
"""


def _llm_validate_binding(
    conn,
    placeholder: str,
    candidate_name: str,
    candidate_type: str,
    document_id: int,
) -> tuple[str, float]:
    """Ask Gemma to validate the binding. Returns (decision, confidence_adj).

    decision is 'yes' | 'no' | 'uncertain' | 'unavailable'.
    On unavailable, callers should fall back to heuristic confidence.
    """
    try:
        from core.ai.local_provider import OllamaProvider
        from core.ai.provider import LLMMessage
    except Exception as e:
        log.debug("LLM imports failed: %s", e)
        return ("unavailable", 0.0)

    # Pull up to 3 sentences from this document that mention the placeholder
    # OR a partial match. These give the model the context it needs.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT raw_text FROM statements
             WHERE document_id = %s
               AND (
                 raw_text ILIKE %s
                 OR raw_text ILIKE %s
                 OR raw_text ILIKE %s
               )
               AND raw_text IS NOT NULL
             ORDER BY statement_id
             LIMIT 3
            """,
            (
                document_id,
                f"%{placeholder}%",
                f"%{candidate_name}%",
                f"%{candidate_name.split()[0]}%" if " " in candidate_name else f"%{candidate_name}%",
            ),
        )
        samples = [r[0] for r in cur.fetchall() if r[0]]
    if not samples:
        # No context to validate against — let heuristic stand.
        return ("uncertain", 0.0)

    prompt = _LLM_VALIDATOR_PROMPT.format(
        placeholder=placeholder,
        candidate_name=candidate_name,
        candidate_type=candidate_type or "entity",
        sample_sentences="\n".join(f"  - {s[:240]}" for s in samples),
    )

    try:
        provider = OllamaProvider({"model": "gemma4:e2b"})
        if not provider.is_available():
            return ("unavailable", 0.0)
        # Gemma4 reasoning models need budget for hidden thinking tokens
        # before they reach the visible reply. <128 tokens often returns
        # empty content because budget exhausts mid-thought.
        resp = provider.complete(
            messages=[LLMMessage(role="user", content=prompt)],
            max_tokens=256,
            temperature=0.0,
        )
        if resp.error or not resp.content:
            return ("unavailable", 0.0)
        content_upper = resp.content.strip().upper()
        # Be permissive: only reject on an explicit, unambiguous NO at the
        # start of the response. If the model rambles, hedges, or says YES
        # somewhere — keep the heuristic binding.
        first_token = content_upper.split()[0].rstrip(".,;:!?") if content_upper else ""
        if first_token == "NO" or content_upper.startswith("NO,") or content_upper.startswith("NO."):
            return ("no", -1.0)
        if first_token == "YES" or "YES" in content_upper[:30]:
            return ("yes", +0.05)
        return ("uncertain", 0.0)
    except Exception as e:
        log.debug("LLM validator failed: %s", e)
        return ("unavailable", 0.0)


# Names whose canonical_name looks like a placeholder. We don't trust the
# extraction-time string; we re-check against the canonical placeholders
# table at scan time.
def _load_placeholder_set(conn) -> dict[str, tuple[str | None, str | None]]:
    """Return {placeholder: (expected_type, notes)}.

    `notes` can carry routing hints. The format "alias_bridge:<FullForm>"
    tells the resolver to use partial-name resolution: e.g. "al" with
    note "alias_bridge:Alex" tries to find "Alex *" entities in the
    same document.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT placeholder, expected_type, notes FROM anaphora_placeholders")
        return {p.lower(): (t, n) for p, t, n in cur.fetchall()}


def _alias_bridge_prefix(notes: str | None) -> str | None:
    """Extract the full-form prefix from an `alias_bridge:<Prefix>` note."""
    if not notes or not notes.startswith("alias_bridge:"):
        return None
    return notes.split(":", 1)[1].strip() or None


def _is_placeholder_name(
    name: str | None, placeholders: dict[str, tuple[str | None, str | None]]
) -> tuple[bool, str | None, str | None]:
    """Return (is_placeholder, expected_type, notes) for a canonical_name."""
    if not name:
        return False, None, None
    key = name.strip().lower()
    if key in placeholders:
        et, nt = placeholders[key]
        return True, et, nt
    return False, None, None


def _looks_like_partial_person_name(name: str) -> bool:
    """Is this a person name short enough to plausibly be an alias for a
    fuller form? 1-2 tokens, alpha-ish, not an honorific."""
    if not name:
        return False
    tokens = name.replace(".", "").split()
    if not tokens or len(tokens) > 2:
        return False
    if name.strip().lower() in {"mr", "ms", "mrs", "dr", "mr.", "ms.", "mrs.", "dr."}:
        return False
    # All-letters-ish (allow apostrophes, hyphens)
    cleaned = name.replace(".", "").replace("-", "").replace("'", "").replace(" ", "")
    if not cleaned or not cleaned.replace(" ", "").isalpha():
        return False
    return True


def _find_auto_alias_candidates(
    conn, document_id: int, partial_name: str, exclude_id: int | None
) -> list[tuple[int, str, str, int]]:
    """Generalized alias-bridge: given a 1-2-token person name (e.g. "J Doe"
    or "Doe"), find person entities in the same document whose canonical
    name CONTAINS every token of the partial as a whole word AND has more
    tokens (i.e. is a fuller form).

    Examples (all in the same document):
      "J Doe"   → "Jane Doe"  (both tokens match; fuller name)
      "Al"          → "Alex Acme"    (token match via stem in alias_bridge)
      "Doe"     → "Jane Doe"  (single shared token; fuller name)
    """
    tokens = [t.strip(".,") for t in partial_name.split() if t.strip(".,")]
    if not tokens:
        return []
    # Build a regex that requires each token as a word-bounded match
    # (case-insensitive) AND requires the candidate to have a space (fuller form).
    # We do the token checks in Python after fetching all person entities in doc.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.entity_id, e.canonical_name, e.canonical_type, COUNT(*) AS hits
              FROM (
                SELECT speaker_entity_id AS eid FROM statements WHERE document_id = %s AND speaker_entity_id IS NOT NULL
                UNION ALL
                SELECT object_entity_id  AS eid FROM statements WHERE document_id = %s AND object_entity_id  IS NOT NULL
              ) m
              JOIN entities e ON e.entity_id = m.eid
             WHERE e.canonical_type = 'person'
               AND e.entity_id <> COALESCE(%s, -1)
               AND POSITION(' ' IN e.canonical_name) > 0
             GROUP BY e.entity_id, e.canonical_name, e.canonical_type
            """,
            (document_id, document_id, exclude_id),
        )
        rows = cur.fetchall()
    # Filter to those that contain every token from partial_name as a token
    # OR as an initial match (e.g. "G" matches "Jane").
    keep: list[tuple[int, str, str, int]] = []
    partial_lower_tokens = [t.lower() for t in tokens]
    for eid, ename, etype, hits in rows:
        ename_tokens = [t.strip(".,").lower() for t in ename.split() if t.strip(".,")]
        if not ename_tokens or len(ename_tokens) <= len(tokens):
            continue
        # Each partial token must match an ename token by either:
        #   - equality (Doe == Doe)
        #   - initial: partial token is 1 char AND it's the first letter of some ename token
        match_all = True
        for pt in partial_lower_tokens:
            matched = False
            for et in ename_tokens:
                if pt == et:
                    matched = True
                    break
                if len(pt) == 1 and et.startswith(pt):
                    matched = True
                    break
            if not matched:
                match_all = False
                break
        if match_all:
            keep.append((eid, ename, etype, hits))
    # Sort by hits desc, then prefer fuller names (more tokens)
    keep.sort(key=lambda x: (-x[3], -len(x[1].split())))
    return keep


def _find_alias_bridge_candidates(
    conn, document_id: int, name_prefix: str
) -> list[tuple[int, str, str, int]]:
    """Return person entities mentioned in this document whose canonical_name
    starts with `name_prefix ` (e.g. 'Alex '). Sorted by doc mention
    count. Used for short-name → full-name resolution."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.entity_id, e.canonical_name, e.canonical_type, COUNT(*) AS hits
              FROM (
                SELECT speaker_entity_id AS eid FROM statements WHERE document_id = %s AND speaker_entity_id IS NOT NULL
                UNION ALL
                SELECT object_entity_id  AS eid FROM statements WHERE document_id = %s AND object_entity_id  IS NOT NULL
              ) m
              JOIN entities e ON e.entity_id = m.eid
             WHERE e.canonical_name ILIKE %s
               AND e.canonical_type = 'person'
             GROUP BY e.entity_id, e.canonical_name, e.canonical_type
             ORDER BY hits DESC, e.entity_id
            """,
            (document_id, document_id, name_prefix + " %"),
        )
        return cur.fetchall()


def _find_document_candidates(
    conn, document_id: int, expected_type: str | None, exclude_id: int | None
) -> list[tuple[int, str, str, int]]:
    """Return entities of expected_type that are mentioned in any statement
    within this document, ranked by mention frequency. Tuple is
    (entity_id, canonical_name, canonical_type, mention_count)."""
    with conn.cursor() as cur:
        # Pull all entity_ids that show up as subj or obj on any statement in this doc
        if expected_type:
            # expected_type is the placeholder's category (facility, person, ...)
            # We accept any entity whose canonical_type matches OR is a strict
            # sub-classification (address → facility, organization → person, etc.
            # is NOT included here — be conservative).
            type_filter = "AND e.canonical_type = ANY(%s)"
            type_args: list = [_expand_type(expected_type)]
        else:
            type_filter = ""
            type_args = []
        params: list = [document_id, document_id]
        params.extend(type_args)
        if exclude_id is not None:
            exclude_clause = "AND e.entity_id <> %s"
            params.append(exclude_id)
        else:
            exclude_clause = ""
        cur.execute(
            f"""
            SELECT e.entity_id, e.canonical_name, e.canonical_type, COUNT(*) AS hits
              FROM (
                SELECT speaker_entity_id AS eid FROM statements WHERE document_id = %s AND speaker_entity_id IS NOT NULL
                UNION ALL
                SELECT object_entity_id  AS eid FROM statements WHERE document_id = %s AND object_entity_id  IS NOT NULL
              ) m
              JOIN entities e ON e.entity_id = m.eid
             WHERE TRUE {type_filter} {exclude_clause}
             GROUP BY e.entity_id, e.canonical_name, e.canonical_type
             ORDER BY hits DESC, e.entity_id
            """,
            params,
        )
        return cur.fetchall()


_TYPE_GROUPS = {
    "facility": ["facility", "facility_location", "address", "building", "compound", "residence"],
    "person":   ["person"],
    "organization": ["organization", "company", "government_agency"],
    "location": ["facility", "address", "location", "city", "region", "country", "place"],
}


def _expand_type(expected_type: str) -> list[str]:
    """Map an anaphora 'expected_type' to the entity canonical_type group."""
    return _TYPE_GROUPS.get(expected_type, [expected_type])


def _upsert_binding(
    conn,
    placeholder: str,
    document_id: int,
    entity_id: int,
    learned_from: str,
    confidence: float,
) -> int:
    """Write or strengthen a binding. Returns binding_id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO context_bindings
                (placeholder, context_scope, context_value, resolved_entity_id,
                 learned_from, confidence, evidence_count, created_by)
            VALUES (%s, 'document', %s, %s, %s, %s, 1, 'anaphora_resolver')
            ON CONFLICT (tenant_id, placeholder, context_scope, context_value)
            DO UPDATE
              SET resolved_entity_id = EXCLUDED.resolved_entity_id,
                  confidence = GREATEST(context_bindings.confidence, EXCLUDED.confidence),
                  evidence_count = context_bindings.evidence_count + 1,
                  updated_at = NOW()
            RETURNING binding_id
            """,
            (placeholder, str(document_id), entity_id, learned_from, confidence),
        )
        return cur.fetchone()[0]


def scan_library(library_id: int, llm_validate: bool = True) -> dict:
    """For every assessment whose subject or object is a placeholder
    entity, look at each source statement's document, find the unique
    candidate of the right type within that doc, and write a binding.

    Two-tier process:
      1. heuristic: doc-singleton OR alias-bridge picks a candidate
      2. (optional) LLM validation: Gemma confirms before write

    Idempotent — re-running strengthens existing bindings without
    duplicating rows. If `llm_validate=False`, skips the LLM step (useful
    when Ollama is down or for fast smoke tests).
    """
    bound = 0
    skipped_no_candidate = 0
    skipped_multi_candidate = 0
    skipped_no_doc = 0
    llm_rejected = 0
    llm_unavailable = 0
    llm_confirmed = 0

    with get_connection() as conn:
        placeholders = _load_placeholder_set(conn)
        if not placeholders:
            return {"library_id": library_id, "ok": False, "reason": "no placeholders configured"}

        # Pull every assessment touching an entity in this library, plus
        # the source statements (we need document_id).
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH lib_entities AS (
                  SELECT member_id AS entity_id
                    FROM library_members
                   WHERE library_id = %s AND member_kind = 'entity'
                )
                SELECT a.assessment_id,
                       a.subj_entity_id, ses.canonical_name AS subj_name,
                       a.obj_entity_id,  oes.canonical_name AS obj_name,
                       a.source_statement_ids
                  FROM assessments a
                  LEFT JOIN entities ses ON ses.entity_id = a.subj_entity_id
                  LEFT JOIN entities oes ON oes.entity_id = a.obj_entity_id
                 WHERE a.subj_entity_id IN (SELECT entity_id FROM lib_entities)
                    OR a.obj_entity_id  IN (SELECT entity_id FROM lib_entities)
                """,
                (library_id,),
            )
            assessments = cur.fetchall()

        for assessment_id, subj_id, subj_name, obj_id, obj_name, stmt_ids in assessments:
            for side_id, side_name in (("subj", subj_name), ("obj", obj_name)):
                if side_name is None:
                    continue
                is_ph, expected_type, notes = _is_placeholder_name(side_name, placeholders)
                # If it's not a known placeholder but it LOOKS like a partial
                # person name (1-2 tokens, alpha), treat it as an auto-alias
                # candidate. This is how "J Doe" → "Jane Doe"
                # gets caught without anyone hardcoding it.
                auto_alias = False
                if not is_ph and _looks_like_partial_person_name(side_name):
                    is_ph = True
                    expected_type = "person"
                    notes = "auto_alias_bridge"
                    auto_alias = True
                if not is_ph:
                    continue
                placeholder = side_name.strip().lower()
                target_eid_to_exclude = subj_id if side_id == "subj" else obj_id

                # For each source statement, get document_id and try to resolve.
                if not stmt_ids:
                    skipped_no_doc += 1
                    continue
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT DISTINCT document_id FROM statements WHERE statement_id = ANY(%s) AND document_id IS NOT NULL",
                        (list(stmt_ids),),
                    )
                    doc_ids = [r[0] for r in cur.fetchall()]
                if not doc_ids:
                    skipped_no_doc += 1
                    continue

                alias_prefix = _alias_bridge_prefix(notes)

                for doc_id in doc_ids:
                    if auto_alias:
                        # Generalized: find fuller-form persons in same doc
                        # that contain every token of the partial as a word
                        # or initial. "J Doe" → "Jane Doe".
                        candidates = _find_auto_alias_candidates(
                            conn, doc_id, side_name, exclude_id=target_eid_to_exclude
                        )
                        method = "auto_alias_bridge"
                        confidence = 0.78
                    elif alias_prefix:
                        # Hardcoded short-form → full-form: "al" → "Alex *"
                        candidates = _find_alias_bridge_candidates(conn, doc_id, alias_prefix)
                        method = "alias_bridge"
                        confidence = 0.80
                    else:
                        # Doc-singleton: dominant entity of expected type.
                        candidates = _find_document_candidates(
                            conn, doc_id, expected_type, exclude_id=target_eid_to_exclude
                        )
                        method = "doc_singleton"
                        confidence = 0.85
                    if not candidates:
                        skipped_no_candidate += 1
                        continue
                    # High-precision: only bind when one candidate dominates.
                    if len(candidates) == 1:
                        chosen = candidates[0]
                    elif candidates[0][3] >= 2 * candidates[1][3]:
                        chosen = candidates[0]
                    else:
                        skipped_multi_candidate += 1
                        continue

                    # Optional LLM validation gate.
                    final_method = method
                    final_conf = confidence
                    if llm_validate:
                        decision, adj = _llm_validate_binding(
                            conn,
                            placeholder=placeholder,
                            candidate_name=chosen[1],
                            candidate_type=chosen[2],
                            document_id=doc_id,
                        )
                        if decision == "no":
                            llm_rejected += 1
                            log.info(
                                "anaphora[%s]: LLM REJECTED %r -> %r in doc %s",
                                method, placeholder, chosen[1], doc_id,
                            )
                            continue
                        if decision == "yes":
                            llm_confirmed += 1
                            final_method = f"{method}+llm"
                            final_conf = min(0.95, confidence + adj)
                        elif decision == "uncertain":
                            final_conf = max(0.5, confidence + adj)
                        elif decision == "unavailable":
                            llm_unavailable += 1
                            # keep heuristic confidence as-is

                    _upsert_binding(
                        conn,
                        placeholder=placeholder,
                        document_id=doc_id,
                        entity_id=chosen[0],
                        learned_from=final_method,
                        confidence=final_conf,
                    )
                    bound += 1
                    log.info(
                        "anaphora[%s]: bound %r in doc %s -> entity %s (%s) conf=%.2f",
                        final_method, placeholder, doc_id, chosen[0], chosen[1], final_conf,
                    )
        conn.commit()

    return {
        "library_id": library_id,
        "ok": True,
        "bindings_written": bound,
        "skipped_no_candidate": skipped_no_candidate,
        "skipped_multi_candidate": skipped_multi_candidate,
        "skipped_no_doc": skipped_no_doc,
        "llm_used": llm_validate,
        "llm_confirmed": llm_confirmed,
        "llm_rejected": llm_rejected,
        "llm_unavailable": llm_unavailable,
    }


def lookup_binding(
    placeholder: str, document_id: int | None
) -> Optional[tuple[int, float, int]]:
    """Runtime query: does a doc-scoped binding exist for this placeholder?
    Returns (entity_id, confidence, binding_id) or None.

    The placeholder is matched case-insensitively against the canonical name
    of the placeholder entity (e.g. 'House' or 'the house' both map to 'house').
    """
    if not placeholder or document_id is None:
        return None
    key = placeholder.strip().lower()
    # Also try a 'the X' variant — the substrate sometimes stores 'House'
    # while the placeholder table has 'the house'.
    variants = {key, f"the {key}"}
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT resolved_entity_id, confidence, binding_id
                  FROM context_bindings
                 WHERE context_scope = 'document'
                   AND context_value = %s
                   AND placeholder = ANY(%s)
                 ORDER BY confidence DESC, evidence_count DESC
                 LIMIT 1
                """,
                (str(document_id), list(variants)),
            )
            row = cur.fetchone()
            if not row:
                return None
            return row[0], float(row[1]), row[2]


def record_application(
    binding_id: int, assessment_id: int | None = None, statement_id: int | None = None
) -> None:
    if binding_id is None:
        return
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO context_binding_applications
                    (binding_id, assessment_id, statement_id)
                VALUES (%s, %s, %s)
                """,
                (binding_id, assessment_id, statement_id),
            )
        conn.commit()
