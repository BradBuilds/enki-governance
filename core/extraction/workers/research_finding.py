"""
ResearchFindingWorker — pass-through worker for synthesized research
artifacts.

Unlike the article or seismic workers (which unpack raw connector
payloads into envelopes), research findings arrive WITH the envelope
already synthesized by the orchestrator (term_research, story_research,
watch backfill). The worker's job is therefore minimal:

    - cheap_pass: stamp identity / summary / temporal / node_id from
      whatever the orchestrator pre-populated in signal.payload.
      Every field we copy is treated as authoritative — LLM already
      produced the finding, we're not re-extracting from raw text.

    - deep_pass: no-op. The orchestrator already ran its own Gemini /
      Claude synthesis; re-running the universal deep extractors would
      waste tokens and potentially overwrite careful outputs.

Everything downstream (resolver, reality-check, completeness, store,
fanout) runs normally — which is the whole point of routing research
through the universal runner. Entity resolution dedupes across feed
and research. Reality check validates every research entity. Fanout
attributes research artifacts to matching watch topics.

Registered for signal.intelligence.research_finding (existing in
signalvocab) and signal.intelligence.osint (aliased — OSINT results
have the same shape).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from core.extraction.base import ExtractionWorker
from core.extraction.envelope import record_pass, set_path
from core.extraction.registry import register_worker

log = logging.getLogger("enki.extraction.workers.research_finding")


_COPY_PATHS = (
    "node_id",
    "node_type",
    "subtype",
    "identity.title_raw",
    "identity.title_display",
    "identity.subtitle",
    "identity.canonical_name",
    "identity.aliases",
    "summary.headline",
    "summary.narrative",
    "summary.key_facts",
    "summary.quotes",
    "summary.structured_fields",
    "temporal.event_time",
    "temporal.precision",
    "spatial.coordinates",
    "spatial.place_text",
    "source.url",
    "source.outlet_name",
    "source.language_declared",
    "source.language_detected",
    "source.language_displayed",
    "source.editorial_stance",
    "evidence.epistemic_type",
    "evidence.confidence",
    "evidence.background_relevance",
    "media.thumbnail",
    "relationships.follows",
    "relationships.part_of",
    "relationships.mentions",
    "relationships.cites",
    "relationships.custom",
)


def _get(d: dict, path: str, default=None):
    cur: Any = d
    for seg in path.split("."):
        if not isinstance(cur, dict) or seg not in cur:
            return default
        cur = cur[seg]
    return cur


@register_worker(
    "signal.intelligence.research_finding",
    "signal.intelligence.osint",
)
class ResearchFindingWorker(ExtractionWorker):

    signal_type = "signal.intelligence.research_finding"
    aliases = ("signal.intelligence.osint",)

    def cheap_pass(self, signal: dict, envelope: dict) -> dict:
        t0 = time.monotonic()
        payload = signal.get("payload") or {}

        # The orchestrator should pass a 'prebuilt_envelope' under payload
        # with the synthesis results. Any pre-filled field is copied into
        # the fresh envelope built by the runner so downstream stages see
        # exactly what the orchestrator intended.
        prebuilt = payload.get("prebuilt_envelope") or {}
        filled: list[str] = []

        for path in _COPY_PATHS:
            value = _get(prebuilt, path)
            if value is None or value == "" or value == [] or value == {}:
                continue
            set_path(envelope, path, value)
            filled.append(path)

        # Entities / topics — copy arrays wholesale. Resolver will
        # dedupe and stamp node_ids.
        ents = prebuilt.get("entities") or []
        if isinstance(ents, list) and ents:
            envelope["entities"] = ents
            filled.append("entities")
        topics = prebuilt.get("topics") or []
        if isinstance(topics, list) and topics:
            envelope["topics"] = topics
            filled.append("topics")

        # Source of record for research findings — always AI + public
        # sources unless orchestrator overrode it.
        if not envelope.get("source", {}).get("outlet_name"):
            set_path(envelope, "source.outlet_name", "Research synthesis")
            filled.append("source.outlet_name")
        if not envelope.get("evidence", {}).get("epistemic_type"):
            # node_type=assessment/subtype=question|claim|fact carries the
            # epistemic flavor via subtype; evidence.epistemic_type is
            # kept at the default "observation" for now.
            pass

        # Fill in card actions so the UI can render "Re-research", "View sources"
        envelope["card_actions"] = prebuilt.get("card_actions") or [
            {"label": "Re-research", "action": "research.term"},
        ]

        dur_ms = int((time.monotonic() - t0) * 1000)
        record_pass(
            envelope, stage="cheap",
            fields_filled=filled,
            duration_ms=dur_ms,
            success=True,
            prompt_version="research_finding_cheap_v1",
        )
        return envelope

    def deep_pass(self, signal: dict, envelope: dict,
                  llm: Optional[Any] = None) -> dict:
        """Deliberate no-op — research orchestrators own LLM synthesis."""
        record_pass(
            envelope, stage="deep",
            fields_filled=[],
            duration_ms=0,
            success=True,
            prompt_version="research_finding_deep_noop",
            error=None,
        )
        return envelope
