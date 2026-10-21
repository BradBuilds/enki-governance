"""
GDU Completeness Scoring — the DATABASE adapter.

Reads a node out of Postgres, hands it to the one scoring core, and persists the
verdict stamped with the regime that produced it. The arithmetic, the slot-fill
semantics, the abstention rule and the governed tier ladder are NOT here — they
are in `core/gdu/completeness_core.py`, shared with the envelope adapter
(`core/extraction/completeness.py`) so the two cannot drift again. They did:
before the 2026-08-05 merge the two scorers disagreed on score in 70.3% of
reachable fill states and on tier in up to 11.6%.

This module owns three things the core deliberately does not:
  - the DB projection (which columns become which declared slots)
  - persistence to knowledge_nodes and entities
  - the completeness_regime stamp (migration 141)

Called by the gateway after admit_node(), and by
scripts/reconcile_completeness.py for everything that never transits the gateway
— which is most of it (see §2a of docs/whitepaper/SYSTEM.md). Completeness is a
derived property, so reconciliation is sufficient; it does not need a chokepoint.

Tier thresholds come from completenessvocab.tiers.json and are read, never
restated here. As of 0.1.0 they are stub / partial / solid / verified, with
`verified` additionally requiring ≥1 corroboration and 0 contradictions.

KNOWN LIMIT — projection blindness. The projection below cannot fill every slot
the specs declare: `document.article` declares 13 required slots of which 11 are
unreachable from these columns, and the pack's `default_spec` has 3 of 4
unreachable. Scores from this path are therefore systematically depressed, which
is the node-A Phase 0 gap (52.5% scorer view against 77.4% corpus view). Widening the
projection is Phase 1 work and is tracked in the plan; do not "fix" it by
narrowing the specs.
"""

from __future__ import annotations

import logging
from typing import Optional

_log = logging.getLogger("enki.gdu.completeness")


# _as_int moved to core/gdu/completeness_core.py with the arithmetic it served.


def compute_score(
    node_type: str,
    subtype: str,
    data: dict,
    blocked_slots: Optional[set] = None,
) -> Optional[tuple]:
    """Compute completeness score and tier for a node. DB-side adapter.

    The arithmetic, the slot-fill semantics, the abstention rule and the governed
    tier ladder all live in `core.gdu.completeness_core` — the one place they are
    allowed to live. This function's remaining job is to be the shape the DB path
    already expects.

    Kept as a tuple rather than returning the core's `Completeness` because
    `score_node` and `scripts/reconcile_completeness.py` unpack it positionally;
    changing the shape here would be a second change riding along with the merge.
    Callers wanting filled/total/gaps should call `completeness_core.compute`
    directly — that is the Phase 3 entry point.

    Returns:
        (score, tier, used_default) — score 0.0-1.0, tier from the governed ladder
        None — completeness is UNDEFINED for this (node_type, subtype): nothing
               declares what it should contain. Not zero. The caller records the
               abstention rather than substituting a number.
    """
    from core.gdu.completeness_core import compute

    result = compute(node_type, subtype, data, blocked_slots=blocked_slots)
    if result is None:
        return None
    return (result.score, result.tier, result.used_default)


#: How long a recorded refusal is allowed to speak for. A slot block is a fact
#: about our REACH on a given day, not about the world, and reach changes: a
#: Cloudflare rule today says nothing about next month. Past this age the block
#: stops withdrawing the slot, so the score goes back to counting it as a
#: genuine gap and Phase 3.3 queues it for another attempt.
#:
#: Without an expiry, one bad afternoon would shrink a node's denominator
#: permanently — and a smaller denominator is a HIGHER score, so a stale block
#: flatters exactly the way this phase exists to stop.
BLOCK_MAX_AGE_DAYS = 30


def blocked_slots_for(cur, node_id: str, tenant_id: str = "default") -> set:
    """Required slots currently withdrawn for this node, from `slot_blocks`.

    Returns an empty set when the table does not exist yet (migration 145 not
    applied), so this degrades to today's behaviour — everything counted as
    asked — rather than failing scoring. Degrading toward the LESS flattering
    reading is the safe direction: it under-reports our reach, never over-reports
    our knowledge.
    """
    # SAVEPOINT, not a bare ROLLBACK. An UndefinedTable poisons the transaction,
    # and this runs INSIDE score_node's — discarding the whole thing to recover
    # from a missing table would throw away the caller's work to answer an
    # optional question. The savepoint unwinds exactly this query.
    try:
        cur.execute("SAVEPOINT sp_slot_blocks")
    except Exception:
        return set()
    try:
        cur.execute(
            """
            SELECT DISTINCT slot FROM slot_blocks
             WHERE tenant_id = %s AND node_id = %s
               AND blocked_at > NOW() - (%s || ' days')::interval
            """,
            (tenant_id, node_id, str(BLOCK_MAX_AGE_DAYS)),
        )
        rows = {r[0] for r in cur.fetchall()}
        cur.execute("RELEASE SAVEPOINT sp_slot_blocks")
        return rows
    except Exception as e:
        try:
            cur.execute("ROLLBACK TO SAVEPOINT sp_slot_blocks")
            cur.execute("RELEASE SAVEPOINT sp_slot_blocks")
        except Exception:
            pass
        _log.debug("slot_blocks unavailable for %s: %s", node_id, e)
        return set()


def current_regime() -> str:
    """The regime string stamped onto every score this module writes.

    Names the pack versions that produced the number, so a score computed under
    one slot spec is never silently compared with one computed under another.
    Phase 1 bumps slot_specs; every row still carrying the old string is then
    visibly stale rather than quietly wrong.
    """
    from core.gdu import pack_registry
    from core.gdu.pack_registry import get_completeness_tiers, get_slot_specs

    def _v(pack: dict) -> str:
        # The definition file's own version, which tracks the content actually
        # read. Falling back to pack.json would let a slot-spec bump pass
        # unrecorded, which is precisely the staleness this stamp exists to catch.
        return str(pack.get("__definition_version__")
                   or pack.get("__pack_version__") or "unknown")

    get_completeness_tiers()  # ensure the ladder loaded before we claim it
    return (f"slot_specs@{_v(get_slot_specs())}"
            f"/tiers@{_v(pack_registry._COMPLETENESS_TIERS or {})}"
            f"/scorer@{_scorer_version()}")


def _scorer_version() -> str:
    """Content hash of the scoring logic itself.

    Two scores are comparable only if the same packs AND the same code produced
    them. Pack versions alone are not enough: changing `compute_score` to abstain
    on missing specs altered every unspecced node's answer without touching a
    single pack, and the stale check could not see it — the rows still carried a
    regime string that was, on its face, current.

    Hashing the source rather than bumping a constant by hand follows the
    extraction-regime rule already in use elsewhere (profile version = sha of the
    prompt): a version you must remember to bump is a version that eventually
    lies. Comment edits do force a rescore; rescoring is cheap and rare, and the
    alternative is silently incomparable numbers.

    HASHES THE CORE, NOT THE WRAPPERS. Since decision A the arithmetic lives in
    `completeness_core`; `compute_score` and `_score_to_tier` here are adapters
    that delegate. Hashing the adapters would leave the stamp unchanged when the
    actual scoring rule moved — a regime string that is current on its face and
    false in fact, which is the precise failure this function exists to catch.

    AND HASHES THE PROJECTION. `project_row` decides which declared slots this
    path can see at all, so it determines the number just as directly as the
    arithmetic does — widening it on 2026-08-06 moved scores corpus-wide without
    touching a pack or the core. Leaving it out would have let two genuinely
    incomparable numbers share one regime string, which is the same defect one
    level out. The projection is part of the rule.
    """
    import hashlib
    import inspect

    from core.gdu import completeness_core

    try:
        src = "".join(inspect.getsource(fn) for fn in (
            completeness_core.compute,
            completeness_core.score_to_tier,
            completeness_core.is_filled,
            completeness_core.read_slot,
            project_row,
        ))
    except (OSError, TypeError):
        return "unknown"
    return hashlib.sha256(src.encode()).hexdigest()[:8]


def unscoreable_regime() -> str:
    """Regime string for a node whose subtype has no spec.

    Carries the pack versions too, so the state is re-evaluated exactly when the
    specs move and not before. Phase 1 declares specs for new subtypes; those
    nodes then read as stale against this string and get picked up on the next
    reconcile, with no migration and no manual list.
    """
    return f"unscoreable:no_spec/{current_regime()}"


# The columns project_row reads, in the order project_node selects them. Named
# so a caller doing its own bulk scan (slot_coverage_report) selects exactly what
# production scores instead of maintaining a parallel list.
PROJECTION_NODE_COLUMNS = (
    "node_type", "subtype", "title", "description",
    "confidence", "source_count", "entity_name",
    "master_entity_id", "signal_rid",
    "base60_address", "enrichment_status",
    "corroboration_count", "contradiction_count",
    # ── widened 2026-08-06 (Phase 1 prerequisite) ────────────────────────────
    # Columns the corpus has always carried and this projection did not read.
    # The specs declare slots for all of them, so every one was scored as a gap
    # on evidence the row was holding the whole time — `document.article` could
    # not exceed ~0.15 and every unspecced subtype was capped at 0.25.
    #
    # This is the projection widening the plan requires BEFORE the vessel spec
    # is rewritten: without it, Phase 1.3's honest-worse number would be
    # measuring this reader rather than the vessel.
    "node_id", "epistemic_type", "concepts", "metadata",
    "latitude", "longitude", "entity_id", "entity_identifiers",
    "event_time", "place_name", "place_context", "images",
)

PROJECTION_ENTITY_COLUMNS = (
    "canonical_name", "aliases", "identifiers",
    "signal_count", "first_seen", "last_seen",
)


def project_row(row: dict, entity: Optional[dict] = None) -> dict:
    """Map node columns onto declared slots. PURE — no database access.

    This is the projection itself, separated from the reading of it so that
    exactly ONE mapping exists. Three callers need it and each would otherwise
    keep a copy:

      - `project_node` below (scoring one node by id)
      - `scripts/slot_coverage_report.py`, which streams the whole corpus through
        a server-side cursor and so cannot re-query per row. It previously kept a
        hand-synchronised duplicate captioned "Mirrors score_node" — and a
        baseline that mirrors production by hand stops baselining it the moment
        someone edits one side.
      - `scripts/verify_rescore_identity.py`, which must project exactly what
        production scores or it proves nothing.

    KNOWN LIMIT — this projection is narrow; see the module docstring. It is the
    reader Phase 1 has to widen, not the specs.
    """
    lat, lon = row.get("latitude"), row.get("longitude")
    data = {
        "identity.title_display": row.get("title"),
        "summary.narrative": row.get("description"),
        "evidence.confidence": row.get("confidence"),
        "evidence.source_count": row.get("source_count"),
        "identity.entity_name": row.get("entity_name"),
        "master_entity_id": row.get("master_entity_id"),
        "signal_rid": row.get("signal_rid"),
        "base60_address": row.get("base60_address"),
        "enrichment_status": row.get("enrichment_status"),
        # Needed for the `verified` tier's extra_requirements, which the ladder
        # declares and which score alone cannot satisfy. Without these every
        # 0.9+ node promoted to the top tier uncorroborated.
        "evidence.corroborations": row.get("corroboration_count"),
        "evidence.contradictions": row.get("contradiction_count"),

        # ── widened 2026-08-06 ───────────────────────────────────────────────
        # `subtype` is the starkest case: it is a REQUIRED slot in several specs
        # and it is the value this function was already handed to look the spec
        # up with. Every vessel, person and organization was penalised for not
        # declaring the thing the scorer used to find its spec.
        "subtype": row.get("subtype"),
        "identity.canonical_name": row.get("entity_name") or row.get("title"),
        "temporal.event_time": row.get("event_time"),
        "spatial.place_text": row.get("place_name"),
        "spatial.coordinates": ((lat, lon) if lat is not None and lon is not None
                                else None),
        "spatial.resolved_places": row.get("place_context") or None,
        "evidence.epistemic_type": row.get("epistemic_type"),
        "source.node_id": row.get("node_id"),
        "topics": row.get("concepts") or None,
        "entities": row.get("entity_id") or row.get("master_entity_id"),
        "media.thumbnail": row.get("images") or None,
        "identifiers": row.get("entity_identifiers") or None,
    }

    if entity:
        # The entity record wins where it has a value, but must not ERASE the
        # node-level fallbacks added above: a bare assignment would overwrite a
        # good node title with a NULL canonical_name and turn a filled slot into
        # a gap. Same `or` semantics the corpus projection has always used.
        data["identity.canonical_name"] = (entity.get("canonical_name")
                                           or data["identity.canonical_name"])
        data["identity.aliases"] = entity.get("aliases")
        data["identifiers"] = entity.get("identifiers") or data["identifiers"]
        data["evidence.source_count"] = (entity.get("signal_count")
                                         if entity.get("signal_count") is not None
                                         else data["evidence.source_count"])
        data["temporal.first_seen"] = entity.get("first_seen")
        data["temporal.last_seen"] = entity.get("last_seen")

    return data


def project_node(cur, node_id: str, tenant_id: str = "default") -> Optional[tuple]:
    """Read one knowledge_node and project its columns onto declared slots.

    Split out of `score_node` so the read and the mapping are separable; the
    mapping itself is `project_row`.

    Returns (node_type, subtype, data, master_entity_id), or None if not found.
    """
    cur.execute(f"""
        SELECT {", ".join(PROJECTION_NODE_COLUMNS)}
        FROM knowledge_nodes
        WHERE node_id = %s AND tenant_id = %s
    """, (node_id, tenant_id))
    r = cur.fetchone()
    if not r:
        return None
    row = dict(zip(PROJECTION_NODE_COLUMNS, r))

    entity = None
    if row["master_entity_id"]:
        cur.execute(f"""
            SELECT {", ".join(PROJECTION_ENTITY_COLUMNS)}
            FROM entities WHERE entity_id = %s
        """, (row["master_entity_id"],))
        erow = cur.fetchone()
        if erow:
            entity = dict(zip(PROJECTION_ENTITY_COLUMNS, erow))

    return (row["node_type"] or "entity", row["subtype"] or "person",
            project_row(row, entity), row["master_entity_id"])


def score_node(node_id: str, tenant_id: str = "default",
               dsn: Optional[str] = None) -> Optional[tuple[float, str]]:
    """Compute and persist completeness score for an existing knowledge_node.

    Reads the node from DB, computes score, writes back.
    Returns (score, tier) or None if node not found.
    """
    from storage.postgres.db import get_connection

    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                projected = project_node(cur, node_id, tenant_id)
                if projected is None:
                    return None
                node_type, subtype, data, master_entity_id = projected
                # Phase 3: withdraw slots whose source refused. Read on the same
                # cursor as the projection so the block set and the data are the
                # same instant — a block landing mid-score would otherwise shrink
                # a denominator that was measured against the wider spec.
                blocked = blocked_slots_for(cur, node_id, tenant_id)
                result = compute_score(node_type, subtype, data,
                                       blocked_slots=blocked)

                # Write back, stamped with the regime that produced it. The stamp
                # is written in the same statement as the score so the two can
                # never disagree about which specs the number came from.
                #
                # No spec for this subtype -> NULL score and NULL tier, stamped
                # 'unscoreable:no_spec'. Writing a number here would assert
                # completeness against a standard that does not exist; writing
                # 0.00 would assert emptiness. NULL with a reason is the only
                # reading that is true, and it keeps the gap countable.
                if result is None:
                    # No spec AND no usable default — nothing declares what this
                    # subtype should contain, so completeness is undefined.
                    score, tier = None, None
                    regime = unscoreable_regime()
                else:
                    score, tier, used_default = result
                    # Scored against the pack's generic default_spec rather than
                    # a subtype-specific one. A real score, but measured against
                    # envelope hygiene rather than against what this kind of
                    # thing actually is — recorded so the difference stays
                    # queryable, and so Phase 1 can find the subtypes worth
                    # writing a real spec for.
                    regime = (f"default_spec/{current_regime()}" if used_default
                              else current_regime())
                cur.execute("""
                    UPDATE knowledge_nodes
                    SET completeness_score = %s, completeness_tier = %s,
                        completeness_regime = %s, updated_at = NOW()
                    WHERE node_id = %s
                """, (score, tier, regime, node_id))

                # Also update entity if linked
                if master_entity_id:
                    cur.execute("""
                        UPDATE entities
                        SET completeness_score = %s, completeness_tier = %s,
                            completeness_regime = %s, updated_at = NOW()
                        WHERE entity_id = %s
                    """, (score, tier, regime, master_entity_id))

            conn.commit()
            return (score, tier)

    except Exception as e:
        # WARNING, not DEBUG. This module produced no scores at all for months
        # while every failure went to a log level nobody reads — the same
        # silent-failure mode that hid the auto-research trigger's root cause.
        _log.warning("Completeness scoring failed for %s: %s", node_id, e)
        return None


def _slot_filled(data: dict, slot: str) -> bool:
    """Is this slot filled? Delegates to the core's fill semantics.

    Retained under this name because `scripts/slot_coverage_report.py` imports it
    deliberately — the report measures fill rate against exactly the rule
    production scores with, and a second implementation there would make the
    baseline unfalsifiable.

    One behaviour change came with the merge: a whitespace-only string ("   ")
    now reads EMPTY. The previous test was `val != ""`, which counted it as
    filled. Nobody decided that; it fell out of an inequality chain.
    """
    from core.gdu.completeness_core import is_filled, read_slot

    return is_filled(read_slot(data, slot))


def _score_to_tier(score: float, corroborations: int = 0,
                   contradictions: int = 0) -> str:
    """Map a score onto the governed ladder. Delegates to the core.

    Kept as a name here because it is part of this module's historical surface;
    the rule itself (fail-closed, highest-min-cleared, generic `demotes_to`) is
    in `completeness_core.score_to_tier`.
    """
    from core.gdu.completeness_core import score_to_tier

    return score_to_tier(score, corroborations, contradictions)
