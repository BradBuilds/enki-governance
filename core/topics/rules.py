"""Membership rule schema + constructors.

A membership_rule is a JSONB blob attached to topics.membership_rule. The
top-level shape is always:

  {
    "type": "rule_set",
    "version": 1,
    "rules": [ <clause>, <clause>, ... ],
    "combine": "union" | "intersection"
  }

Each <clause> is:

  {
    "id":          "<short slug, unique within rule_set>",
    "match":       { "kind": "<kind>", "params": {<kind-specific>} },
    "score_method": "<see below>"  # optional, defaults to "constant_1"
  }

Supported `match.kind` values (general — no domain hardcoding):

  co_occurrence
    "entities that appear together in N+ documents OR N+ signals"
    params:
      scope:           { "library_id": int }
                     | { "document_ids": [int,...] }
                     | { "signal_ids": [int,...] }
                     | { "all": true }
      min_documents:   int (default 3) — required co-occurrence count
      entity_types:    optional [str,...] filter

  predicate_walk
    "entities reachable from seeds via allowed predicates within max_hops"
    params:
      seed_entity_ids:    [int,...]
      max_hops:           int (default 2)
      allowed_predicates: optional [str,...] — empty/missing = all

  user_curated
    "explicit list — user picked these"
    params:
      entities:    [int,...]    # optional
      events:      [int,...]    # optional
      assessments: [int,...]    # optional
      documents:   [int,...]    # optional
      signals:     [int,...]    # optional

  signal_topic
    "everything tagged by a watch_topics row"
    params:
      watch_topic_id:    int

Score methods:
  constant_1         — every member gets score 1.0
  doc_frequency      — score = (#docs the entity appears in) / max_docs
  hop_decay          — score = 1 / 2^hops (only meaningful for predicate_walk)
  cooccurrence_count — score = #shared docs / min_documents (≥ 1.0)

The combine operator decides how to merge multiple clauses:
  union          — UNION of all clauses, each member's max score across clauses
  intersection   — INTERSECTION (member must satisfy every clause)
"""
from __future__ import annotations

import json
from typing import Any


class MembershipRuleError(ValueError):
    """Raised when a membership_rule fails validation."""


# Valid match-kind names. New kinds register here.
VALID_KINDS = {"co_occurrence", "predicate_walk", "user_curated", "signal_topic"}

VALID_SCORE_METHODS = {"constant_1", "doc_frequency", "hop_decay", "cooccurrence_count"}

VALID_COMBINE = {"union", "intersection"}


def validate(rule: dict[str, Any]) -> None:
    """Raise MembershipRuleError if the rule is malformed.

    Doesn't run the rule — just shape-checks. Cheap. Always run before
    persisting a rule (the gateway-admit path enforces this)."""
    if not isinstance(rule, dict):
        raise MembershipRuleError("rule must be a JSON object")
    if rule.get("type") != "rule_set":
        raise MembershipRuleError("rule.type must be 'rule_set'")
    if rule.get("version") != 1:
        raise MembershipRuleError("rule.version must be 1")
    rules = rule.get("rules")
    if not isinstance(rules, list) or not rules:
        raise MembershipRuleError("rule.rules must be a non-empty array")
    combine = rule.get("combine", "union")
    if combine not in VALID_COMBINE:
        raise MembershipRuleError(f"rule.combine must be one of {VALID_COMBINE}")

    seen_ids = set()
    for i, clause in enumerate(rules):
        if not isinstance(clause, dict):
            raise MembershipRuleError(f"clause[{i}] must be an object")
        cid = clause.get("id")
        if not isinstance(cid, str) or not cid:
            raise MembershipRuleError(f"clause[{i}].id must be a non-empty string")
        if cid in seen_ids:
            raise MembershipRuleError(f"duplicate clause id: {cid}")
        seen_ids.add(cid)
        match = clause.get("match")
        if not isinstance(match, dict):
            raise MembershipRuleError(f"clause[{cid}].match must be an object")
        kind = match.get("kind")
        if kind not in VALID_KINDS:
            raise MembershipRuleError(
                f"clause[{cid}].match.kind must be one of {VALID_KINDS}, got {kind!r}"
            )
        params = match.get("params", {})
        if not isinstance(params, dict):
            raise MembershipRuleError(f"clause[{cid}].match.params must be an object")
        _validate_params(cid, kind, params)
        score_method = clause.get("score_method", "constant_1")
        if score_method not in VALID_SCORE_METHODS:
            raise MembershipRuleError(
                f"clause[{cid}].score_method must be one of {VALID_SCORE_METHODS}"
            )


def _validate_params(cid: str, kind: str, params: dict) -> None:
    if kind == "co_occurrence":
        scope = params.get("scope")
        if not isinstance(scope, dict):
            raise MembershipRuleError(f"clause[{cid}].params.scope required")
        valid_scope = (
            "library_id" in scope or "document_ids" in scope or
            "signal_ids" in scope or scope.get("all") is True
        )
        if not valid_scope:
            raise MembershipRuleError(
                f"clause[{cid}].params.scope must specify library_id, "
                "document_ids, signal_ids, or all:true"
            )
        md = params.get("min_documents", 3)
        if not isinstance(md, int) or md < 1:
            raise MembershipRuleError(
                f"clause[{cid}].params.min_documents must be int >= 1"
            )
    elif kind == "predicate_walk":
        seeds = params.get("seed_entity_ids")
        if not isinstance(seeds, list) or not seeds:
            raise MembershipRuleError(
                f"clause[{cid}].params.seed_entity_ids must be non-empty list"
            )
        for s in seeds:
            if not isinstance(s, int):
                raise MembershipRuleError(
                    f"clause[{cid}].params.seed_entity_ids items must be int"
                )
        hops = params.get("max_hops", 2)
        if not isinstance(hops, int) or hops < 1 or hops > 4:
            raise MembershipRuleError(
                f"clause[{cid}].params.max_hops must be int in [1,4]"
            )
    elif kind == "user_curated":
        any_listed = any(
            isinstance(params.get(k), list) and params[k]
            for k in ("entities", "events", "assessments", "documents", "signals")
        )
        if not any_listed:
            raise MembershipRuleError(
                f"clause[{cid}].params must include at least one non-empty list"
            )
    elif kind == "signal_topic":
        wt = params.get("watch_topic_id")
        if not isinstance(wt, int):
            raise MembershipRuleError(
                f"clause[{cid}].params.watch_topic_id must be int"
            )


# ─── Constructors — convenience, no domain assumptions ────────────────────────

def co_occurrence(
    *, scope: dict, min_documents: int = 3,
    entity_types: list[str] | None = None,
    clause_id: str = "co_occurrence",
    score_method: str = "cooccurrence_count",
) -> dict:
    params: dict = {"scope": scope, "min_documents": min_documents}
    if entity_types:
        params["entity_types"] = list(entity_types)
    return {
        "id": clause_id,
        "match": {"kind": "co_occurrence", "params": params},
        "score_method": score_method,
    }


def predicate_walk(
    *, seed_entity_ids: list[int], max_hops: int = 2,
    allowed_predicates: list[str] | None = None,
    clause_id: str = "predicate_walk",
    score_method: str = "hop_decay",
) -> dict:
    params: dict = {"seed_entity_ids": list(seed_entity_ids), "max_hops": max_hops}
    if allowed_predicates:
        params["allowed_predicates"] = list(allowed_predicates)
    return {
        "id": clause_id,
        "match": {"kind": "predicate_walk", "params": params},
        "score_method": score_method,
    }


def user_curated(
    *, entities: list[int] | None = None,
    events: list[int] | None = None,
    assessments: list[int] | None = None,
    documents: list[int] | None = None,
    signals: list[int] | None = None,
    clause_id: str = "curated",
    score_method: str = "constant_1",
) -> dict:
    params: dict = {}
    if entities: params["entities"] = list(entities)
    if events: params["events"] = list(events)
    if assessments: params["assessments"] = list(assessments)
    if documents: params["documents"] = list(documents)
    if signals: params["signals"] = list(signals)
    return {
        "id": clause_id,
        "match": {"kind": "user_curated", "params": params},
        "score_method": score_method,
    }


def rule_set(clauses: list[dict], combine: str = "union") -> dict:
    """Wrap clauses into a complete rule_set."""
    rule = {"type": "rule_set", "version": 1, "rules": clauses, "combine": combine}
    validate(rule)
    return rule
