"""Topic primitive — declarative membership over the substrate.

A topic is a governed node (via core.gdu.gateway) whose members are
COMPUTED from a JSON `membership_rule`, not hand-tagged. The same engine
evaluates every topic — a UFO research library, a live news watch on
Iran-Israel, a financial-crime case file, a personal investigation —
because all of them reduce to the same primitive: "which substrate
elements match this rule?"

Public surface:
  - rules:       schema validators and rule constructors
  - evaluator:   evaluate(rule, conn) -> set of (member_kind, member_id, score, via_rule)
  - materialize: materialize_topic(topic_id) — write to topic_membership

Generality test: no code in this module references any
specific topic. If the word "Roswell" or "Iran" or any other domain word
appears, we failed.
"""
