"""Expansion — the inverse of collapse.

Collapse NARROWS: form + context → one meaning (argmax of S). Expansion BROADENS,
in two senses, both governed by the same grid:

  1. meaning_fan(f, C)  — the form's DISTRIBUTION over candidate meanings, P(a|f,C).
     Collapse picks the argmax; the fan is everything before the pick. Same scores,
     normalized. This is "what could this mean, and how likely."

  2. expand(a)          — from a meaning, walk concept_relations to RELATED meanings
     (synonyms, hypernyms, …). This is "what is this connected to." Ontological
     (WordNet) and associative (thesaurus) edges both live here; the caller chooses
     which rel_types to follow and how far.

Both are deterministic (sorted, content-addressed), so two nodes expand identically.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

from core.collapse.equation import Candidate, Context, Policy, score


def meaning_fan(candidates: list[Candidate], ctx: Context, policy: Policy) -> dict:
    """P(a | f, C) over the candidates — the meaning distribution (softmax of the
    collapse scores; vetoed candidates get probability 0). Deterministic (keys sorted)."""
    live = [s for s in (score(c, ctx, policy) for c in candidates) if not s.vetoed]
    if not live:
        return {}
    mx = max(s.score for s in live)
    exps = {s.candidate.address: math.exp(s.score - mx) for s in live}
    z = sum(exps.values()) or 1.0
    return {a: exps[a] / z for a in sorted(exps)}


#: Source libraries `expand` will walk, and what believing each one commits you to.
#: Measured on node-A 2026-08-09, `concept_relations` is 228,300 rows of imported
#: WordNet taxonomy and 9,106 of corpus co-occurrence — and every single row is
#: lexical→lexical. None of it is derived from what a registry published.
KNOWN_SOURCES = {
    # Open English WordNet. IMPORTED JUDGEMENT and licensed data: someone else's
    # opinion of what a word is-a. Useful for sense work, never evidence about an
    # entity. `scripts/audit_contamination.py` counts it as a cross.
    "wordnet",
    # Co-occurrence observed in this corpus. Derived here, but ASSOCIATIVE — "seen
    # near" is contingent, not constitutive, and must never be read as depth.
    "corpus",
}


def expand(address: str, get_conn, *, sources: set,
           rel_types: set | None = None, max_depth: int = 1) -> list:
    """Walk concept_relations outward from `address` — the related-meaning graph.

    `sources` is REQUIRED and has no default. It used to walk everything in the
    table, which meant a caller got imported WordNet taxonomy back without ever
    asking for it — and could not tell, from the return value, that it had. A
    default that silently includes imported data is how contamination returns
    after it has been removed. Naming the source makes believing it a decision.

    This walks ASSOCIATIVE relations only. For what a concept is MADE OF — its
    identifiers, its constitutive frame — use `expand_constitutive`, which is
    derived from the governed decomposition and cannot reach this table.

    Returns a sorted list of (neighbor_address, rel_type, depth, source_library).
    Symmetric edges are followed both ways; directed edges follow a→b.
    Deterministic BFS.
    """
    unknown = set(sources) - KNOWN_SOURCES
    if unknown:
        # Raise rather than return []. An empty walk reads as "nothing is
        # connected to this", which is a claim about the corpus; a typo'd source
        # is a claim about the call, and the two must not look alike.
        raise ValueError(
            f"unknown expansion source(s) {sorted(unknown)}; "
            f"known: {sorted(KNOWN_SOURCES)}")
    if not sources:
        raise ValueError("expand() requires at least one source to walk")
    seen = {address}
    frontier = [address]
    out: list = []
    for depth in range(1, max_depth + 1):
        nxt: list = []
        with get_conn() as c:
            with c.cursor() as cur:
                for addr in frontier:
                    # source_library is filtered IN THE QUERY and returned in the
                    # row, so a caller can always see which library it is looking
                    # at rather than inferring it from the rel_type.
                    cur.execute("""
                        SELECT cb.concept_address, cr.rel_type, cr.source_library
                          FROM concept_relations cr
                          JOIN concept_nodes ca ON ca.concept_id = cr.concept_id_a
                          JOIN concept_nodes cb ON cb.concept_id = cr.concept_id_b
                         WHERE ca.concept_address = %s
                           AND cr.source_library = ANY(%s)
                        UNION
                        SELECT ca.concept_address, cr.rel_type, cr.source_library
                          FROM concept_relations cr
                          JOIN concept_nodes ca ON ca.concept_id = cr.concept_id_a
                          JOIN concept_nodes cb ON cb.concept_id = cr.concept_id_b
                         WHERE cb.concept_address = %s AND cr.is_symmetric
                           AND cr.source_library = ANY(%s)
                    """, (addr, sorted(sources), addr, sorted(sources)))
                    for nbr, rt, src in cur.fetchall():
                        if rel_types and rt not in rel_types:
                            continue
                        if nbr not in seen:
                            seen.add(nbr)
                            out.append((nbr, rt, depth, src))
                            nxt.append(nbr)
        frontier = nxt
        if not frontier:
            break
    return sorted(out)


# ── Constitutive expansion — what a concept is MADE OF ──────────────────────

@dataclass(frozen=True)
class Constituent:
    """One declared part of a concept's decomposition."""
    slot: str
    #: Node type this slot points at, from `slot_targets`. None for a scalar —
    #: an IMO number and a tonnage are values, and inventing a node type for
    #: them would fabricate structure.
    target_type: Optional[str]
    #: This slot alone settles identity (pack `decisive_slots`).
    decisive: bool
    #: Declared required rather than optional.
    required: bool
    filled: bool
    value: Any = None
    #: 1 for a direct constituent, 2 for a constituent OF a constituent, and so
    #: on. Depth is the whole point: a person is not a flat list of facts, it has
    #: a body, and the body has features. Knowing 3 of 8 body features is a
    #: different statement from knowing 3 of 40 person facts.
    depth: int = 1
    #: Full path from the root concept, e.g. "body.eye_colour". Unique across the
    #: tree, so two constituents named `colour` under different parents never
    #: collide.
    path: str = ""
    #: Always "constitutive". Carried explicitly so a caller holding a mixed list
    #: can tell a registry-derived part from an associative neighbour without
    #: knowing which function produced it.
    source: str = "constitutive"
    #: For a connection drawn from a list-valued slot: True when the entry names
    #: a node, False when it is a mention nobody has bound yet, None when the
    #: constituent is not a connection at all.
    #:
    #: Measured on node-A 2026-08-10: 87,413 of 110,929 entity mentions (78.8%) carry
    #: no `node_id` IN THE ENVELOPE. Counting those as connections would make the
    #: graph look far denser than it is, which is the flattering direction this
    #: whole design refuses. But the envelope is a FROZEN extraction-time
    #: snapshot, so its silence is not the substrate's — see `bound_via`.
    bound: Optional[bool] = None
    #: HOW the connection was established, and it is not a detail.
    #:
    #:   "envelope"  — the extractor bound it at extraction time.
    #:   "substrate" — the envelope said nothing and the substrate's governed
    #:                 form→concept binding resolved it since.
    #:   None        — not a connection, or still unresolved.
    #:
    #: The two are different epistemic objects: one is a decision frozen into a
    #: snapshot, the other is the current state of the binding layer. A caller
    #: that cannot tell them apart cannot say which of its connections would
    #: survive a re-extraction.
    bound_via: Optional[str] = None
    #: For a substrate resolution: how many independent signals back the concept
    #: it resolved to. Carried because a concept minted from ONE capture is an
    #: echo of the very mention being resolved, and a caller must be able to see
    #: that rather than infer it. None when not resolved through the substrate.
    corroborating_signals: Optional[int] = None
    #: The role the entry claims in this node, e.g. "subject", "perpetrator".
    #: Carried because "mentions X" and "X did this" are different facts and a
    #: bare pointer loses the difference.
    role: Optional[str] = None


def expand_constitutive(node_type: str, subtype: str, data: dict, *,
                        specs: Optional[dict] = None,
                        max_depth: int = 1,
                        resolve: Optional[Any] = None,
                        _seen: Optional[set] = None,
                        _prefix: str = "",
                        _depth: int = 1) -> list:
    """The full constitutive frame of one concept: every declared slot, filled or not.

    This is the expansion that answers "what would it take to KNOW this thing",
    and it is the inverse of collapse in the sense that matters: collapse narrows
    many observations to one node, this widens one node to the frame it must fill.

    DERIVED, NOT STORED — and that is the answer to open question 3. The
    decomposition is already declared in `slot_specs` and the fills are already on
    the node, so a table of constitutive relations would be a second owner of a
    fact the pack owns. This codebase has now paid for that mistake three times:
    twelve completeness writers, two hash-chain implementations, and a decisive-key
    list sitting beside the decomposition it duplicated.

    IT CANNOT REACH `concept_relations`. Not by policy — structurally: this
    function never opens a connection. Every constituent it returns is sourced
    from the governed pack, so imported WordNet taxonomy has no path into the
    result even if someone later points a caller at the wrong thing.

    `resolve` — OPTIONAL, and this function stays pure with it. It is a callable
    `(name, category, subtype) -> Resolution | None` supplied by the caller,
    consulted ONLY for a list entry the envelope left unbound. The DB read lives
    in the caller's resolver (`core.collapse.mention_resolver`), never here, so
    the structural guarantee above is unchanged: there is still no connection in
    this module's constitutive path.

    Omitting it is not a neutral default. The envelope is a frozen
    extraction-time snapshot, so with `resolve=None` this function reports the
    substrate as it looked when the document was extracted, not as it is. That
    reading is the conservative one — it can only UNDER-report connections — so
    it is the default. Naming a resolver is how a caller asks for the current
    state and accepts the resolver's own abstentions.

    THE UNFILLED SLOTS ARE THE POINT. Returning only what is filled would report a
    vessel we know nothing about as fully expanded. The empty ones are both the
    honest measure of our ignorance and the exact list Phase 3.3 schedules research
    against — `gaps` and this frame are two readings of one structure.
    """
    from core.gdu.completeness_core import is_filled, read_slot

    if specs is None:
        from core.gdu.pack_registry import get_slot_specs
        specs = get_slot_specs() or {}

    all_specs = specs.get("specs") or {}
    spec = all_specs.get(f"{node_type}.{subtype}")
    if not spec:
        # No declared decomposition. Abstain with an empty frame rather than
        # falling back to `default_spec`: the default exists to keep COMPLETENESS
        # measurable, and reusing it here would assert that a subtype nobody has
        # decomposed is made of a title and a source — a claim about the world
        # drawn from a scoring convenience.
        return []

    required = list(spec.get("required_slots") or [])
    optional = list(spec.get("optional_slots") or [])
    targets = {k: v for k, v in (spec.get("slot_targets") or {}).items()
               if not k.startswith("_")}
    decisive = {s for s in (spec.get("decisive_slots") or [])
                if isinstance(s, str) and not s.startswith("_")}

    seen = set(_seen or ())
    key = f"{node_type}.{subtype}"
    if key in seen:
        # A decomposition that reaches itself would expand forever, and the plan
        # is explicit that constitutive depth must be BOUNDED or "the address
        # space stops meaning anything". Stop at the cycle rather than at an
        # arbitrary limit, so a legitimately deep tree is not truncated while a
        # circular one cannot run away.
        return []
    seen.add(key)

    out: list = []
    for slot in required + optional:
        value = read_slot(data, slot)
        path = f"{_prefix}{slot}"
        target = targets.get(slot)
        out.append(Constituent(
            slot=slot,
            target_type=target,
            decisive=slot in decisive,
            required=slot in required,
            filled=is_filled(value),
            value=value if is_filled(value) else None,
            depth=_depth,
            path=path,
        ))

        # ── A list-valued slot whose entries carry their own type ───────────
        # `entities`, `topics` and `spatial.resolved_places` do not point at ONE
        # thing of a declared type — they hold many, and each entry states its
        # own `_category`/`_subtype`, its role, and whether it was ever bound to
        # a node. So the target type here is a property of the DATA, not of the
        # spec, which is why `slot_targets` cannot express it.
        entries = value if isinstance(value, list) else None
        if entries and _depth < max_depth and all(
                isinstance(e, dict) for e in entries):
            for i, e in enumerate(entries):
                cat = e.get("_category")
                sub = e.get("_subtype")
                if not cat or not sub:
                    continue
                nid = e.get("node_id")
                label = (e.get("name_canonical") or e.get("name_raw")
                         or f"#{i}")
                epath = f"{path}[{label}]"

                # ── the envelope is a SNAPSHOT, not the substrate ────────
                # A null `node_id` records that nothing was bound when this
                # document was extracted. The binding layer may have resolved
                # the name since, and reading the snapshot as though it were
                # current reports a connection as absent because it was late.
                # The resolver is asked only when the snapshot is silent — an
                # existing binding is a decision already taken, and re-deciding
                # it here would quietly overrule the extractor.
                via = "envelope" if nid else None
                signals = None
                if not nid and resolve is not None:
                    r = resolve(label, cat, sub)
                    if r is not None:
                        nid = r.node_ref
                        signals = r.corroborating_signals
                        via = "substrate"

                out.append(Constituent(
                    slot=f"{slot}[{label}]",
                    target_type=f"{cat}.{sub}",
                    decisive=False,
                    required=False,
                    filled=nid is not None,
                    value=nid,
                    depth=_depth + 1,
                    path=epath,
                    bound=nid is not None,
                    bound_via=via,
                    corroborating_signals=signals,
                    role=e.get("role"),
                ))
                # UNBOUND ENTRIES ARE NOT RECURSED. A mention nobody has bound
                # is not a connection to something — it is a name we have not
                # resolved. Expanding its frame would ask "what is this thing's
                # tonnage" when the actionable question is "what IS this thing".
                # It would also let 80% of mentions inflate the tree with
                # hypotheticals.
                if nid:
                    out.extend(expand_constitutive(
                        cat, sub, e, specs=specs, max_depth=max_depth,
                        resolve=resolve,
                        _seen=seen, _prefix=f"{epath}.", _depth=_depth + 2,
                    ))
            continue

        # ── Recurse into a singly-typed constituent ─────────────────────────
        # A slot that points at a node type is not a leaf: it names another
        # concept, which has a decomposition of its own. This is what makes the
        # frame a TREE rather than a row — `person -> body -> eye_colour` — and
        # it is why "how much do we know about this person" cannot be answered
        # one level up.
        #
        # Recursion does NOT require the slot to be filled. An unfilled `body` on
        # a person still tells you the eight things a body would have to answer;
        # collapsing it to one empty slot would hide seven questions and make the
        # person look far better known than they are.
        if target and _depth < max_depth:
            if "." in target:
                child_type, child_sub = target.split(".", 1)
            else:
                # A bare target names a category with no subtype (e.g. "entity").
                # There is no single decomposition for "some entity", so stop —
                # picking one would invent structure.
                continue
            child_data = value if isinstance(value, dict) else {}
            out.extend(expand_constitutive(
                child_type, child_sub, child_data,
                specs=specs, max_depth=max_depth, resolve=resolve,
                _seen=seen, _prefix=f"{path}.", _depth=_depth + 1,
            ))
    # Sorted so two nodes expanding the same concept get the same list in the
    # same order — expansion is part of the determinism claim, same as collapse.
    return sorted(out, key=lambda c: c.path)


def frame_completeness(frame: list) -> dict:
    """How much of a whole constitutive TREE is known — not just its top row.

    `compute()` in completeness_core scores a node against ITS OWN declared
    slots. That is correct for what it measures, and it is not the question "how
    much do we know about this person". Measured on the laptop: adding `body` to
    `entity.person` moved persons 1.00 -> 0.85, because `body` is ONE slot on the
    person and its seven physical descriptors belong to `entity.person_body` — a
    different node type, scored separately. Node-complete and tree-complete are
    genuinely different readings and the gap between them widens with depth.

    So: 4 of 5 on the node, 4 of 12 across the tree. Both true. The second is the
    one that answers what a reader actually means.

    Reported DERIVED and never stored. The stored score keeps meaning "this node
    against its own spec"; changing that would rewrite every number in the corpus
    to answer a different question than the one it was asked.

    Unresolved mentions count in `total` and never in `filled` — an unbound name
    is a connection point we have not closed, and dropping it would make the tree
    look smaller and better known than it is.
    """
    if not frame:
        return {"filled": 0, "total": 0, "ratio": None, "max_depth": 0,
                "unresolved": 0, "confirmation_gated": 0,
                "bound_via_envelope": 0, "bound_via_substrate": 0}
    filled = sum(1 for c in frame if c.filled)
    unresolved = sum(1 for c in frame if c.bound is False)
    return {
        "filled": filled,
        "total": len(frame),
        # None rather than 0.0 when there is nothing to measure — the same
        # abstention the scorer uses, for the same reason.
        "ratio": round(filled / len(frame), 3) if frame else None,
        "max_depth": max(c.depth for c in frame),
        "unresolved": unresolved,
        # Reported separately rather than summed into one "connections" figure.
        # A connection the extractor froze into the envelope and one the binding
        # layer resolved afterwards are not interchangeable, and a single total
        # would let a resolver's abstention policy silently move a headline
        # number that reads as a fact about the corpus.
        "bound_via_envelope": sum(1 for c in frame
                                  if c.bound_via == "envelope"),
        "bound_via_substrate": sum(1 for c in frame
                                   if c.bound_via == "substrate"),
    }
