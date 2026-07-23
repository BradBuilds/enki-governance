"""
Entity validator worker — walks pending entities/places/topics and asks
the validator chain to confirm each one.

Runs at REVIEW priority under the governor so it pauses under foreground
pressure. Limited batch size per tick so Wikidata rate limits stay
comfortable (max ~10 validations per minute).

Results:
  match   → canonical_name/type updated, validation_status=validated,
            source_id/source_url/confidence stamped
  no match → validation_status=unvalidated, validation_last_error recorded
  error    → validation_attempts bumped; retried next tick if < retry_limit

The worker never deletes candidates — even rejected ones stay as nodes
so extraction isn't silently lossy. The UI badge tells the user.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("enki.learning.entity_validator")

BATCH_LIMIT = 5                # validations per tick — keep rate-limit friendly

# Cache write threshold. Validator confidence below this is too close
# to a tie-broken decision — caching it would freeze a wrong winner
# (we caught Q92561 "London, Ontario" being cached for "london" at
# 0.65). Lookups for sub-threshold names re-run the provider chain
# every time. Trade-off: rates a little more Wikidata traffic for
# correctness, which is what May 23 restart demanded.
CACHE_THRESHOLD = 0.85
MAX_RETRIES = 3                # after this many failed attempts, mark unvalidated


def run(conn) -> dict:
    stats = {
        "examined": 0,
        "validated": 0,
        "unvalidated": 0,
        "errors": 0,
    }
    from core.entity_validation import default_chain, Candidate
    from core.entity_validation.cache import (
        check_user_contribution, lookup_cache, write_cache,
    )
    chain = default_chain()

    candidates = _fetch_pending(conn, limit=BATCH_LIMIT)
    stats["examined"] = len(candidates)

    for cand_row in candidates:
        try:
            # Pull recent context (signal titles, co-mention surface forms)
            # for this entity so the Wikidata provider can score candidates
            # against corpus context instead of taking the first-typed hit.
            # No-op if the entity has no signal_rid mentions yet.
            context = None
            if cand_row["kind"] == "entity":
                try:
                    context = _fetch_context_for_entity(conn, cand_row["id"])
                except Exception as e:
                    log.debug("context fetch failed for entity %s: %s",
                              cand_row["id"], e)
                    try: conn.rollback()
                    except Exception: pass

            cand = Candidate(
                raw_text=cand_row["canonical_name"],
                kind=cand_row["kind"],
                hint_type=cand_row["canonical_type"],
                context=context,
            )

            # Layer C — user contributions override everything upstream
            match = check_user_contribution(
                conn, cand.raw_text, cand.kind,
                tenant_id=cand_row["tenant_id"],
            )
            decision = None

            # Layer B — persistent KB cache
            if match is None:
                match = lookup_cache(
                    conn, cand.raw_text, cand.kind,
                    tenant_id=cand_row["tenant_id"],
                )

            # Layer A — provider chain (only if cache missed)
            if match is None:
                match, decision = chain.resolve(cand)
                # Write positive matches back to cache for future lookups
                # ONLY when confidence is high enough that we're not
                # locking in a tie-broken decision. Below CACHE_THRESHOLD
                # every future lookup re-runs the provider chain — slower
                # but correct, which is what "proper runs" demands.
                # Example we caught: 'london' validated at confidence
                # 0.65 to Q92561 (London, Ontario), cached, then every
                # subsequent London signal returned Ontario without ever
                # running the scoring on the actual UK candidate.
                if match is not None and match.confidence >= CACHE_THRESHOLD:
                    try:
                        write_cache(
                            conn, cand.raw_text, cand.kind, match,
                            tenant_id=cand_row["tenant_id"],
                        )
                    except Exception as e:
                        log.debug("cache write failed for %s: %s", cand.raw_text, e)
                        try: conn.rollback()
                        except Exception: pass

            if match is not None:
                _apply_match(conn, cand_row, match)
                stats["validated"] += 1
            elif decision is not None and not decision.accept:
                # Filter-level rejection — not a real entity
                _mark_rejected(conn, cand_row, decision.reason)
                stats["unvalidated"] += 1
            else:
                # Provider chain exhausted, no match
                attempts = (cand_row["validation_attempts"] or 0) + 1
                if attempts >= MAX_RETRIES:
                    _mark_unvalidated(
                        conn, cand_row,
                        reason="no provider matched after {} attempts".format(attempts),
                        attempts=attempts,
                    )
                    stats["unvalidated"] += 1
                else:
                    _bump_attempts(conn, cand_row, attempts)
        except Exception as e:
            log.exception("entity_validator: %s/%s failed: %s",
                          cand_row["kind"], cand_row["id"], e)
            try: conn.rollback()
            except Exception: pass
            stats["errors"] += 1

    return stats


def register(governor) -> None:
    from core.learning.governor import Priority
    governor.register(
        name="entity_validator",
        priority=Priority.REVIEW,
        interval_seconds=45.0,      # 5 rows / 45s = ~6.7/min — well under Wikidata limits
        fn=run,
    )


# ── internals ──────────────────────────────────────────────────────────

def _fetch_context_for_entity(conn, entity_id: int) -> Optional[str]:
    """Return a short blob of context for the entity — recent signal
    titles where this entity was mentioned. Used by the Wikidata
    provider's scoring (lifespan_fit + context_overlap). Empty/None if
    the entity has no mentions yet (newly-admitted, pre-resolution).
    """
    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                SELECT s.payload->>'title' AS title
                  FROM entity_mentions em
                  JOIN signals s ON s.rid = em.signal_rid
                 WHERE em.entity_id = %s
                   AND s.payload->>'title' IS NOT NULL
                 ORDER BY s.timestamp DESC NULLS LAST
                 LIMIT 5
                """,
                (entity_id,),
            )
            titles = [r[0] for r in cur.fetchall() if r[0]]
        except Exception:
            return None
    return " | ".join(titles) if titles else None


def _fetch_pending(conn, *, limit: int) -> list[dict]:
    """Pull pending rows from entities / topics, oldest first.

    Post-unification (migration 118): places are entities with
    place-like canonical_type. No separate places table to query.
    The entities UNION branch now also covers place subtypes.

    Grandfathered rows whose canonical_type isn't in entityvocab's
    admittable set (migration 116 CHECK) are skipped here — any UPDATE
    on such a row, including a metadata-only UPDATE, fails because
    PostgreSQL re-validates the CHECK against the new full tuple.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            (
              SELECT 'entity'::text AS kind, entity_id AS id, tenant_id,
                     canonical_name, canonical_type, validation_attempts
                FROM entities
               WHERE validation_status = 'pending'
                 AND validation_attempts < %(max_retries)s
                 AND (
                   canonical_type IS NULL
                   OR canonical_type IN (
                     -- Person / org subtypes
                     'account', 'aircraft', 'device', 'facility',
                     'government_agency', 'mass_media', 'organization',
                     'person', 'product', 'unit', 'vehicle', 'vessel',
                     'weapon_system',
                     -- Place subtypes (post-migration 118)
                     'place', 'city', 'region', 'country', 'gpe',
                     'admin_region', 'facility_location', 'island',
                     'peninsula', 'archipelago', 'river', 'ocean',
                     'sea', 'strait', 'maritime_area', 'poi', 'named_area'
                   )
                 )
               ORDER BY created_at ASC
               LIMIT %(limit)s
            )
            UNION ALL
            (
              SELECT 'topic'::text, topic_id, tenant_id,
                     canonical_name, canonical_type, validation_attempts
                FROM topics
               WHERE validation_status = 'pending'
                 AND validation_attempts < %(max_retries)s
               ORDER BY created_at ASC
               LIMIT %(limit)s
            )
            LIMIT %(limit)s
            """,
            {"limit": limit, "max_retries": MAX_RETRIES},
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _table_for(kind: str) -> tuple[str, str]:
    """Return (table_name, pk_column) for a candidate kind. Post-
    migration 118 there's no separate 'place' kind — places live in
    entities."""
    return {
        "entity": ("entities", "entity_id"),
        "topic":  ("topics",   "topic_id"),
    }[kind]


def _apply_match(conn, cand_row: dict, match) -> None:
    table, pk = _table_for(cand_row["kind"])
    existing_aliases_sql = ""     # no-op for topics without aliases

    # Subtype lockdown — pick a canonical_type safe to write. The
    # row-skipping for non-admittable CURRENT types is done upstream in
    # _fetch_pending so we never reach _apply_match for a row that
    # PostgreSQL's CHECK constraint would reject regardless. Here we
    # only need to ensure the PROPOSED (Wikidata) type is in vocab; if
    # not, fall back to the row's current (already-admittable) type.
    safe_type = match.canonical_type
    if cand_row["kind"] == "entity":
        try:
            from core.ldu.subtype_validator import safe_canonical_type
            safe_type = safe_canonical_type(
                match.canonical_type,
                current=cand_row.get("canonical_type"),
            )
            if safe_type != match.canonical_type:
                log.info(
                    "entity_validator: keeping existing type %r for entity/%s "
                    "(Wikidata proposed %r, not in entityvocab)",
                    safe_type, cand_row["id"], match.canonical_type,
                )
        except Exception:
            log.debug("safe_canonical_type failed in entity_validator", exc_info=True)
            safe_type = cand_row.get("canonical_type") or match.canonical_type

    # Entities have an aliases[] array column; topics don't.
    has_aliases = cand_row["kind"] == "entity"
    if has_aliases and match.aliases:
        # Union-merge aliases
        existing_aliases_sql = """,
            aliases = ARRAY(SELECT DISTINCT unnest(
                COALESCE(aliases, ARRAY[]::text[]) || %(new_aliases)s::text[]
            ))"""

    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {table} SET
                canonical_name = %(name)s,
                canonical_type = %(type)s,
                validation_status       = 'validated',
                validated_by_source     = %(source)s,
                validation_source_id    = %(source_id)s,
                validation_source_url   = %(source_url)s,
                validation_confidence   = %(confidence)s,
                validated_at            = NOW(),
                validation_attempts     = validation_attempts + 1,
                validation_last_error   = NULL,
                updated_at              = NOW()
                {existing_aliases_sql}
             WHERE {pk} = %(id)s
            """,
            {
                "id":          cand_row["id"],
                "name":        match.canonical_name,
                "type":        safe_type,
                "source":      match.source,
                "source_id":   match.source_id,
                "source_url":  match.source_url,
                "confidence":  float(match.confidence or 0.0),
                "new_aliases": match.aliases,
            },
        )
    conn.commit()


def _mark_unvalidated(conn, cand_row: dict, *, reason: str, attempts: int) -> None:
    table, pk = _table_for(cand_row["kind"])
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {table} SET
                validation_status     = 'unvalidated',
                validation_attempts   = %s,
                validation_last_error = %s,
                updated_at            = NOW()
             WHERE {pk} = %s
            """,
            (attempts, reason[:500], cand_row["id"]),
        )
    conn.commit()


def _mark_rejected(conn, cand_row: dict, reason: str) -> None:
    table, pk = _table_for(cand_row["kind"])
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {table} SET
                validation_status     = 'rejected',
                validation_attempts   = validation_attempts + 1,
                validation_last_error = %s,
                updated_at            = NOW()
             WHERE {pk} = %s
            """,
            ("filter_rejected: " + reason[:400], cand_row["id"]),
        )
    conn.commit()


def _bump_attempts(conn, cand_row: dict, attempts: int) -> None:
    table, pk = _table_for(cand_row["kind"])
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {table} SET
                validation_attempts = %s,
                updated_at = NOW()
             WHERE {pk} = %s
            """,
            (attempts, cand_row["id"]),
        )
    conn.commit()
