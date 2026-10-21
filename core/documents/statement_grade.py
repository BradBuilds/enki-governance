"""Structural grading for statements — everything survives, structure decides trust.

The fact layer currently answers one question: is there an entry in `edgevocab`
for this verb? If not the statement is marked `rejected`, and 8,300 of the
9,092 rejections mean *"could not understand"* rather than *"is untrue"*. A
rejection is terminal — `type_pair_invalid` appears exactly once in the
codebase, at the site that writes it — so a statement rejected on a lemma that
enters the dictionary tomorrow stays rejected forever. 161 statements in the
corpus are in exactly that position right now.

Grading asks a different question, and a checkable one: **is this statement
structurally sound?** Does it have a resolved subject, something to point at,
a predicate that has a verb reading at all, and a source span that is prose
rather than a docket caption? None of that requires knowing what the
connection MEANS, which is exactly why it can be judged when meaning cannot.

The pattern is `text_quality.band()` one layer up, including its most
important property: the worst band means *raw evidence only*, never deletion.

    collapsible    structurally sound — eligible to collapse
    untrusted      sound span, something unresolved. FIRST-CLASS: queryable,
                   attached to its evidence, never counted as fact, and
                   re-graded automatically when the map changes
    evidence_only  the span is document furniture, or the statement is a
                   self-reference. Stays as searchable evidence, never a fact

`untrusted` is the whole point. It is not a nicer word for rejected — the
difference is that an untrusted statement carries the REASON it is untrusted
as structured signals, so a sweep can find every statement whose only problem
was a missing verb reading and re-grade it the moment the lexicon gains one.
A backlog nothing re-grades is a rejection with better branding.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from core.documents.text_quality import span_quality

log = logging.getLogger("enki.documents.statement_grade")

COLLAPSIBLE = "collapsible"
UNTRUSTED = "untrusted"
EVIDENCE_ONLY = "evidence_only"

# Bumped when the grading logic changes, so a re-grade sweep can find every
# statement graded under an older rule without re-reading the whole corpus.
GRADE_VERSION = "v1"


def _predicate_has_verb_reading(cur, lemma: str, surface: str,
                                tenant: str = "default") -> bool:
    """Does this predicate have ANY verb sense in the loaded lexicon?

    Measured in Phase 0: of the top 200 discarded lemmas, the 303 statements
    whose predicate has no verb reading are precisely the extraction noise —
    `to`, `in`, `v`, `with`, `at`, `on`, `from`, `by`, `vs`, `and`,
    `president`, `friend`, `jurisdiction`. "Has no verb reading" is a better
    noise filter than the ~60-word `_COPULA_BLOCKLIST` hardcoded in
    `predicate_extender.py`, and it is data rather than Python.

    This is deliberately NOT sense resolution. Whether the verb collapses to a
    particular sense is Phase 6's question and needs the corpus-derived priors
    that do not exist yet. Whether it is a verb at all is structural.
    """
    cands = {c for c in ((lemma or "").strip().lower(),
                         (surface or "").strip().lower()) if c}
    if not cands:
        return False
    try:
        from core.language.lemmatizer import lemmas
        for c in list(cands):
            cands.update(lemmas(c))
    except Exception:
        pass
    try:
        cur.execute(
            """
            SELECT 1 FROM form_nodes f
              JOIN form_concept_bindings b ON b.form_id = f.form_id
              JOIN concept_nodes c ON c.concept_id = b.concept_id
             WHERE f.surface_form_normalized = ANY(%s) AND f.tenant_id = %s
               AND c.subtype_path LIKE 'lexical.%%'
               AND c.canonical_subtype = 'verb'
             LIMIT 1
            """,
            (list(cands), tenant),
        )
        return cur.fetchone() is not None
    except Exception:
        log.exception("verb-reading lookup failed for %r", lemma)
        return False


def grade_statement(
    cur,
    *,
    speaker_entity_id: Optional[int],
    object_entity_id: Optional[int],
    event_time: Any = None,
    event_location_entity_id: Optional[int] = None,
    predicate_lemma: Optional[str] = None,
    predicate_verb_surface: Optional[str] = None,
    raw_text: Optional[str] = None,
    tenant: str = "default",
) -> dict:
    """Grade one statement on structure. Returns

        {grade, signals{...}, reasons[...], grade_version}

    Every signal is recorded whether it passed or not — the reasons are what
    make a re-grade targetable, and a grade with no reasons is not auditable.
    """
    signals: dict[str, Any] = {}
    reasons: list[str] = []

    signals["subject_resolved"] = speaker_entity_id is not None
    if not signals["subject_resolved"]:
        reasons.append("no_subject_entity")

    # Self-reference: subject and object are the same entity ("X did-to X").
    # Produced by coreference merges folding a bare name into a full identity.
    # The plan's one exception to "everything survives" — dropped as a fact,
    # kept as evidence and as a pointer to the merge/split backlog.
    signals["self_reference"] = bool(
        speaker_entity_id is not None
        and object_entity_id is not None
        and speaker_entity_id == object_entity_id
    )
    if signals["self_reference"]:
        reasons.append("self_reference")

    # Something to point at: an object entity, or a date/place that makes the
    # statement event-shaped. Measured: 1,881 of the corpus's statements are
    # event-shaped, and the promoter rejected all of them as "type_pair
    # invalid" with a null destination type — not one was a real mismatch.
    signals["has_object_entity"] = object_entity_id is not None
    signals["event_shaped"] = bool(event_time or event_location_entity_id)
    signals["has_referent"] = signals["has_object_entity"] or signals["event_shaped"]
    if not signals["has_referent"]:
        reasons.append("no_object_or_event")

    signals["predicate_is_verb"] = _predicate_has_verb_reading(
        cur, predicate_lemma or "", predicate_verb_surface or "", tenant)
    if not signals["predicate_is_verb"]:
        reasons.append("predicate_has_no_verb_reading")

    # The span, not the paragraph. Every statement already comes from a prose
    # paragraph that passed chunk_quality; the furniture that reaches the fact
    # layer is the SENTENCE carved out of it. 24% of statements fail this,
    # including 21% of those currently promoted.
    if raw_text:
        try:
            sq = span_quality(cur, raw_text, tenant=tenant)
            signals["span_ok"] = bool(sq.get("span_ok"))
            signals["span_reason"] = sq.get("reason")
            signals["caps_ratio"] = sq.get("caps_ratio")
        except Exception:
            log.exception("span grading failed")
            signals["span_ok"] = True          # never fail a statement on our own error
            signals["span_reason"] = "grader_error"
    else:
        signals["span_ok"] = False
        signals["span_reason"] = "no_span"
    if not signals["span_ok"]:
        reasons.append(f"span:{signals.get('span_reason')}")

    # ── the band ──
    # Order matters. Furniture and self-reference are terminal as FACTS
    # regardless of everything else; the remaining failures are all things the
    # substrate can fix later, so they land in untrusted and stay re-gradable.
    if not signals["span_ok"] or signals["self_reference"]:
        grade = EVIDENCE_ONLY
    elif (signals["subject_resolved"] and signals["has_referent"]
          and signals["predicate_is_verb"]):
        grade = COLLAPSIBLE
    else:
        grade = UNTRUSTED

    return {"grade": grade, "signals": signals, "reasons": reasons,
            "grade_version": GRADE_VERSION}


def grade_row(cur, row: dict, tenant: str = "default") -> dict:
    """Grade a statements row given as a dict of column values."""
    return grade_statement(
        cur,
        speaker_entity_id=row.get("speaker_entity_id"),
        object_entity_id=row.get("object_entity_id"),
        event_time=row.get("event_time"),
        event_location_entity_id=row.get("event_location_entity_id"),
        predicate_lemma=row.get("predicate_lemma"),
        predicate_verb_surface=row.get("predicate_verb_surface"),
        raw_text=row.get("raw_text"),
        tenant=tenant,
    )
