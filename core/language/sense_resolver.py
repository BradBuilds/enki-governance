"""Live sense resolution — disambiguate a surface form to ONE sense via the equation.

The production counterpart of scripts/collapse_run.py: given a surface form and its
context, gather the candidate senses bound to that form in the live grid (across all
loaded lexical libraries), score them through the one governed equation
(collapsepolicy), and either COLLAPSE to a single sense or PARK (abstain). Every
decision is written to `collapse_decisions` — reversible, auditable, and the source
of the `prior` evidence axis (the cyclical feedback).

This is the sense-layer entry point the ingestion pipeline calls on significant
terms. It is read-mostly: it never mutates the grid, only appends a decision row.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from core.collapse.equation import Candidate, Context, Policy, collapse, CollapseResult

_POLICY: Policy | None = None


def _policy() -> Policy:
    global _POLICY
    if _POLICY is None:
        _POLICY = Policy.load("libraries/collapsepolicy")
    return _POLICY


@dataclass
class SenseResolution:
    form: str
    action: str                 # "collapse" | "park"
    address: str | None         # chosen sense address (None if parked)
    library: str | None
    score: float | None
    reason: str
    n_candidates: int


def gather_sense_candidates(cur, form_norm: str, tenant: str = "default",
                            also_forms: tuple = (), pos: str | None = None,
                            as_of=None):
    """Every sense bound to this form in the grid + each sense's relation neighbours
    (the collocation evidence). Returns (candidates, library_by_address).

    `as_of` is the RESOLUTION CLOCK (Phase 4.3). It is accepted here — this is
    the function that reads `form_concept_bindings`, so it is where a Phase 5
    interval predicate has to land — and in Phase 4 it is deliberately NOT USED
    TO FILTER anything. Phase 4's exit criterion is *"bindings can carry
    intervals; nothing yet reads them"*, and `scripts/exit_criteria_phase4.py`
    [8] asserts that a resolution under `as_of=1850-01-01` is identical to one
    under `as_of=None`. That check is EXPECTED TO FLIP when Phase 5 lands; it is
    not a bug when it does.

    Threading it now, unused, is what makes 5.4 cheap: the clock becomes part of
    the recorded input before there is data whose provenance would have to be
    reconstructed afterwards.

    `also_forms` unions in the senses of additional surface forms (e.g. the lemmas
    of an inflected input, from core.language.lemmatizer) so an inflected form is
    disambiguated against its real lemma's senses too. Each candidate keeps the
    sense-frequency of the form it came from; duplicate addresses keep the higher
    freq (the more dominant reading across the forms).

    `pos` constrains candidates to one part of speech. Default None = every
    lexical reading, which is the historical behaviour and what every existing
    caller gets. A token in PREDICATE position must pass pos='verb': measured
    over the top 200 discarded lemmas, 48 lemmas / 508 statements — 45% of
    everything the unconstrained resolver claimed to understand — resolved
    confidently to a noun or adjective ('at' to a Laotian currency unit, 'v'
    to the 22nd letter of the alphabet). Constraining also RAISES the rate,
    53% -> 75%, because the correct verb sense was being crowded out by an
    adjective matching the surface string more exactly (the `exact` axis
    carries weight 3.0)."""
    forms = [form_norm] + [f for f in also_forms if f and f != form_norm]
    cur.execute("""
        SELECT c.concept_id, c.concept_address, c.canonical_name,
               c.type_evidence->>'register', c.type_evidence->>'domain',
               c.type_evidence->>'library',
               (fcb.binding_evidence->>'freq')::float
          FROM form_nodes f
          JOIN form_concept_bindings fcb ON fcb.form_id = f.form_id
          JOIN concept_nodes c ON c.concept_id = fcb.concept_id
         WHERE f.surface_form_normalized = ANY(%s) AND f.tenant_id = %s
           AND c.subtype_path LIKE 'lexical.%%'
           AND (%s IS NULL OR c.canonical_subtype = %s)
         ORDER BY c.concept_address
    """, (forms, tenant, pos, pos))
    rows = cur.fetchall()
    # dedup by concept_id, keeping the row with the higher freq
    best: dict = {}
    for cid, addr, name, reg, dom, lib, freq in rows:
        prev = best.get(cid)
        if prev is None or (freq or 0.0) > (prev[6] or 0.0):
            best[cid] = (cid, addr, name, reg, dom, lib, freq)
    cands, lib_by_addr = [], {}
    for cid, addr, name, reg, dom, lib, freq in sorted(best.values(), key=lambda r: r[1]):
        cur.execute("""
            SELECT cb.concept_address FROM concept_relations cr
              JOIN concept_nodes cb ON cb.concept_id = cr.concept_id_b
             WHERE cr.concept_id_a = %s
            UNION
            SELECT ca.concept_address FROM concept_relations cr
              JOIN concept_nodes ca ON ca.concept_id = cr.concept_id_a
             WHERE cr.concept_id_b = %s AND cr.is_symmetric
        """, (cid, cid))
        rel = frozenset(r[0] for r in cur.fetchall())
        cands.append(Candidate(addr, name or "", reg or "", dom, relation_addresses=rel,
                               freq=freq or 0.0))
        lib_by_addr[addr] = lib
    return cands, lib_by_addr


def resolve_sense(form_norm: str, get_conn, *, domain: str | None = None,
                  register_hint: str | None = None, nearby: frozenset = frozenset(),
                  pins: dict | None = None, prior: dict | None = None,
                  tenant: str = "default", lemmatize: bool = False,
                  write_decision: bool = True,
                  pos: str | None = None, as_of=None) -> SenseResolution:
    """Disambiguate `form_norm` in context to one sense (or park). Writes a reversible
    collapse_decision unless write_decision=False.

    `pins` is the domain-conditional override channel (a domain/curated pack asserting
    a sense for this form); it outranks the unconditional frequency default. `prior` is
    the cyclical-feedback channel (accumulated collapse_decisions). Both are
    {address: 0..1}.

    `pos` constrains the candidate set to one part of speech. Default None is
    every lexical reading — the historical behaviour every existing caller
    gets. A token in PREDICATE position must pass pos='verb', or 45% of the
    confident answers land on a noun or adjective.

    `as_of` is the resolution clock (Phase 4.3). **`None` means "no clock
    asserted", NEVER "now"** — 5.3, and the reason is measured rather than
    stylistic: `document_date` is NULL on every node-A document, so a "now" default
    would silently license 6,820 archive documents to be read with 2026
    vocabulary. In Phase 4 the clock changes NO decision; it is validated and
    recorded on the decision row so that a collapse can afterwards say which
    clock produced it. Phase 5 is what makes it govern.

    It stays optional here rather than becoming a required argument like
    `expand()`'s `sources`. That rule exists where a default silently ACCEPTS a
    class of evidence — and a clock that decides nothing cannot. When Phase 5
    makes it govern, the same argument says it should become required, and that
    is the right change to make then rather than churn 30 call sites now."""
    from core.collapse.temporal import normalize_as_of
    as_of = normalize_as_of(as_of)
    pol = _policy()
    also = ()
    if lemmatize:
        from core.language.lemmatizer import lemmas
        also = tuple(lemmas(form_norm))
    with get_conn() as conn, conn.cursor() as cur:
        cands, lib_by_addr = gather_sense_candidates(cur, form_norm, tenant,
                                                     also_forms=also, pos=pos,
                                                     as_of=as_of)
        ctx = Context(form_norm, domain=domain, register_hint=register_hint,
                      nearby_addresses=nearby, pins=pins or {}, prior=prior or {})
        r: CollapseResult = collapse(cands, ctx, pol)
        chosen = r.chosen.candidate.address if r.chosen else None
        if write_decision:
            # 5.4 — the clock is part of "same input", so a decision must say
            # which clock produced it. Recorded even in Phase 4, where it
            # produced nothing: a decision row written today under no clock and
            # one written tomorrow under a clock must be distinguishable, and
            # that distinction cannot be reconstructed afterwards. `null` here
            # is the honest "no clock asserted", not an absent key.
            ev = {"decided_address": chosen, "domain": domain,
                  "register_hint": register_hint,
                  "as_of": as_of.isoformat() if as_of else None,
                  "ranked": [(s.candidate.address, round(s.score, 3)) for s in r.ranked[:8]]}
            cur.execute("""
                INSERT INTO collapse_decisions
                    (surface_form_norm, action, score, confidence_tier,
                     evidence_for, decided_by, reversible)
                VALUES (%s, %s, %s, %s, %s::jsonb, 'auto:equation_v1', TRUE)
            """, (form_norm, r.action, (r.chosen.score if r.chosen else None),
                  'hard' if r.action == 'collapse' else 'no', json.dumps(ev)))
            conn.commit()
        return SenseResolution(
            form=form_norm, action=r.action, address=chosen,
            library=lib_by_addr.get(chosen), score=(r.chosen.score if r.chosen else None),
            reason=r.reason, n_candidates=len(cands))
