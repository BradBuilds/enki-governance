"""The collapse equation — a GENERAL, governed scoring ENGINE.

    S(a | f, C) = Σ_k  w_k · e_k(a, f, C)        (weighted evidence)
    subject to   veto if any enabled g_j(a, f, C) fires  (hard eliminations)

    collapse to a* = argmax_a S(a)  IFF  S(a*) ≥ θ  AND  S(a*) − S(a₂) ≥ δ
    else PARK (ambiguity) — the deferred-disambiguation case.

NOT a hardcoded collapse formula. The engine is small and fixed; the AXES, their
WEIGHTS, the VETOES, and the thresholds all come from a governed, content-hashed
POLICY pack (e.g. `collapsepolicy`). Evidence axes are a REGISTRY — a policy
selects which to use by naming them in its weights, and a new axis is a registered
function + a weight in the pack, never a code change to the engine.

Because the policy is content-hashed, the equation is deterministic and identical
on every node (models SUM). And because it's general, the SAME engine runs other
decisions the system already has vocabs for — relevance, completeness, precision,
promotion — each with its own policy pack. Collapse is just the first instance.

Expansion is the inverse: the full distribution P(a|f) over candidates is the
form's meaning-fan; expansion(a) walks concept_relations from a.

Built-in registered axes (a policy uses any subset):
  id   strong-identifier match (QID/CIK/LEI)  — drives entity collapse
  exact / loose   name evidence (loose = token-superset guarded)
  register / domain / collocation   sense evidence (from the libraries)
  prior   accumulated collapse_decisions (the cyclical feedback)
Built-in vetoes:
  identifier_conflict   hard eliminate (the two-Acmes rule)
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

NEG_INF = float("-inf")
_WS = re.compile(r"\s+")

# Axis + veto REGISTRIES. The engine knows nothing about specific axes — it applies
# whatever the policy names. Register a function here + add a weight in the policy
# pack to add an axis; no engine change. Decorators below populate these.
EVIDENCE_FNS: dict = {}
VETO_FNS: dict = {}


def evidence_axis(name):
    def deco(fn):
        EVIDENCE_FNS[name] = fn
        return fn
    return deco


def veto_rule(name):
    def deco(fn):
        VETO_FNS[name] = fn
        return fn
    return deco


# ── Inputs ────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Candidate:
    address: str
    name: str = ""
    register: str = ""
    domain: str | None = None
    identifiers: dict = field(default_factory=dict)
    relation_addresses: frozenset = field(default_factory=frozenset)  # expansion neighbours
    freq: float = 0.0  # dominant-sense prior: 1/sense_rank for THIS form (per-lemma)


@dataclass(frozen=True)
class Context:
    form: str
    identifiers: dict = field(default_factory=dict)
    domain: str | None = None
    register_hint: str | None = None
    nearby_addresses: frozenset = field(default_factory=frozenset)  # collocation evidence
    pins: dict = field(default_factory=dict)                        # address -> 0..1 curated/domain override
    prior: dict = field(default_factory=dict)                       # address -> P(a|f)
    contributor_weight: float = 0.5                                 # source trust [0,1]; open-contribution gate


class PolicyIntegrityError(ValueError):
    """A policy pack's content does not match the hash pinned for it in the active set."""


def _verify_pinned_policy(root: Path) -> None:
    """Fail closed if this policy pack is pinned in the active set and its content differs.

    Policy packs are not in the active set's `packs` list (they do not declare
    namespaces), so their hashes are pinned under `metadata.policy_packs`, which the
    active-set signature covers. A pack with no pin loads as before.
    """
    from core.gdu.config import ACTIVE_SET_PATH
    from core.spine.content_hash import compute_pack_hash

    pack_json = root / "pack.json"
    if not pack_json.exists() or not ACTIVE_SET_PATH.exists():
        return
    pack_id = json.loads(pack_json.read_text(encoding="utf-8")).get("id", root.name)
    active_set = json.loads(ACTIVE_SET_PATH.read_text(encoding="utf-8"))
    pin = (active_set.get("metadata") or {}).get("policy_packs", {}).get(pack_id)
    if not pin:
        return
    actual = compute_pack_hash(str(root))
    if actual != pin["content_hash"]:
        raise PolicyIntegrityError(
            f"policy pack '{pack_id}' does not match the active set: "
            f"pinned {pin['content_hash']}, found {actual}")


@dataclass
class Policy:
    weights: dict
    vetoes: dict
    theta: float
    delta: float

    @staticmethod
    def load(lib_root: str | Path = "libraries/collapsepolicy") -> "Policy":
        """Load a governed policy pack by its manifest — works for ANY policy pack
        (collapsepolicy, entitypolicy, …), no hardcoded filename. Falls back to the
        legacy fixed name if a pack.json/manifest isn't present."""
        root = Path(lib_root)
        _verify_pinned_policy(root)
        weights_file = None
        pack_json = root / "pack.json"
        if pack_json.exists():
            manifest = json.loads(pack_json.read_text()).get("manifest", [])
            for rel in manifest:
                if rel.endswith(".weights.json") or "weights" in rel:
                    weights_file = root / rel
                    break
        if weights_file is None:  # legacy / manifest-less fallback
            weights_file = root / "definitions" / f"{root.name}.weights.json"
            if not weights_file.exists():
                weights_file = root / "definitions" / "collapsepolicy.weights.json"
        c = json.loads(weights_file.read_text())["content"]
        return Policy(weights=c["weights"], vetoes=c["vetoes"],
                      theta=c["thresholds"]["theta"], delta=c["thresholds"]["delta"])


@dataclass(frozen=True)
class Scored:
    candidate: Candidate
    score: float
    evidence: dict
    vetoed: bool = False
    reason: str = ""


def policy_axes(policy: "Policy") -> list:
    """The evidence axes this policy actually uses (its declared weight keys)."""
    return list(policy.weights.keys())


# ── Evidence axes (each 0..1) ─────────────────────────────────────────────────
def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").lower()).strip()


def _tokens(s: str) -> frozenset:
    return frozenset(t for t in _WS.split((s or "").lower().strip()) if t)


def _norm_ids(d: dict | None) -> dict:
    return {str(k).lower(): str(v).strip().lower()
            for k, v in (d or {}).items() if v not in (None, "", "null")}


def _identifier_conflict(a: dict, b: dict) -> bool:
    na, nb = _norm_ids(a), _norm_ids(b)
    return any(na[k] != nb[k] for k in na.keys() & nb.keys())


@evidence_axis("id")
def _e_id(c: Candidate, x: Context) -> float:
    na, nb = _norm_ids(c.identifiers), _norm_ids(x.identifiers)
    return 1.0 if any(na[k] == nb[k] for k in na.keys() & nb.keys()) else 0.0


@evidence_axis("exact")
def _e_exact(c: Candidate, x: Context) -> float:
    return 1.0 if c.name and _norm(c.name) == _norm(x.form) else 0.0


@evidence_axis("loose")
def _e_loose(c: Candidate, x: Context) -> float:
    tc, tf = _tokens(c.name), _tokens(x.form)
    if not tc or not tf:
        return 0.0
    return 1.0 if (tc <= tf or tf <= tc) else 0.0


@evidence_axis("register")
def _e_register(c: Candidate, x: Context) -> float:
    if not x.register_hint:
        return 0.5                                  # no signal → neutral
    return 1.0 if _norm(c.register) == _norm(x.register_hint) else 0.0


@evidence_axis("domain")
def _e_domain(c: Candidate, x: Context) -> float:
    if not x.domain:
        return 0.5
    if c.domain is None:
        return 0.5                                  # general sense → neutral
    return 1.0 if _norm(c.domain) == _norm(x.domain) else 0.0


@evidence_axis("collocation")
def _e_collocation(c: Candidate, x: Context) -> float:
    if not c.relation_addresses or not x.nearby_addresses:
        return 0.0
    hit = len(c.relation_addresses & x.nearby_addresses)
    return hit / len(c.relation_addresses)          # fraction of neighbours present


@evidence_axis("frequency")
def _e_frequency(c: Candidate, x: Context) -> float:
    # Dominant-sense prior: WordNet orders a word's senses by decreasing frequency
    # (sense 1 = most common). c.freq = 1/sense_rank for THIS form, so the dominant
    # sense scores 1.0 and rarer senses decay (1/2, 1/3, …). This is the
    # most-frequent-sense baseline — what to default to ABSENT other evidence; the
    # domain/collocation/prior axes override it when context actually fires. Already
    # in [0,1]; 0.0 when the binding has no frequency (non-lexical / unranked).
    return max(0.0, min(1.0, float(c.freq)))


@evidence_axis("pin")
def _e_pin(c: Candidate, x: Context) -> float:
    # Domain-conditional prior: a curated/domain pack asserts THIS sense for the form
    # in this context (admit→concede in legal, circumvent→evade in FOIA). Conditioned
    # on KNOWN context, so it carries more weight than the unconditional `frequency`
    # default — its policy weight sits above all soft evidence, below only the hard
    # `id`. A pin only fires when explicitly supplied; 0 otherwise (no effect on the
    # frequency/feedback path). Value is the pin strength in [0,1]; authority is the
    # weight.
    return max(0.0, min(1.0, float(x.pins.get(c.address, 0.0))))


@evidence_axis("initial_match")
def _e_initial_match(c: Candidate, x: Context) -> float:
    # Alias evidence: the form is the INITIALS/acronym of a candidate's name
    # ("AA" → "Alex Acme", "JD" → "Jane Doe"). Weak ALONE by design
    # — the alias policy weights it below θ so it only collapses when corroborated
    # (co-occurrence via `prior`). Fires only for short forms (≤4 letters).
    letters = re.sub(r"[^a-z]", "", (x.form or "").lower())
    if not letters or len(letters) > 4 or not c.name:
        return 0.0
    initials = "".join(t[0] for t in _WS.split(c.name.lower().strip()) if t)
    return 1.0 if letters == initials else 0.0


@evidence_axis("prior")
def _e_prior(c: Candidate, x: Context) -> float:
    return float(x.prior.get(c.address, 0.0))


@evidence_axis("contributor_trust")
def _e_contributor_trust(c: Candidate, x: Context) -> float:
    # Open-contribution gate: evidence sourced from a low-trust contributor
    # (anonymous, or one whose docs keep flagging off-topic) carries less
    # weight. Neutral (0.5) when unknown so legacy/system evidence is unaffected.
    return max(0.0, min(1.0, float(x.contributor_weight)))


@veto_rule("identifier_conflict")
def _v_identifier_conflict(c: Candidate, x: Context) -> bool:
    return _identifier_conflict(c.identifiers, x.identifiers)


# ── The engine ────────────────────────────────────────────────────────────────
def score(c: Candidate, x: Context, policy: Policy) -> Scored:
    # vetoes the policy has enabled (registry-driven, not hardcoded)
    for name, enabled in policy.vetoes.items():
        if enabled and VETO_FNS.get(name) and VETO_FNS[name](c, x):
            return Scored(c, NEG_INF, {}, vetoed=True, reason=name)
    # weighted sum over exactly the axes the policy names
    e = {}
    s = 0.0
    for axis, w in policy.weights.items():
        fn = EVIDENCE_FNS.get(axis)
        if fn is None:
            continue  # policy named an axis with no registered function → ignored
        e[axis] = fn(c, x)
        s += w * e[axis]
    return Scored(c, s, e)


@dataclass
class CollapseResult:
    action: str                 # "collapse" | "park"
    chosen: Scored | None
    margin: float
    reason: str
    ranked: list               # list[Scored], live candidates by score desc


def collapse(candidates: list[Candidate], x: Context, policy: Policy) -> CollapseResult:
    scored = [score(c, x, policy) for c in candidates]
    live = sorted((s for s in scored if not s.vetoed),
                  key=lambda s: (-s.score, s.candidate.address))  # deterministic tiebreak
    if not live:
        return CollapseResult("park", None, 0.0, "all_vetoed", scored)
    top = live[0]
    second = live[1].score if len(live) > 1 else NEG_INF
    margin = top.score - second
    if top.score >= policy.theta and margin >= policy.delta:
        return CollapseResult("collapse", top, margin, "confident", live)
    return CollapseResult("park", None, margin if margin != math.inf else 0.0,
                          "ambiguous", live)
