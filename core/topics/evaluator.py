"""Rule evaluator — runs a membership_rule against the substrate and
returns the resulting member set.

Each rule kind has its own evaluator function; the dispatch table maps
match.kind → callable. New kinds register here.

Output shape:
    list[Member] where Member = (member_kind, member_id, score, via_rule)

No domain knowledge. The same evaluator handles a UFO research library,
an Iran-Israel news watch, a financial-crime case file. Membership
constraints come from the rule data, not from the engine.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from storage.postgres.db import get_connection
from core.topics.rules import validate as validate_rule, MembershipRuleError

log = logging.getLogger("enki.topics.evaluator")


@dataclass(frozen=True)
class Member:
    kind: str          # 'entity' | 'event' | 'assessment' | 'document' | 'signal' | 'topic'
    id: int
    score: float
    via_rule: str      # clause id that produced this membership


# ─── Per-kind evaluators ──────────────────────────────────────────────────────

def _eval_co_occurrence(params: dict, conn, clause_id: str,
                        score_method: str) -> list[Member]:
    """Entities that appear in N+ same documents (or N+ same signals) within scope.

    Scope choices:
      {library_id: int}        → document_mentions within that library
      {document_ids: [int]}    → document_mentions within those docs
      {signal_ids: [int]}      → entity_mentions on those signals
      {all: true}              → all documents
    """
    scope = params.get("scope") or {}
    min_docs = int(params.get("min_documents", 3))
    type_filter = params.get("entity_types") or []

    # Resolve the working document set OR signal set
    doc_ids: list[int] = []
    sig_ids: list[int] = []
    with conn.cursor() as cur:
        if "library_id" in scope:
            cur.execute(
                "SELECT member_id FROM library_members "
                " WHERE library_id=%s AND member_kind='document'",
                (int(scope["library_id"]),),
            )
            doc_ids = [r[0] for r in cur.fetchall()]
        elif "document_ids" in scope:
            doc_ids = [int(x) for x in scope["document_ids"]]
        elif "signal_ids" in scope:
            sig_ids = [int(x) for x in scope["signal_ids"]]
        elif scope.get("all") is True:
            # All docs across the substrate
            cur.execute("SELECT document_id FROM documents")
            doc_ids = [r[0] for r in cur.fetchall()]

    members: list[Member] = []

    if doc_ids:
        # Count distinct docs each entity appears in
        sql = """
            SELECT dm.entity_id, COUNT(DISTINCT dm.document_id) AS n
              FROM document_mentions dm
              JOIN entities e ON e.entity_id = dm.entity_id
             WHERE dm.document_id = ANY(%s)
               AND dm.entity_id IS NOT NULL
        """
        sql_params: list = [doc_ids]
        if type_filter:
            sql += " AND e.canonical_type = ANY(%s)"
            sql_params.append(type_filter)
        sql += " GROUP BY dm.entity_id HAVING COUNT(DISTINCT dm.document_id) >= %s"
        sql_params.append(min_docs)
        with conn.cursor() as cur:
            cur.execute(sql, sql_params)
            rows = cur.fetchall()
        max_docs_seen = max((r[1] for r in rows), default=1)
        for eid, n in rows:
            members.append(Member(
                kind="entity", id=eid,
                score=_score_for_co_occurrence(score_method, n, max_docs_seen, min_docs),
                via_rule=clause_id,
            ))
        # Also include the documents themselves as members
        for did in doc_ids:
            members.append(Member(
                kind="document", id=did, score=1.0, via_rule=clause_id,
            ))
        return members

    if sig_ids:
        sql = """
            SELECT em.entity_id, COUNT(DISTINCT em.signal_rid) AS n
              FROM entity_mentions em
              JOIN entities e ON e.entity_id = em.entity_id
             WHERE em.signal_rid = ANY(%s)
               AND em.entity_id IS NOT NULL
        """
        sql_params = [sig_ids]
        if type_filter:
            sql += " AND e.canonical_type = ANY(%s)"
            sql_params.append(type_filter)
        sql += " GROUP BY em.entity_id HAVING COUNT(DISTINCT em.signal_rid) >= %s"
        sql_params.append(min_docs)
        with conn.cursor() as cur:
            cur.execute(sql, sql_params)
            rows = cur.fetchall()
        max_sigs_seen = max((r[1] for r in rows), default=1)
        for eid, n in rows:
            members.append(Member(
                kind="entity", id=eid,
                score=_score_for_co_occurrence(score_method, n, max_sigs_seen, min_docs),
                via_rule=clause_id,
            ))
        for sid in sig_ids:
            members.append(Member(kind="signal", id=sid, score=1.0, via_rule=clause_id))
        return members

    return []


def _eval_predicate_walk(params: dict, conn, clause_id: str,
                         score_method: str) -> list[Member]:
    """Entities reachable from seeds via allowed predicates within max_hops.

    Walks the assessments graph. Each hop is a (subj_entity_id, predicate,
    obj_entity_id) triple where predicate ∈ allowed_predicates (or any if
    not specified). Returns reachable entities + the seeds themselves.
    """
    seeds = [int(x) for x in params["seed_entity_ids"]]
    max_hops = int(params.get("max_hops", 2))
    allowed = params.get("allowed_predicates") or []

    visited: dict[int, int] = {s: 0 for s in seeds}  # entity_id -> hop
    frontier = set(seeds)
    with conn.cursor() as cur:
        for hop in range(1, max_hops + 1):
            if not frontier:
                break
            sql = """
                SELECT subj_entity_id, obj_entity_id
                  FROM assessments
                 WHERE (subj_entity_id = ANY(%s) OR obj_entity_id = ANY(%s))
                   AND subj_entity_id IS NOT NULL AND obj_entity_id IS NOT NULL
            """
            params_sql: list = [list(frontier), list(frontier)]
            if allowed:
                sql += " AND predicate_edge_type = ANY(%s)"
                params_sql.append(allowed)
            cur.execute(sql, params_sql)
            new_frontier = set()
            for s, o in cur.fetchall():
                for nid in (s, o):
                    if nid not in visited:
                        visited[nid] = hop
                        new_frontier.add(nid)
            frontier = new_frontier

    members: list[Member] = []
    for eid, hop in visited.items():
        members.append(Member(
            kind="entity", id=eid,
            score=_score_for_predicate_walk(score_method, hop),
            via_rule=clause_id,
        ))
    return members


def _eval_user_curated(params: dict, conn, clause_id: str,
                       score_method: str) -> list[Member]:
    """Explicit list — every member gets the same score (default 1.0)."""
    members: list[Member] = []
    score = 1.0
    for kind, key in (
        ("entity", "entities"), ("event", "events"),
        ("assessment", "assessments"), ("document", "documents"),
        ("signal", "signals"),
    ):
        for mid in (params.get(key) or []):
            members.append(Member(
                kind=kind, id=int(mid), score=score, via_rule=clause_id,
            ))
    return members


def _eval_signal_topic(params: dict, conn, clause_id: str,
                       score_method: str) -> list[Member]:
    """Members are everything tagged by a watch_topics row — signals, the
    cluster-derived entities, the cluster docs. Closes the loop between
    the watch_topic primitive and the generic topic primitive."""
    wt = int(params["watch_topic_id"])
    members: list[Member] = []
    with conn.cursor() as cur:
        # Signals linked to the watch_topic
        cur.execute(
            "SELECT signal_rid FROM watch_topic_articles "
            " WHERE watch_topic_id=%s AND signal_rid IS NOT NULL",
            (wt,),
        )
        for (rid,) in cur.fetchall():
            members.append(Member(kind="signal", id=rid, score=1.0, via_rule=clause_id))
        # Entities tagged for that watch_topic
        cur.execute(
            "SELECT entity_id FROM watch_topic_entities WHERE watch_topic_id=%s",
            (wt,),
        )
        for (eid,) in cur.fetchall():
            members.append(Member(kind="entity", id=eid, score=1.0, via_rule=clause_id))
    return members


# ─── Score functions ──────────────────────────────────────────────────────────

def _score_for_co_occurrence(method: str, n: int, max_n: int, threshold: int) -> float:
    if method == "constant_1":
        return 1.0
    if method == "doc_frequency":
        return float(n) / float(max(max_n, 1))
    if method == "cooccurrence_count":
        return min(float(n) / float(max(threshold, 1)), 10.0)
    # default
    return 1.0


def _score_for_predicate_walk(method: str, hop: int) -> float:
    if method == "constant_1":
        return 1.0
    if method == "hop_decay":
        return 1.0 / (2 ** hop) if hop > 0 else 1.0
    return 1.0


# ─── Dispatch ────────────────────────────────────────────────────────────────

_DISPATCH = {
    "co_occurrence": _eval_co_occurrence,
    "predicate_walk": _eval_predicate_walk,
    "user_curated": _eval_user_curated,
    "signal_topic": _eval_signal_topic,
}


def evaluate(rule: dict, *, dsn: str | None = None) -> list[Member]:
    """Run a complete rule_set and return the merged member list.

    `combine: union`        — UNION of all clauses' outputs; per-member
                              score = MAX across clauses; via_rule = clause
                              that won the score.
    `combine: intersection` — INTERSECTION; member must be produced by
                              every clause; score = MIN across clauses.
    """
    validate_rule(rule)
    combine = rule.get("combine", "union")

    with get_connection(dsn) as conn:
        per_clause: list[list[Member]] = []
        for clause in rule["rules"]:
            kind = clause["match"]["kind"]
            params = clause["match"].get("params", {})
            score_method = clause.get("score_method", "constant_1")
            evaluator = _DISPATCH.get(kind)
            if evaluator is None:
                raise MembershipRuleError(f"unknown match.kind: {kind}")
            try:
                per_clause.append(evaluator(params, conn, clause["id"], score_method))
            except Exception as e:
                log.warning("clause %s (%s) eval failed: %s", clause["id"], kind, e)
                per_clause.append([])

    if combine == "intersection":
        return _intersect(per_clause)
    return _union(per_clause)


def _union(per_clause: list[list[Member]]) -> list[Member]:
    """UNION: keep the best (highest-score) instance of each (kind, id)."""
    best: dict[tuple[str, int], Member] = {}
    for ms in per_clause:
        for m in ms:
            k = (m.kind, m.id)
            cur = best.get(k)
            if cur is None or m.score > cur.score:
                best[k] = m
    return list(best.values())


def _intersect(per_clause: list[list[Member]]) -> list[Member]:
    """INTERSECTION: keep only members that appear in EVERY clause's output."""
    if not per_clause:
        return []
    sets = [{(m.kind, m.id): m for m in ms} for ms in per_clause]
    common = set(sets[0].keys())
    for s in sets[1:]:
        common &= set(s.keys())
    out: list[Member] = []
    for k in common:
        # min score across clauses, via_rule = lowest-score clause
        clauses = [s[k] for s in sets]
        winner = min(clauses, key=lambda m: m.score)
        out.append(winner)
    return out
