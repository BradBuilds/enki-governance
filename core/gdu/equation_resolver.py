"""Entity resolution through the ONE general equation.

The live gateway resolver (`_resolve_or_create_entity`) is the collapse equation
hand-coded as layers: Layer 0.5 = the `id` axis (weight 10), Layer 1 = `exact`,
Layer 2.5 = `loose` (token-superset), and the `identifier_conflict` guards are the
veto. This module expresses that SAME decision through the general engine
(`core.collapse.equation`) governed by the content-hashed `entitypolicy` pack — so
entity collapse and sense collapse run one equation, tuned by data not code.

Two functions:
  gather_candidates(...)  — union of the layer retrieval queries → [Candidate]
  resolve(...)            — score + collapse(): the entity_id to attach to, or None
                            (park → caller creates a new entity)

It is introduced in SHADOW mode first: the gateway computes this decision alongside
the proven layered one and logs agreement/divergence, changing no live behaviour
until the two are shown to agree on the locked cases.
"""
from __future__ import annotations

from core.collapse.equation import Candidate, Context, Policy, collapse, CollapseResult

# IDENTIFIER_PRECEDENCE lists the strong ids, in order; reused for candidate pull.
from core.gdu.identity import IDENTIFIER_PRECEDENCE


def _norm_ids(identifiers: dict | None) -> dict:
    return {str(k).lower(): str(v) for k, v in (identifiers or {}).items() if v}


def gather_candidates(cur, tenant_id: str, normalized: str, loose: str,
                      identifiers: dict, entity_type: str) -> list[Candidate]:
    """Every entity the layered resolver could match — strong-identifier, exact
    normalized name, alias, and loose (variant) — as scoring Candidates. Deduped by
    entity_id; richest row (with identifiers) wins. Address = str(entity_id)."""
    inc = _norm_ids(identifiers)
    rows: dict[int, tuple] = {}  # entity_id -> (normalized_name, identifiers)

    def add(eid, name, idents):
        if eid not in rows or (idents and not rows[eid][1]):
            rows[eid] = (name or "", idents or {})

    # strong-identifier matches (drives the `id` axis)
    for key in IDENTIFIER_PRECEDENCE:
        val = inc.get(key)
        if not val:
            continue
        cur.execute("""
            SELECT entity_id, normalized_name, identifiers FROM entities
            WHERE tenant_id = %s AND identifiers @> jsonb_build_object(%s, %s::text)
            ORDER BY signal_count DESC NULLS LAST, entity_id ASC LIMIT 5
        """, (tenant_id, key, val))
        for eid, nm, idents in cur.fetchall():
            add(eid, nm, idents)

    # exact normalized-name matches (the `exact` axis)
    cur.execute("""
        SELECT entity_id, normalized_name, identifiers FROM entities
        WHERE tenant_id = %s AND normalized_name = %s
        ORDER BY signal_count DESC NULLS LAST, entity_id ASC LIMIT 5
    """, (tenant_id, normalized))
    for eid, nm, idents in cur.fetchall():
        add(eid, nm, idents)

    # alias matches (strict or loose surface seen before). Array-overlap (&&) so the
    # GIN index on aliases is used — `= ANY(aliases)` cannot use it (seq scan).
    cur.execute("""
        SELECT entity_id, normalized_name, identifiers FROM entities
        WHERE tenant_id = %s AND aliases && ARRAY[%s, %s]::text[]
        ORDER BY signal_count DESC NULLS LAST, entity_id ASC LIMIT 5
    """, (tenant_id, normalized, loose))
    for eid, nm, idents in cur.fetchall():
        add(eid, nm, idents)

    # loose / variant-collision matches within the same type (the `loose` axis,
    # which in the equation IS the token-superset test)
    cur.execute("""
        SELECT entity_id, normalized_name, identifiers FROM entities
        WHERE tenant_id = %s AND canonical_type = %s AND normalized_name_loose = %s
        ORDER BY signal_count DESC NULLS LAST, entity_id ASC LIMIT 5
    """, (tenant_id, entity_type, loose))
    for eid, nm, idents in cur.fetchall():
        add(eid, nm, idents)

    return [Candidate(address=str(eid), name=nm, identifiers=idents)
            for eid, (nm, idents) in rows.items()]


def cooccurrence_context(cur, tenant_id: str, document_ids: list[int],
                         entity_type: str = "person", limit: int = 60) -> dict[int, float]:
    """Which canonical entities co-occur in these documents, weighted [0,1] by
    mention share. This is the corroborating evidence for alias assignment: in the
    flight logs the passenger 'AA' is resolved against the entities that actually
    populate those documents (Acme dominates → strong prior for Acme)."""
    if not document_ids:
        return {}
    cur.execute("""
        SELECT dm.entity_id, COUNT(*) AS n
          FROM document_mentions dm
          JOIN entities e ON e.entity_id = dm.entity_id
         WHERE dm.entity_id IS NOT NULL
           AND dm.document_id = ANY(%s)
           AND e.tenant_id = %s
           AND e.canonical_type = %s
         GROUP BY dm.entity_id
         ORDER BY n DESC
         LIMIT %s
    """, (document_ids, tenant_id, entity_type, limit))
    rows = cur.fetchall()
    if not rows:
        return {}
    top = float(rows[0][1]) or 1.0
    return {int(eid): min(1.0, float(n) / top) for eid, n in rows}


def resolve_alias(cur, tenant_id: str, form: str, context: dict[int, float],
                  policy: Policy, entity_type: str = "person",
                  ) -> tuple[int | None, CollapseResult]:
    """Resolve a short/ambiguous surface (initials 'AA', partial name) to a canonical
    entity through the ONE equation, governed by `aliaspolicy`. `context` = {entity_id:
    co-occurrence strength} from cooccurrence_context(). Returns (entity_id|None, result);
    None ⇒ PARK ⇒ caller routes the surface to LDU-P instead of minting a junk entity.
    Multiple evidence required: initials alone are below θ (see aliaspolicy)."""
    from core.collapse.equation import _norm  # normalized-name helper
    from core.collapse.manifest_split import looks_like_manifest

    norm = _norm(form)
    rows: dict[int, tuple] = {}  # entity_id -> (name, identifiers)

    def add(eid, name, idents):
        # A passenger token must resolve to a real person, never to another
        # manifest-list fragment ("PAT EXAMPLE" → itself). Skip junk targets.
        if looks_like_manifest(name):
            return
        if eid not in rows or (idents and not rows[eid][1]):
            rows[eid] = (name or "", idents or {})

    # candidate pool 1: the co-occurring entities (initials/exact resolve against these)
    if context:
        cur.execute("""
            SELECT entity_id, canonical_name, identifiers FROM entities
             WHERE tenant_id = %s AND entity_id = ANY(%s)
        """, (tenant_id, list(context.keys())))
        for eid, nm, idents in cur.fetchall():
            add(eid, nm, idents)

    # candidate pool 2: exact normalized-name or alias hits (full-name passengers)
    cur.execute("""
        SELECT entity_id, canonical_name, identifiers FROM entities
         WHERE tenant_id = %s AND canonical_type = %s
           AND (normalized_name = %s OR aliases && ARRAY[%s]::text[])
         ORDER BY signal_count DESC NULLS LAST, entity_id ASC LIMIT 10
    """, (tenant_id, entity_type, norm, norm))
    for eid, nm, idents in cur.fetchall():
        add(eid, nm, idents)

    if not rows:
        return None, CollapseResult("park", None, 0.0, "no_candidates", [])

    candidates = [Candidate(address=str(eid), name=nm, identifiers=idents)
                  for eid, (nm, idents) in rows.items()]
    # co-occurrence strengths ride the `prior` axis (address -> P(a|form))
    prior = {str(eid): float(w) for eid, w in (context or {}).items()}
    ctx = Context(form=form, prior=prior)
    result = collapse(candidates, ctx, policy)
    if result.action == "collapse" and result.chosen is not None:
        return int(result.chosen.candidate.address), result
    return None, result


def resolve(cur, tenant_id: str, normalized: str, loose: str, identifiers: dict,
            entity_type: str, policy: Policy) -> tuple[int | None, CollapseResult]:
    """The equation's decision for this signal. Returns (entity_id|None, result).
    entity_id None ⇒ PARK (no confident, non-conflicting match) ⇒ caller creates new.
    The form is the normalized name; candidates carry their own normalized name so
    the `exact` axis is an apples-to-apples comparison."""
    candidates = gather_candidates(cur, tenant_id, normalized, loose, identifiers, entity_type)
    ctx = Context(form=normalized, identifiers=identifiers or {})
    result = collapse(candidates, ctx, policy)
    if result.action == "collapse" and result.chosen is not None:
        return int(result.chosen.candidate.address), result
    return None, result
