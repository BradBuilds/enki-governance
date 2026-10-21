"""Re-grading — the half of Phase 2 without which the other half is a lie.

A statement held back because the lexicon had no verb reading for its
predicate is not wrong; it is unresolved. The distinction only means something
if something re-asks the question when the lexicon changes. Otherwise
`untrusted` is `rejected` with better branding, which is the guard rail this
module exists to satisfy.

TRIGGERED BY THE CHANGE, NOT A TIMER. The events that can move a statement's
grade are all knowable: a new sense binding, an entity merge, a pack version
bump, a gazetteer reload. Each of them maps to a REASON, so a change only
re-asks the statements it could actually affect —

    a lexicon load      -> predicate_has_no_verb_reading   (667 statements)
    an entity merge     -> no_subject_entity               (921)
    a place binding     -> no_object_or_event              (879)

and never the whole corpus. A blanket re-grade of 9,092 statements through an
unproven path is how a bad mapping reaches the graph at scale.

WHAT IT WILL NOT DO. It never touches a statement that has already promoted.
Grading adds a dimension; it does not re-decide one. The 538 promoted
statements now known to rest on document furniture stay promoted and stay
visible as a correctness backlog — demoting them is a separate decision with
its own evidence, not a side effect of a sweep.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from core.documents.statement_grade import (
    COLLAPSIBLE, EVIDENCE_ONLY, GRADE_VERSION, UNTRUSTED, grade_row,
)
from storage.postgres.db import get_connection

log = logging.getLogger("enki.documents.statement_regrade")

# Which reason each kind of substrate change can possibly clear. A change
# re-asks only the statements whose recorded reason it could answer.
TRIGGER_REASONS = {
    "lexicon":      ["predicate_has_no_verb_reading"],
    "entity_merge": ["no_subject_entity"],
    "place":        ["no_object_or_event"],
    "span":         [],   # span verdicts move only when the grader itself does
}

_COLUMNS = ("statement_id", "tenant_id", "speaker_entity_id", "object_entity_id",
            "event_time", "event_location_entity_id", "predicate_lemma",
            "predicate_verb_surface", "raw_text", "validation_status")

# Statuses a re-grade is allowed to look at. 'promoted' is deliberately absent.
REGRADABLE = ("untrusted", "pending", "rejected")


def regrade(
    *,
    reasons: Optional[Iterable[str]] = None,
    statuses: Iterable[str] = ("untrusted",),
    limit: Optional[int] = None,
    requeue: bool = True,
    apply: bool = False,
) -> dict:
    """Re-grade statements and, when one becomes collapsible, put it back in
    the promoter's queue.

    `reasons` narrows to statements recorded as held back by those specific
    reasons — that is what makes a trigger cheap. Omit it and every statement
    in `statuses` is re-asked.

    Returns counters. Writes nothing unless `apply`.
    """
    statuses = tuple(s for s in statuses if s in REGRADABLE)
    if not statuses:
        raise ValueError(f"regrade refuses statuses outside {REGRADABLE} "
                         f"— promoted statements are never re-decided")

    where = ["validation_status = ANY(%s)"]
    params: list = [list(statuses)]
    if reasons:
        where.append("structure_reasons && %s")
        params.append(list(reasons))

    sql = (f"SELECT {', '.join(_COLUMNS)} FROM statements "
           f"WHERE {' AND '.join(where)} ORDER BY statement_id")
    if limit:
        sql += f" LIMIT {int(limit)}"

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = [dict(zip(_COLUMNS, r)) for r in cur.fetchall()]

    stats = {"examined": len(rows), "still_held": 0, "now_collapsible": 0,
             "now_evidence_only": 0, "requeued": 0, "applied": bool(apply)}
    if not rows:
        return stats

    updates: list[tuple] = []
    requeues: list[tuple] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            for row in rows:
                g = grade_row(cur, row, tenant=row.get("tenant_id") or "default")
                updates.append((g["grade"], g["reasons"], GRADE_VERSION,
                                row["statement_id"]))
                if g["grade"] == COLLAPSIBLE:
                    stats["now_collapsible"] += 1
                    if requeue:
                        requeues.append((row["statement_id"],))
                elif g["grade"] == EVIDENCE_ONLY:
                    stats["now_evidence_only"] += 1
                else:
                    stats["still_held"] += 1

            if apply:
                cur.executemany(
                    """
                    UPDATE statements
                       SET structure_grade = %s, structure_reasons = %s,
                           grade_version = %s, graded_at = now()
                     WHERE statement_id = %s
                    """,
                    updates,
                )
                if requeues:
                    # Back into the queue, with the reason recorded. The
                    # promoter is the only thing that decides what promotes;
                    # this only says "worth asking again".
                    cur.executemany(
                        """
                        UPDATE statements
                           SET validation_status = 'pending',
                               validation_reason = 'regraded_collapsible'
                         WHERE statement_id = %s
                           AND validation_status <> 'promoted'
                        """,
                        requeues,
                    )
                    stats["requeued"] = len(requeues)
                conn.commit()

    return stats


def on_substrate_change(kind: str, *, apply: bool = True,
                        limit: Optional[int] = None) -> dict:
    """Hook for the events that can change a grade. `kind` is one of
    TRIGGER_REASONS. Unknown kinds are a no-op rather than a full sweep —
    an unrecognised trigger must never become a corpus-wide re-grade."""
    reasons = TRIGGER_REASONS.get(kind)
    if not reasons:
        log.info("regrade trigger %r has no mapped reasons — no-op", kind)
        return {"examined": 0, "skipped": f"no reasons mapped for {kind}"}
    return regrade(reasons=reasons, statuses=("untrusted",),
                   limit=limit, apply=apply)
