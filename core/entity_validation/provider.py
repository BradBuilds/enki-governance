"""Provider interface.

Each external validation source (Wikidata, Wikipedia, GeoNames, …)
implements Provider. The chain walks providers in priority order and
returns the first Match.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional

from core.entity_validation.types import Candidate, Match

log = logging.getLogger("enki.validation.provider")


class Provider(ABC):
    """One validation source."""

    # Human-readable identifier used on the validated_by_source column
    name: str = "abstract"

    # Which Candidate.kind values this provider handles. Use "*" to accept all.
    supports_kinds: tuple[str, ...] = ("entity", "place", "topic")

    # Timeout seconds for the network call(s) this provider makes
    timeout_seconds: float = 10.0

    # Lower number = runs earlier in the chain
    priority: int = 100

    def supports(self, kind: str) -> bool:
        return "*" in self.supports_kinds or kind in self.supports_kinds

    @abstractmethod
    def validate(self, candidate: Candidate) -> Optional[Match]:
        """Return a Match on success, None on no-result.

        Raise only for unexpected failures (network, auth) — the chain
        will log + move to the next provider. Do NOT raise for 'no match
        found'; return None.
        """
        ...
