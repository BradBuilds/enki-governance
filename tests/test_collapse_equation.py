import itertools
import math

import pytest

from core.collapse.equation import EVIDENCE_FNS, Candidate, Context, Policy, collapse, score
from conftest import REPO_ROOT


@pytest.fixture
def policy():
    return Policy(weights={"id": 10.0, "exact": 3.0, "loose": 1.5},
                  vetoes={"identifier_conflict": True}, theta=1.0, delta=0.5)


def test_collapses_on_a_clear_winner(policy):
    acme, other = Candidate("e:1", name="Acme Corp"), Candidate("e:2", name="Globex")
    r = collapse([acme, other], Context(form="  ACME   corp "), policy)
    assert r.action == "collapse" and r.reason == "confident"
    assert r.chosen.candidate is acme
    assert r.chosen.score == pytest.approx(4.5)  # exact 3.0 + loose 1.5
    assert r.margin == pytest.approx(4.5)


def test_parks_when_two_candidates_are_indistinguishable(policy):
    twins = [Candidate("e:1", name="John Smith"), Candidate("e:2", name="John Smith")]
    r = collapse(twins, Context(form="John Smith"), policy)
    assert r.action == "park" and r.reason == "ambiguous"
    assert r.chosen is None
    assert r.margin == 0.0


def test_parks_when_the_best_score_is_below_theta(policy):
    r = collapse([Candidate("e:1", name="Globex")], Context(form="Acme Corp"), policy)
    assert r.action == "park" and r.reason == "ambiguous"


def test_parks_when_the_margin_is_below_delta():
    policy = Policy(weights={"prior": 2.0}, vetoes={}, theta=1.0, delta=0.5)
    ctx = Context(form="x", prior={"e:1": 0.9, "e:2": 0.8})  # scores 1.8 and 1.6
    r = collapse([Candidate("e:1"), Candidate("e:2")], ctx, policy)
    assert r.action == "park"
    assert r.margin == pytest.approx(0.2)


def test_identifier_conflict_vetoes_the_otherwise_best_candidate(policy):
    conflicting = Candidate("e:1", name="Acme Corp", identifiers={"ein": "11-111"})
    weaker = Candidate("e:2", name="Acme")
    ctx = Context(form="Acme Corp", identifiers={"EIN": "22-222"})
    vetoed = score(conflicting, ctx, policy)
    assert vetoed.vetoed and vetoed.reason == "identifier_conflict" and vetoed.score == -math.inf
    r = collapse([conflicting, weaker], ctx, policy)
    assert r.action == "collapse" and r.chosen.candidate is weaker
    assert all(s.candidate is not conflicting for s in r.ranked)


def test_parks_when_every_candidate_is_vetoed(policy):
    c = Candidate("e:1", name="Acme Corp", identifiers={"ein": "11-111"})
    r = collapse([c], Context(form="Acme Corp", identifiers={"ein": "22-222"}), policy)
    assert r.action == "park" and r.reason == "all_vetoed" and r.chosen is None


def test_a_disabled_veto_does_not_fire():
    policy = Policy(weights={"exact": 3.0}, vetoes={"identifier_conflict": False}, theta=1.0, delta=0.5)
    c = Candidate("e:1", name="Acme Corp", identifiers={"ein": "11-111"})
    r = collapse([c], Context(form="Acme Corp", identifiers={"ein": "22-222"}), policy)
    assert r.action == "collapse"


def test_matching_identifier_outweighs_a_matching_name(policy):
    by_id = Candidate("e:1", name="Acme Holdings LLC", identifiers={"ein": " 12-345 "})
    by_name = Candidate("e:2", name="Acme Corp")
    r = collapse([by_name, by_id], Context(form="Acme Corp", identifiers={"EIN": "12-345"}), policy)
    assert r.action == "collapse" and r.chosen.candidate is by_id


def test_result_does_not_depend_on_candidate_order(policy):
    cands = [Candidate("e:3", name="Acme"), Candidate("e:1", name="Acme Corp"),
             Candidate("e:2", name="Globex"), Candidate("e:4", name="Acme Corp Holdings")]
    ctx = Context(form="Acme Corp")
    results = [collapse(list(p), ctx, policy) for p in itertools.permutations(cands)]
    first = results[0]
    assert first.action == "collapse" and first.chosen.candidate.address == "e:1"
    for r in results[1:]:
        assert (r.action, r.reason, r.margin) == (first.action, first.reason, first.margin)
        assert [s.candidate.address for s in r.ranked] == [s.candidate.address for s in first.ranked]


def test_ties_are_ranked_by_address(policy):
    cands = [Candidate("e:b", name="Acme"), Candidate("e:a", name="Acme"), Candidate("e:c", name="Acme")]
    r = collapse(cands, Context(form="Acme"), policy)
    assert [s.candidate.address for s in r.ranked] == ["e:a", "e:b", "e:c"]


def test_an_axis_with_no_registered_function_is_ignored():
    policy = Policy(weights={"exact": 3.0, "not_a_real_axis": 99.0}, vetoes={}, theta=1.0, delta=0.5)
    s = score(Candidate("e:1", name="Acme"), Context(form="Acme"), policy)
    assert s.score == pytest.approx(3.0)
    assert set(s.evidence) == {"exact"}


def test_no_candidates_parks(policy):
    r = collapse([], Context(form="Acme"), policy)
    assert r.action == "park" and r.chosen is None


def test_shipped_policy_pack_loads_and_names_only_registered_axes():
    policy = Policy.load(REPO_ROOT / "libraries" / "collapsepolicy")
    assert policy.theta > 0 and policy.delta > 0
    assert set(policy.weights) <= set(EVIDENCE_FNS), "policy names an evidence axis the engine does not implement"
    assert all(w >= 0 for w in policy.weights.values())
