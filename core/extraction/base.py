"""
ExtractionWorker — abstract base for per-signal-type extraction.

A worker is the Layer-1 adapter that knows how to translate ONE specific
signal_type's payload shape into the canonical envelope. It does not do
the intelligence work itself — that's delegated to the universal
extractors in core.extraction.extractors. The worker's job is:

    1. Unpack the signal payload into identity/source/temporal/spatial/
       media/summary fields on the envelope.
    2. Assemble the text bundle (title + summary + body) that the
       universal extractors will analyze.
    3. Call the universal extractors that are SEMANTICALLY APPLICABLE to
       this signal type. A news article calls entity/place/topic/quote/
       narrative extractors. A seismic event would call place only.
       A flight position report would call none — its fields are
       kinematic, not textual.

Idempotency contract: re-running `cheap_pass` or `deep_pass` on the same
signal with the same envelope state MUST produce the same envelope. For
rule-based extractors this is automatic. For LLM deep passes it is
enforced by temperature=0 and deterministic prompt versions.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Optional


class ExtractionWorker(ABC):
    """Abstract base for signal-type-specific extraction workers.

    Subclasses MUST declare `signal_type` as a class variable and decorate
    themselves with @register_worker(signal_type) to be discoverable by
    the runner.

    Subclasses MAY declare `aliases` — additional signal_type strings
    that resolve to the same worker (e.g. the article worker handles
    both 'signal.content.news_article' and 'signal.content.rss_entry'
    because both carry the same semantic content shape).
    """

    signal_type: ClassVar[str] = ""
    aliases: ClassVar[tuple[str, ...]] = ()

    @abstractmethod
    def cheap_pass(self, signal: dict, envelope: dict) -> dict:
        """Rule-based extraction — runs on every signal.

        Must be fast (no network, no LLM), deterministic, and side-effect
        free beyond mutating the passed-in envelope. Returns the envelope
        for fluent use.

        Args:
            signal:   the raw signal dict (with type, payload, source,
                      and optional signal_rid)
            envelope: the envelope being built — may already have fields
                      populated by a prior stage (e.g. connector)

        Returns:
            The (mutated) envelope.
        """
        ...

    @abstractmethod
    def deep_pass(
        self,
        signal: dict,
        envelope: dict,
        llm: Optional[Any] = None,
    ) -> dict:
        """LLM-backed extraction — runs only when the gate opens.

        Gate conditions (evaluated by the runner, not by the worker):
            - signal matches a designated target
            - cheap-pass metadata was garbage (e.g. HN title "Show HN: N...")
            - language is new for this entity or this worker's expected
              language

        Phase 2 stubs this — deep methods should detect llm=None, record
        a no-op pass with error='llm_unavailable', and return the
        envelope unchanged. Phase 4 wires Gemma through `llm`.

        Args:
            signal:   raw signal dict
            envelope: envelope that already ran through cheap_pass
            llm:      optional LLM client implementing the .extract()
                      protocol — None in Phase 2

        Returns:
            The (mutated) envelope.
        """
        ...
