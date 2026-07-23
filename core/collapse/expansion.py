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


def expand(address: str, get_conn, rel_types: set | None = None, max_depth: int = 1) -> list:
    """Walk concept_relations outward from `address` — the related-meaning graph.

    Returns a sorted list of (neighbor_address, rel_type, depth). Symmetric edges
    are followed both ways; directed edges follow a→b. Deterministic BFS."""
    seen = {address}
    frontier = [address]
    out: list = []
    for depth in range(1, max_depth + 1):
        nxt: list = []
        with get_conn() as c:
            with c.cursor() as cur:
                for addr in frontier:
                    cur.execute("""
                        SELECT cb.concept_address, cr.rel_type
                          FROM concept_relations cr
                          JOIN concept_nodes ca ON ca.concept_id = cr.concept_id_a
                          JOIN concept_nodes cb ON cb.concept_id = cr.concept_id_b
                         WHERE ca.concept_address = %s
                        UNION
                        SELECT ca.concept_address, cr.rel_type
                          FROM concept_relations cr
                          JOIN concept_nodes ca ON ca.concept_id = cr.concept_id_a
                          JOIN concept_nodes cb ON cb.concept_id = cr.concept_id_b
                         WHERE cb.concept_address = %s AND cr.is_symmetric
                    """, (addr, addr))
                    for nbr, rt in cur.fetchall():
                        if rel_types and rt not in rel_types:
                            continue
                        if nbr not in seen:
                            seen.add(nbr)
                            out.append((nbr, rt, depth))
                            nxt.append(nbr)
        frontier = nxt
        if not frontier:
            break
    return sorted(out)
