"""Phase 3.3 — gaps schedule their own work. The substrate becomes the planner.

A required slot that is empty is not a complaint, it is a QUERY: "this vessel has
a name and no flag state" names both the thing to find and, through the slot's
declared target node type, what kind of answer would count. That is enough to
schedule research, which is why the queue stops being hand-fed here.

WHAT GETS QUEUED, AND WHAT DELIBERATELY DOES NOT
------------------------------------------------
`gaps` only. Not `blocked` — the core already withdraws blocked slots from the
measurement and keeps them out of `gaps`, and queueing research against a source
that just refused is how a work queue fills with work it cannot do. The two
states exist precisely so this function can tell them apart.

THE SLOT IS THE ENRICHMENT TYPE
-------------------------------
`enrichment_type` is written as `slot:<path>`, e.g. `slot:registration.flag_state`.
That is not a formatting choice — it makes the queue's existing partial unique
index on `(entity_id, enrichment_type) WHERE status IN ('pending','running')` do
per-(entity, slot) deduplication for free. Re-running this on every rescore is
therefore idempotent by construction rather than by remembering to check.

The slot's TARGET NODE TYPE is deliberately NOT copied into the row. It lives in
`slot_specs.slot_targets`, the pack is the authority, and a copy in a queue row
is a second owner of a governed fact that goes stale the moment the pack moves.
The worker resolves it from the slot path when it runs.

WHAT THIS CANNOT DO YET, SAID PLAINLY
-------------------------------------
`entity_enrichment_queue.entity_id` is NOT NULL with a foreign key, so only nodes
carrying a `master_entity_id` can be queued at all. Gaps on `event.*` and
`document.*` nodes have nowhere to go today. They are counted and returned as
`skipped_no_entity` rather than silently dropped, because a planner that quietly
ignores most of the corpus while reporting success is worse than one that does
not run.

And the honest state of the vessel case: all seven of `entity.vessel`'s
constitutive gaps are real and currently UNFILLABLE — the OFAC SDN pack that
publishes them has 0 vessel rows loaded. Queueing them produces a working queue
with no results, and the queue is not wrong; the data is missing. Loading it is a
data-acquisition task, not a substrate one.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("enki.gdu.gap_queue")

#: Slots that decisively settle identity are worth more than slots that merely
#: describe. An IMO turns a name into a hull; a tonnage does not. Priority is
#: read from the pack's `decisive_slots`, so this ordering moves when governance
#: moves and never because someone edited a number here.
PRIORITY_DECISIVE = 8
PRIORITY_REQUIRED = 5


def _confirmation_slots(node_type: str, subtype: str) -> set[str]:
    """Slots the pack says a human must fill or confirm.

    These are excluded from automated research entirely — not queued at low
    priority, not queued into a review state, NOT QUEUED. The distinction
    matters: a research task that proposes an eye colour has already collected a
    physical description of a real person, and the fact that a human later
    rejects it does not undo the collection.

    They still appear in `gaps`, so the incompleteness stays visible. What is
    refused is the automated filling, never the admission that we do not know.
    """
    try:
        from core.gdu.pack_registry import get_slot_specs
        spec = ((get_slot_specs() or {}).get("specs") or {}).get(
            f"{node_type}.{subtype}") or {}
        return {s for s in (spec.get("requires_confirmation") or [])
                if isinstance(s, str) and not s.startswith("_")}
    except Exception as e:
        # Fail CLOSED. If the pack cannot be read we do not know which slots are
        # sensitive, and the safe answer is to schedule nothing rather than to
        # assume nothing is protected.
        log.warning("could not read requires_confirmation for %s.%s — "
                    "queueing nothing: %s", node_type, subtype, e)
        return _FAIL_CLOSED


#: Sentinel meaning "treat every slot as confirmation-gated". Used when the pack
#: is unreadable, so a governance failure suppresses automation instead of
#: silently unlocking it.
class _AllSlots(frozenset):
    def __contains__(self, item):  # noqa: D105
        return True


_FAIL_CLOSED = _AllSlots()


def _decisive_slots(node_type: str, subtype: str) -> set[str]:
    try:
        from core.ldu.decisive_identifiers import decisive_slots_for
        return set(decisive_slots_for(subtype))
    except Exception as e:
        log.debug("decisive_slots_for(%s) failed: %s", subtype, e)
        return set()


def queue_gaps_for_node(
    node_id: str,
    tenant_id: str = "default",
    dsn: Optional[str] = None,
    conn=None,
) -> dict:
    """Emit one queue row per empty required slot on this node.

    Returns {"queued": n, "gaps": [...], "skipped_no_entity": bool, "reason": ...}.
    Idempotent: existing pending/running rows for the same (entity, slot) are
    left alone by the queue's own unique index.
    """
    # completeness_core.compute directly, NOT compute_score: the DB adapter
    # deliberately returns only (score, tier, used_default) because score_node
    # and the reconciler unpack it positionally, and its own docstring names the
    # core as the Phase 3 entry point for gaps.
    from core.gdu.completeness import project_node, blocked_slots_for
    from core.gdu.completeness_core import compute
    from storage.postgres.db import get_connection

    out = {"queued": 0, "gaps": [], "skipped_no_entity": False, "reason": None,
           "skipped_requires_confirmation": []}

    own = conn is None
    ctx = get_connection(dsn) if own else None
    c = ctx.__enter__() if own else conn
    try:
        with c.cursor() as cur:
            projected = project_node(cur, node_id, tenant_id)
            if not projected:
                out["reason"] = "node not found"
                return out
            node_type, subtype, data, entity_id = projected
            blocked = blocked_slots_for(cur, node_id, tenant_id)

        scored = compute(node_type, subtype, data, blocked_slots=blocked)
        if scored is None:
            # Abstained: no spec for this subtype, or every required slot
            # blocked. There is no declared standard to be short of, so there is
            # nothing to schedule — and scheduling anyway would invent a target.
            out["reason"] = "unscoreable — no governed standard to be short of"
            return out

        gaps = list(scored.gaps or [])
        out["gaps"] = gaps
        if not gaps:
            return out

        if not entity_id:
            # Counted, not swallowed. See the module docstring.
            out["skipped_no_entity"] = True
            out["reason"] = (f"{len(gaps)} gap(s) on a node with no "
                             f"master_entity_id — the queue is entity-keyed")
            return out

        decisive = _decisive_slots(node_type, subtype)
        gated = _confirmation_slots(node_type, subtype)
        skipped = [g for g in gaps if g in gated]
        if skipped:
            out["skipped_requires_confirmation"] = skipped
        with c.cursor() as cur:
            for slot in gaps:
                if slot in gated:
                    continue
                cur.execute("""
                    INSERT INTO entity_enrichment_queue
                        (entity_id, tenant_id, enrichment_type, status, priority)
                    VALUES (%s, %s, %s, 'pending', %s)
                    ON CONFLICT DO NOTHING
                """, (entity_id, tenant_id, f"slot:{slot}",
                      PRIORITY_DECISIVE if slot in decisive else PRIORITY_REQUIRED))
                out["queued"] += cur.rowcount
        if own:
            c.commit()
    finally:
        if own and ctx is not None:
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                pass
    return out


def slot_for_queue_row(enrichment_type: str) -> Optional[str]:
    """The slot path a queue row refers to, or None if it is not a slot task.

    The inverse of the `slot:` prefix, kept here so the encoding has exactly one
    owner and a worker never parses it by hand.
    """
    if not enrichment_type or not enrichment_type.startswith("slot:"):
        return None
    return enrichment_type[len("slot:"):] or None
