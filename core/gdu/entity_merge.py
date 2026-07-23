"""
Entity merge — fold duplicate/alias entities into a canonical survivor.

The substrate accumulates name variants of the same person/org over time:
"Jane Doe", "J Doe", "Ms.Doe", "JaneDoe", "Doe"
all coexist as separate entity_ids with separate signal_counts. This module
finds those forks and merges them into one canonical entity, redirecting
every reference (assessments, statements, mentions, library_members, etc.).

Public surface:

    find_merge_candidates(library_id=None, limit=50) -> list[dict]
        Heuristic finder. Returns proposed (survivor, duplicate) pairs with
        rationale. Conservative — only proposes when the name relationship
        is strong (same normalized form, OR initial+surname match, OR
        substring match where one contains the other as a token sequence).

    preview_merge(canonical_id, duplicate_id) -> dict
        Counts what would change without writing. Lets a human eyeball
        before committing.

    merge_entities(canonical_id, duplicate_id, reason='manual') -> dict
        Atomic merge. Redirects every reference across:
          assessments.{subj,obj,primary_location}_entity_id
          statements.{speaker,object,assessment,event_location}_entity_id
          entity_mentions.entity_id
          document_mentions.entity_id
          events.actor_entity_ids, target_entity_ids (array updates)
          library_members.member_id (where member_kind='entity')
          entity_addresses.entity_id
          entity_investigations.entity_id
          investigation_findings.entity_id
          context_bindings.resolved_entity_id
          knowledge_edges, knowledge_nodes
        Then deletes the duplicate row and records the redirect.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.gdu.entity_merge")


def _normalize_name(name: str) -> str:
    """Aggressive normalization for duplicate detection."""
    if not name:
        return ""
    s = name.lower().strip()
    # Strip punctuation except spaces
    s = re.sub(r"[^\w\s]", " ", s)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def find_merge_candidates(library_id: Optional[int] = None, limit: int = 50) -> list[dict]:
    """Heuristic finder for entity forks. Returns proposed merges with
    rationale. SURVIVOR is always the entity with higher signal_count."""
    out: list[dict] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Strategy 1: same normalized canonical_name
            cur.execute(
                """
                SELECT e.entity_id, e.canonical_name, e.canonical_type,
                       e.signal_count
                  FROM entities e
                 WHERE e.signal_count > 0
                 ORDER BY e.signal_count DESC
                 LIMIT 5000
                """,
            )
            rows = cur.fetchall()
        # Group by (normalized_name, canonical_type)
        groups: dict[tuple[str, str], list[tuple[int, str, int]]] = {}
        for eid, name, ctype, sig in rows:
            key = (_normalize_name(name), ctype or "")
            if not key[0]:
                continue
            groups.setdefault(key, []).append((eid, name, sig or 0))

        for (norm, ctype), members in groups.items():
            if len(members) < 2:
                continue
            members.sort(key=lambda m: -m[2])  # by signal_count desc
            survivor = members[0]
            for dup in members[1:]:
                if dup[1] == survivor[1]:
                    # Exact same canonical_name (case-different OR truly identical)
                    rationale = "identical canonical_name"
                elif dup[1].lower() == survivor[1].lower():
                    rationale = "case-only difference"
                else:
                    rationale = "same normalized form"
                out.append({
                    "survivor_id": survivor[0],
                    "survivor_name": survivor[1],
                    "survivor_signal_count": survivor[2],
                    "duplicate_id": dup[0],
                    "duplicate_name": dup[1],
                    "duplicate_signal_count": dup[2],
                    "type": ctype,
                    "rationale": rationale,
                    "confidence": 0.95,
                })

        # Strategy 2: surname-anchored variants
        # ("Jane Doe" survivor for "J Doe", "Doe", "Ms.Doe",
        #  "JaneDoe", and other variants of the same person)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT entity_id, canonical_name, canonical_type, signal_count
                  FROM entities
                 WHERE canonical_type = 'person'
                   AND signal_count > 0
                """,
            )
            persons = cur.fetchall()

        def _split_camel(s: str) -> list[str]:
            """Split CamelCase / PascalCase. 'JaneDoe' → ['Jane','Doe']."""
            return re.findall(r"[A-Z][a-z]+|[A-Z]+(?=[A-Z]|$)|[a-z]+", s)

        def _real_tokens(name: str) -> list[str]:
            """Return clean tokens. For no-space concat names, splits the camel
            so 'JaneDoe' produces ['Jane', 'Doe']."""
            raw = [t.strip(",.;:") for t in name.split() if t.strip(",.;:")]
            out_tokens: list[str] = []
            for t in raw:
                # If a token is single-cluster + has internal capital, split it
                if " " not in t and re.search(r"[a-z][A-Z]", t):
                    out_tokens.extend(_split_camel(t))
                else:
                    out_tokens.append(t)
            return out_tokens

        # Index by anchor surname
        by_surname: dict[str, list[tuple[int, str, int, list[str]]]] = {}
        for eid, name, ctype, sig in persons:
            real = _real_tokens(name)
            if not real:
                continue
            anchor = real[-1].lower()
            if len(anchor) < 3:
                continue
            by_surname.setdefault(anchor, []).append((eid, name, sig or 0, real))

        for anchor, group in by_surname.items():
            if len(group) < 2:
                continue
            # Survivor MUST be a clean multi-token name. Bare surnames like
            # "Doe" are too ambiguous to be a merge target (could be any
            # Doe). Also reject names with adjacent duplicate tokens
            # ("Doe Jane Doe") — OCR garbage signature.
            def _is_survivor_eligible(m: tuple) -> bool:
                _eid, _name, _sig, real = m
                if len(real) < 2 or len(real) > 3:
                    return False
                # No adjacent duplicate tokens
                for i in range(1, len(real)):
                    if real[i].lower() == real[i - 1].lower():
                        return False
                # First token must be a real name (>= 2 chars, alpha)
                if len(real[0]) < 2 or not real[0].replace(".", "").isalpha():
                    return False
                # Reject all-caps / OCR-blast names as survivor
                if all(t.isupper() for t in real if len(t) > 1):
                    return False
                return True
            eligible = [m for m in group if _is_survivor_eligible(m)]
            if not eligible:
                continue
            eligible.sort(key=lambda m: -m[2])  # highest signal_count
            survivor = eligible[0]
            sv_eid, sv_name, sv_sig, sv_real = survivor
            sv_first = sv_real[0].lower()

            for dup in group:
                if dup[0] == sv_eid:
                    continue
                d_eid, d_name, d_sig, d_real = dup
                # Case A: bare surname ("Doe")
                if len(d_real) == 1 and d_real[0].lower() == anchor:
                    rationale = "bare surname → fuller form"
                # Case B: initial + surname ("J Doe", "G. Doe")
                elif (len(d_real) == 2
                      and len(d_real[0].rstrip(".")) == 1
                      and d_real[1].lower() == anchor
                      and sv_first.startswith(d_real[0].rstrip(".").lower())):
                    rationale = f"initial '{d_real[0]}' + surname → fuller form"
                # Case C: no-space concat ("JaneDoe") OR "Ms.Doe"
                elif (len(d_name.split()) == 1
                      and anchor in d_name.lower()
                      and sv_first in d_name.lower()):
                    rationale = "no-space variant of same person"
                # Case D: title + surname ("Ms.Doe", "Mr.Doe")
                elif (len(d_real) == 2
                      and d_real[0].lower() in {"mr", "ms", "mrs", "dr", "sir", "sgt", "lt"}
                      and d_real[1].lower() == anchor):
                    rationale = f"title '{d_real[0]}' + surname"
                else:
                    continue
                # Don't propose if dup has more signal than survivor (shouldn't
                # happen post-sort, but defensive — never merge a busy entity
                # INTO a quiet one)
                if d_sig > sv_sig and len(d_real) >= 2:
                    continue
                if any(c["survivor_id"] == sv_eid and c["duplicate_id"] == d_eid
                       for c in out):
                    continue
                out.append({
                    "survivor_id": sv_eid,
                    "survivor_name": sv_name,
                    "survivor_signal_count": sv_sig,
                    "duplicate_id": d_eid,
                    "duplicate_name": d_name,
                    "duplicate_signal_count": d_sig,
                    "type": "person",
                    "rationale": rationale,
                    "confidence": 0.85,
                })

    # Filter by library if requested
    if library_id is not None:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT member_id FROM library_members "
                    " WHERE library_id = %s AND member_kind = 'entity'",
                    (library_id,),
                )
                lib_entities = {r[0] for r in cur.fetchall()}
        out = [c for c in out
               if c["survivor_id"] in lib_entities or c["duplicate_id"] in lib_entities]

    # Sort by confidence desc, then by combined signal_count desc
    out.sort(key=lambda c: (-c["confidence"],
                             -(c["survivor_signal_count"] + c["duplicate_signal_count"])))
    return out[:limit]


def preview_merge(canonical_id: int, duplicate_id: int) -> dict:
    """Count what would change. Read-only."""
    out: dict = {"canonical_id": canonical_id, "duplicate_id": duplicate_id, "changes": {}}
    if canonical_id == duplicate_id:
        return {**out, "error": "same entity"}
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT canonical_name FROM entities WHERE entity_id=%s", (canonical_id,))
            r = cur.fetchone()
            if not r:
                return {**out, "error": "canonical_id not found"}
            out["canonical_name"] = r[0]
            cur.execute("SELECT canonical_name FROM entities WHERE entity_id=%s", (duplicate_id,))
            r = cur.fetchone()
            if not r:
                return {**out, "error": "duplicate_id not found"}
            out["duplicate_name"] = r[0]

            counts: dict[str, int] = {}
            checks = [
                ("assessments.subj",   "SELECT COUNT(*) FROM assessments WHERE subj_entity_id = %s"),
                ("assessments.obj",    "SELECT COUNT(*) FROM assessments WHERE obj_entity_id = %s"),
                ("assessments.loc",    "SELECT COUNT(*) FROM assessments WHERE primary_location_entity_id = %s"),
                ("statements.speaker", "SELECT COUNT(*) FROM statements WHERE speaker_entity_id = %s"),
                ("statements.object",  "SELECT COUNT(*) FROM statements WHERE object_entity_id = %s"),
                ("entity_mentions",    "SELECT COUNT(*) FROM entity_mentions WHERE entity_id = %s"),
                ("document_mentions",  "SELECT COUNT(*) FROM document_mentions WHERE entity_id = %s"),
                ("library_members",    "SELECT COUNT(*) FROM library_members WHERE member_id = %s AND member_kind='entity'"),
                ("events.actor",       "SELECT COUNT(*) FROM events WHERE %s = ANY(COALESCE(actor_entity_ids, ARRAY[]::bigint[]))"),
                ("events.target",      "SELECT COUNT(*) FROM events WHERE %s = ANY(COALESCE(target_entity_ids, ARRAY[]::bigint[]))"),
                ("entity_addresses",   "SELECT COUNT(*) FROM entity_addresses WHERE entity_id = %s"),
                ("entity_investigations", "SELECT COUNT(*) FROM entity_investigations WHERE entity_id = %s"),
                ("investigation_findings", "SELECT COUNT(*) FROM investigation_findings WHERE entity_id = %s"),
                ("context_bindings",   "SELECT COUNT(*) FROM context_bindings WHERE resolved_entity_id = %s"),
            ]
            for label, sql in checks:
                cur.execute(sql, (duplicate_id,))
                counts[label] = int(cur.fetchone()[0] or 0)
            out["changes"] = counts
            out["total_redirects"] = sum(counts.values())
    return out


def merge_entities(canonical_id: int, duplicate_id: int, reason: str = "manual") -> dict:
    """Execute the merge. Atomic — rolls back on any error."""
    if canonical_id == duplicate_id:
        return {"ok": False, "error": "cannot merge entity into itself"}
    result: dict = {"canonical_id": canonical_id, "duplicate_id": duplicate_id, "changed": {}}
    with get_connection() as conn:
        try:
            with conn.cursor() as cur:
                # Record redirect first (so downstream lookups can follow it)
                cur.execute(
                    """
                    INSERT INTO entity_redirects (old_entity_id, new_entity_id,
                                                  merged_by, merge_reason)
                    VALUES (%s, %s, 'entity_merge', %s)
                    ON CONFLICT (old_entity_id) DO UPDATE
                       SET new_entity_id = EXCLUDED.new_entity_id,
                           merge_reason  = EXCLUDED.merge_reason
                    """,
                    (duplicate_id, canonical_id, reason),
                )

                # 1. assessments
                for col in ("subj_entity_id", "obj_entity_id", "primary_location_entity_id"):
                    cur.execute(
                        f"UPDATE assessments SET {col} = %s WHERE {col} = %s",
                        (canonical_id, duplicate_id),
                    )
                    result["changed"][f"assessments.{col}"] = cur.rowcount or 0

                # 2. statements
                for col in ("speaker_entity_id", "object_entity_id",
                            "assessment_entity_id", "event_location_entity_id"):
                    try:
                        cur.execute(
                            f"UPDATE statements SET {col} = %s WHERE {col} = %s",
                            (canonical_id, duplicate_id),
                        )
                        result["changed"][f"statements.{col}"] = cur.rowcount or 0
                    except Exception:
                        conn.rollback()  # column may not exist on older schema
                        # Re-open transaction for remaining work
                        cur = conn.cursor()

                # 3. entity_mentions — drop redundant duplicates first
                # (UNIQUE on (entity_id, signal_rid, first_offset))
                cur.execute(
                    """
                    DELETE FROM entity_mentions dup
                     WHERE dup.entity_id = %s
                       AND EXISTS (
                         SELECT 1 FROM entity_mentions keep
                          WHERE keep.entity_id = %s
                            AND keep.signal_rid IS NOT DISTINCT FROM dup.signal_rid
                            AND COALESCE(keep.first_offset, 0) = COALESCE(dup.first_offset, 0)
                       )
                    """,
                    (duplicate_id, canonical_id),
                )
                cur.execute(
                    "UPDATE entity_mentions SET entity_id = %s WHERE entity_id = %s",
                    (canonical_id, duplicate_id),
                )
                result["changed"]["entity_mentions"] = cur.rowcount or 0

                # 4. document_mentions
                cur.execute(
                    "UPDATE document_mentions SET entity_id = %s WHERE entity_id = %s",
                    (canonical_id, duplicate_id),
                )
                result["changed"]["document_mentions"] = cur.rowcount or 0

                # 5. library_members — UNIQUE(library_id, member_kind, member_id)
                # means we can't just UPDATE; if both canonical and duplicate
                # are in the same library, we'd hit the constraint. So first
                # delete duplicate's rows where canonical already exists, then
                # update the rest.
                cur.execute(
                    """
                    DELETE FROM library_members dup
                     WHERE dup.member_id = %s AND dup.member_kind = 'entity'
                       AND EXISTS (
                         SELECT 1 FROM library_members keep
                          WHERE keep.member_id = %s AND keep.member_kind = 'entity'
                            AND keep.library_id = dup.library_id
                       )
                    """,
                    (duplicate_id, canonical_id),
                )
                redundant = cur.rowcount or 0
                cur.execute(
                    "UPDATE library_members SET member_id = %s "
                    " WHERE member_id = %s AND member_kind = 'entity'",
                    (canonical_id, duplicate_id),
                )
                result["changed"]["library_members"] = (cur.rowcount or 0) + redundant
                result["changed"]["library_members_redundant"] = redundant

                # 6. events.actor_entity_ids / target_entity_ids (arrays)
                cur.execute(
                    """
                    UPDATE events
                       SET actor_entity_ids = array_replace(actor_entity_ids, %s, %s)
                     WHERE %s = ANY(COALESCE(actor_entity_ids, ARRAY[]::bigint[]))
                    """,
                    (duplicate_id, canonical_id, duplicate_id),
                )
                result["changed"]["events.actor"] = cur.rowcount or 0
                cur.execute(
                    """
                    UPDATE events
                       SET target_entity_ids = array_replace(target_entity_ids, %s, %s)
                     WHERE %s = ANY(COALESCE(target_entity_ids, ARRAY[]::bigint[]))
                    """,
                    (duplicate_id, canonical_id, duplicate_id),
                )
                result["changed"]["events.target"] = cur.rowcount or 0

                # 7. entity_addresses — UNIQUE(tenant_id, entity_id, address_type, raw_address)
                # Drop dup rows that collide with canonical's existing rows; redirect the rest.
                cur.execute(
                    """
                    DELETE FROM entity_addresses dup
                     WHERE dup.entity_id = %s
                       AND EXISTS (
                         SELECT 1 FROM entity_addresses keep
                          WHERE keep.entity_id = %s
                            AND keep.tenant_id = dup.tenant_id
                            AND COALESCE(keep.address_type,'') = COALESCE(dup.address_type,'')
                            AND keep.raw_address = dup.raw_address
                       )
                    """,
                    (duplicate_id, canonical_id),
                )
                cur.execute(
                    "UPDATE entity_addresses SET entity_id = %s WHERE entity_id = %s",
                    (canonical_id, duplicate_id),
                )
                result["changed"]["entity_addresses"] = cur.rowcount or 0

                # 8. entity_investigations
                cur.execute(
                    "UPDATE entity_investigations SET entity_id = %s WHERE entity_id = %s",
                    (canonical_id, duplicate_id),
                )
                result["changed"]["entity_investigations"] = cur.rowcount or 0

                # 9. investigation_findings
                cur.execute(
                    "UPDATE investigation_findings SET entity_id = %s WHERE entity_id = %s",
                    (canonical_id, duplicate_id),
                )
                result["changed"]["investigation_findings"] = cur.rowcount or 0

                # 9b. investigation_discovered — FK to entities, must redirect
                try:
                    cur.execute(
                        "UPDATE investigation_discovered SET entity_id = %s WHERE entity_id = %s",
                        (canonical_id, duplicate_id),
                    )
                    result["changed"]["investigation_discovered"] = cur.rowcount or 0
                except Exception:
                    pass  # Table may not exist on older deploys

                # 10. context_bindings.resolved_entity_id
                cur.execute(
                    "UPDATE context_bindings SET resolved_entity_id = %s WHERE resolved_entity_id = %s",
                    (canonical_id, duplicate_id),
                )
                result["changed"]["context_bindings"] = cur.rowcount or 0

                # 11. Merge aliases — add duplicate's canonical_name to survivor's aliases
                cur.execute(
                    "SELECT canonical_name, aliases FROM entities WHERE entity_id=%s",
                    (duplicate_id,),
                )
                dup_row = cur.fetchone()
                if dup_row:
                    dup_name, dup_aliases = dup_row
                    cur.execute(
                        """
                        UPDATE entities
                           SET aliases = (
                             SELECT ARRAY(SELECT DISTINCT UNNEST(
                               COALESCE(aliases, '{}'::text[]) || %s::text[]
                             ))
                           ),
                               signal_count = COALESCE(signal_count,0) +
                                              COALESCE((SELECT signal_count FROM entities WHERE entity_id=%s),0)
                         WHERE entity_id = %s
                        """,
                        ([dup_name] + (dup_aliases or []), duplicate_id, canonical_id),
                    )

                # 11.5 Base-60 substrate consolidation (Phase 2 piece 4).
                # Rewire form→concept bindings: every form bound to a
                # B-concept now also binds to the A-concept; the B-side
                # bindings get marked inactive (audit-preserving).
                # B's concepts are flagged promotion_status='merged' with
                # merged_to_concept_id pointing at A's concept. Failures
                # here log but don't abort merge — the entity-level
                # operation is what matters for downstream queries.
                # SAVEPOINT so a substrate failure rolls back ONLY this sub-step.
                # Without it, an error here (e.g. a varchar overflow) poisons the
                # whole Postgres transaction, so the DELETE below and the commit
                # both fail and the entire merge silently rolls back — defeating
                # the "log but don't abort merge" intent.
                cur.execute("SAVEPOINT sub_consolidate")
                try:
                    from core.ldu.admit_candidate import consolidate_bindings_on_merge
                    sub_result = consolidate_bindings_on_merge(
                        cur=cur,
                        tenant_id="default",
                        canonical_entity_id=canonical_id,
                        duplicate_entity_id=duplicate_id,
                        merge_reason=reason,
                    )
                    result["changed"]["substrate"] = sub_result
                    cur.execute("RELEASE SAVEPOINT sub_consolidate")
                except Exception as _sub_err:
                    cur.execute("ROLLBACK TO SAVEPOINT sub_consolidate")
                    log.warning(
                        "merge substrate consolidation failed (merge still committed): %s",
                        _sub_err,
                    )

                # 12. Finally, retire the duplicate row.
                # If the duplicate is itself the target (new_entity_id) of an
                # existing redirect — i.e. an earlier merge folded some entity
                # INTO it — we CANNOT hard-DELETE it: the ON DELETE CASCADE into
                # entity_redirects is blocked by the append-only no_mutate trigger,
                # which rolls back the entire merge. In that chain-merge case,
                # tombstone instead. All references are already repointed to
                # canonical (steps 1-11), the duplicate->canonical redirect (step 9)
                # plus chain-walking resolve it to canonical on read, and the
                # envelope.reclassification marker hides it from list/aggregation
                # queries via ENTITY_NOT_RECLASSIFIED.
                cur.execute(
                    "SELECT 1 FROM entity_redirects WHERE new_entity_id = %s LIMIT 1",
                    (duplicate_id,),
                )
                if cur.fetchone() is not None:
                    cur.execute(
                        """
                        UPDATE entities
                           SET envelope = COALESCE(envelope, '{}'::jsonb) || jsonb_build_object(
                                 'reclassification', jsonb_build_object(
                                     'action', 'merged_away',
                                     'reclassified_to', 'entity:' || %s::text,
                                     'merged_at', now()::text,
                                     'reason', %s)),
                               updated_at = now()
                         WHERE entity_id = %s
                        """,
                        (canonical_id, reason, duplicate_id),
                    )
                    result["changed"]["entity_tombstoned"] = cur.rowcount or 0
                else:
                    cur.execute(
                        "DELETE FROM entities WHERE entity_id = %s", (duplicate_id,)
                    )
                    result["changed"]["entity_deleted"] = cur.rowcount or 0
            conn.commit()
            result["ok"] = True
        except Exception as e:
            conn.rollback()
            log.exception("merge_entities failed: %s", e)
            result["ok"] = False
            result["error"] = str(e)
    return result
