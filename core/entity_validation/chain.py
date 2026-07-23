"""The validator chain — walks providers in priority order."""
from __future__ import annotations

import logging
from typing import Optional

from core.entity_validation.filter import filter_candidate
from core.entity_validation.provider import Provider
from core.entity_validation.types import Candidate, CandidateDecision, Match

log = logging.getLogger("enki.validation.chain")


class ValidatorChain:
    """Walks providers in priority order. First match wins.

    Re-entrant / thread-safe: providers hold no shared mutable state.
    Internal cache optimization lives separately (Phase 5C).
    """

    def __init__(self, providers: list[Provider]):
        self.providers = sorted(providers, key=lambda p: p.priority)

    def resolve(self, cand: Candidate) -> tuple[Optional[Match], Optional[CandidateDecision]]:
        """Run the filter + provider chain.

        Returns (match, decision):
          - (match, None) — a provider confirmed the candidate
          - (None, decision) — filter rejected it (with reason + optional
            identifier side-channel)
          - (None, None) — filter passed but no provider matched
        """
        decision = filter_candidate(cand)
        if not decision.accept:
            return None, decision

        for provider in self.providers:
            if not provider.supports(cand.kind):
                continue
            try:
                m = provider.validate(cand)
            except Exception as e:
                log.warning("provider %s raised on %r: %s",
                            provider.name, cand.raw_text, e)
                continue
            if m is not None:
                return m, None

        return None, None


_default: Optional[ValidatorChain] = None


def default_chain() -> ValidatorChain:
    """Lazily-constructed chain using the default provider set.

    Order (by priority number, lower = earlier):
      10  Wikidata   — structured, authoritative, broadest coverage
      15  GeoNames   — definitive for places (straits, airports, etc.);
                       requires GEONAMES_USERNAME env var, skips silently
                       if unset
      20  Wikipedia  — narrative + thumbnail enrichment; secondary source

    Future domain-specific providers (OpenCorporates, GLEIF, MITRE ATT&CK,
    OurAirports) slot in at their own priority — lower numbers for more
    authoritative or more specific sources."""
    global _default
    if _default is not None:
        return _default

    providers: list[Provider] = []
    try:
        from core.entity_validation.providers.wikidata import WikidataProvider
        providers.append(WikidataProvider())
    except Exception as e:
        log.warning("wikidata provider unavailable: %s", e)

    try:
        from core.entity_validation.providers.geonames import GeoNamesProvider
        providers.append(GeoNamesProvider())
    except Exception as e:
        log.warning("geonames provider unavailable: %s", e)

    try:
        from core.entity_validation.providers.wikipedia import WikipediaProvider
        providers.append(WikipediaProvider())
    except Exception as e:
        log.warning("wikipedia provider unavailable: %s", e)

    _default = ValidatorChain(providers)
    return _default
