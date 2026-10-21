"""Resolve an unbound mention through the governed form→concept binding layer.

`expand_constitutive` reads `entities[].node_id` out of the envelope, and the
envelope is a FROZEN extraction-time snapshot: a name the binding layer resolved
after the document was extracted still reads as unbound forever. This module is
the read that fixes that, and it lives HERE rather than in `expansion.py` on
purpose — that module's guarantee is structural ("it never opens a connection"),
and the whole reason imported WordNet taxonomy cannot reach a constitutive frame
is that there is no database call in the path. Injecting the resolver keeps that
true.

WHAT THIS IS NOT
----------------
It is not name-matching against `entities`. Matching a mention to an entity by
name is the path already measured at =<71% precision (`EVIDENCE` §3c); it imports
a judgement nobody made. `form_concept_bindings` is a GOVERNED binding — something
decided it, with a source and a confidence — so this reads a decision rather than
making one.

WHAT IT REFUSES, AND WHY EACH REFUSAL IS A MEASURED NUMBER
----------------------------------------------------------
Measured by `scripts/measure_mention_resolution.py` on node-A 2026-08-10, over all
87,839 envelope-unbound entity mentions (44,975 distinct names). The naive reading
of "resolve through form_concept_bindings" would have accepted every row below;
each is refused for a different reason.

 31,806  36.2%  RESOLVED — one live entity concept, agreeing subtype, backed by
                two or more independent signals. ~1,400 carry a promoted
                `entities` row; the rest are concepts that exist and were never
                promoted, which is a real referent either way.
 ------  -----  the five abstentions
 26,095  29.7%  UNCORROBORATED — resolves to exactly one entity concept whose
                `signal_count` is 1, and 98% of those bindings are
                `minted_from_capture` at evidence tier G. The concept was minted
                FROM the capture that produced the mention, so binding the mention
                back to it is circular: it renames the observation and reports it
                as a connection. 26,062 distinct names for 26,095 mentions — these
                are almost all seen exactly once, which is the shape of an echo.
                Consecutive ids 301970-301977 are eight people from one document.
 13,485  15.4%  AMBIGUOUS — the form binds to several live entity concepts.
                `Claude Code` binds to a product, a person, a device and an
                organization; `British Columbia` to four. Picking one is a false
                merge, which is a violation of a property Enki claims about
                itself. Abstain and stay visible.
  6,759   7.7%  CROSS-LAYER — binds ONLY outside the mention's own layer.
                `Syria`, `Iran`, `China`, `Hamas` and 983 more names bind to
                `lexical.noun` — WordNet entries that share a spelling with a
                thing. Accepting these would be a cross-layer collapse: 0 of the
                entity concepts carry WordNet provenance and that is a property
                the project maintains deliberately. This is the trap that makes
                the naive version WORSE than doing nothing.
  6,371   7.3%  no form in the substrate, or a form with no active binding.
  3,323   3.8%  SUBTYPE CONFLICT — `France` mentioned as an organization against
                a concept typed `country`. The disagreement is the finding; it is
                not a tiebreak to be resolved silently.

So the honest recovery is ~36%, not the ~80% the raw envelope gap suggests. The
gap was never one number: it is six populations with different answers, and five
of them are abstentions.

These figures were produced twice by independent implementations — a throwaway
classifier written before this module existed, and this resolver read back by the
measurement script — and they agree within 1% on every bucket. That is the method
note from decision A applied here: a synthetic check says the rule is sound, a
real-corpus check says the data behaves, and neither substitutes for the other.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Optional

log = logging.getLogger("enki.collapse.mention_resolver")


@dataclass(frozen=True)
class Resolution:
    """One mention, resolved through the governed binding layer."""
    #: What the constituent should point at: `entity:<id>` when the concept has
    #: been promoted to an entities row, otherwise the concept's own base-60
    #: address. Both are coordinates the rest of the system already understands;
    #: a `concept:<id>` reference namespace is deliberately NOT invented here.
    node_ref: str
    concept_id: int
    concept_address: Optional[str]
    #: Independent signals backing the concept. Travels onto the Constituent so a
    #: reader can see the strength of the resolution rather than trusting it.
    corroborating_signals: int
    promoted_entity_id: Optional[int] = None


#: Every reason this resolver declines, in the order it checks them. Exposed so a
#: caller can report the shape of its own abstentions instead of seeing one
#: undifferentiated "unresolved" count.
ABSTENTION_REASONS = (
    "no_name",
    "no_form",
    "no_active_binding",
    "cross_layer_only",
    "ambiguous_multi_concept",
    "subtype_conflict",
    "uncorroborated",
)


def substrate_resolver(get_conn: Callable, *,
                       tenant: str = "default",
                       min_corroborating_signals: int) -> Any:
    """Build the resolver `expand_constitutive(resolve=...)` expects.

    `min_corroborating_signals` is REQUIRED and has no default, for the same
    reason `expand()` requires an explicit `sources`: the threshold decides
    whether 26,052 circular self-resolutions are counted as connections, and a
    default would let a caller accept them without ever choosing to. Naming it
    makes believing it a decision.

    Pass 2 to exclude concepts minted from a single capture — the measured,
    defensible setting. Passing 1 accepts echoes and is only honest if the caller
    reports them as such.

    The returned callable carries `.stats`, a count per abstention reason, so a
    run can report WHY it declined rather than only how often.
    """
    if min_corroborating_signals < 1:
        # 0 would mean "a concept with no signal at all still counts", which is
        # not a weaker threshold but a different claim: that a referent needs no
        # observation. Refuse rather than quietly treat it as 1.
        raise ValueError(
            "min_corroborating_signals must be >= 1; "
            f"got {min_corroborating_signals}")

    from core.gdu.normalize import normalize

    cache: dict = {}
    stats: dict = defaultdict(int)

    def resolve(name: Optional[str], category: Optional[str],
                subtype: Optional[str]) -> Optional[Resolution]:
        key = (name, category, subtype)
        if key in cache:
            return cache[key]
        out = _resolve_uncached(get_conn, normalize, tenant,
                               min_corroborating_signals, stats,
                               name, category, subtype)
        cache[key] = out
        return out

    resolve.stats = stats           # type: ignore[attr-defined]
    resolve.min_corroborating_signals = min_corroborating_signals  # type: ignore[attr-defined]
    return resolve


def _resolve_uncached(get_conn, normalize, tenant: str, min_signals: int,
                      stats: dict, name, category, subtype) -> Optional[Resolution]:
    if not name or not str(name).strip() or not category:
        stats["no_name"] += 1
        return None

    # The SAME pure, pack-driven normalizer the admission path used to write
    # `surface_form_normalized`. Re-implementing it here — even as one lower()
    # — would make the lookup miss exactly the forms the packs teach it to fold,
    # and would be a second owner of a rule the pack owns.
    try:
        norm = (normalize(str(name)).normalized or "").strip()
    except Exception as e:                       # pragma: no cover
        log.debug("normalize failed for %r: %s", name, e)
        stats["no_name"] += 1
        return None
    if not norm:
        stats["no_name"] += 1
        return None

    with get_conn() as conn:
        with conn.cursor() as cur:
            # Merged concepts are followed to their survivor: a merge moves
            # identity, so the tombstone is not a second referent.
            cur.execute("""
                SELECT COALESCE(cm.concept_id, c.concept_id),
                       COALESCE(cm.subtype_path, c.subtype_path),
                       COALESCE(cm.canonical_subtype, c.canonical_subtype),
                       COALESCE(cm.promoted_to_entity_id,
                                c.promoted_to_entity_id),
                       COALESCE(cm.promotion_status, c.promotion_status),
                       COALESCE(cm.signal_count, c.signal_count),
                       COALESCE(cm.concept_address, c.concept_address)
                  FROM form_nodes f
                  JOIN form_concept_bindings b
                    ON b.form_id = f.form_id AND b.active
                  JOIN concept_nodes c ON c.concept_id = b.concept_id
                  LEFT JOIN concept_nodes cm
                         ON cm.concept_id = c.merged_to_concept_id
                 WHERE f.tenant_id = %s AND f.surface_form_normalized = %s
            """, (tenant, norm))
            rows = cur.fetchall()

    if not rows:
        # Cannot tell "no form" from "form with no binding" in one query without
        # a second round trip; both are the same answer here — nothing governed
        # has been decided about this name — so they share a bucket honestly
        # rather than being split by a guess.
        stats["no_form"] += 1
        return None

    # ── the cross-layer guard ──────────────────────────────────────────────
    # A mention declares its own category. It may resolve only to a concept in
    # THAT layer. Derived from the mention rather than hardcoded to "entity."
    # so a place mention cannot reach an entity concept either — and because
    # `lexical` is never a mention category, WordNet is unreachable by
    # construction rather than by a blocklist someone must remember to update.
    layer = f"{category}."
    same_layer = [r for r in rows if (r[1] or "").startswith(layer)]
    if not same_layer:
        stats["cross_layer_only"] += 1
        return None

    live = [r for r in same_layer if r[4] in ("pending", "promoted")]
    if not live:
        stats["no_active_binding"] += 1
        return None

    if len({r[0] for r in live}) > 1:
        stats["ambiguous_multi_concept"] += 1
        return None

    r = live[0]
    csub = (r[2] or "").lower()
    if subtype and csub and str(subtype).lower() != csub:
        stats["subtype_conflict"] += 1
        return None

    signals = int(r[5] or 0)
    if signals < min_signals:
        stats["uncorroborated"] += 1
        return None

    eid = r[3]
    ref = f"entity:{eid}" if eid else (r[6] or "")
    if not ref:
        # A promoted-less concept with no address has no coordinate to point at.
        # Migration 032 refuses canonical rows without one, so this is a row that
        # should not exist; abstain rather than mint a reference.
        stats["no_active_binding"] += 1
        return None

    stats["resolved"] += 1
    return Resolution(node_ref=ref, concept_id=int(r[0]),
                      concept_address=r[6], corroborating_signals=signals,
                      promoted_entity_id=int(eid) if eid else None)
