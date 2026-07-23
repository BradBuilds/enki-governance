"""Entity validation — Phase 5A.

Every candidate surfaced by extraction walks a provider chain before being
materialized as a node. Each provider attempts to confirm the candidate
against an authoritative source (Wikidata, Wikipedia, GeoNames, domain
registries). First provider to match wins; the match brings canonical
name + type + source attribution + confidence.
"""
from core.entity_validation.types import Candidate, CandidateDecision, Match
from core.entity_validation.filter import filter_candidate
from core.entity_validation.provider import Provider
from core.entity_validation.chain import ValidatorChain, default_chain

__all__ = [
    "Candidate", "CandidateDecision", "Match",
    "Provider", "ValidatorChain", "default_chain",
    "filter_candidate",
]
