"""
Universal Extraction Pipeline — Phase 2.

Turns any signal into a fully populated envelope through a worker framework
keyed strictly by the already-stamped signal_type. Upstream ingestion
(connectors + parser registry) is responsible for type recognition; this
package is responsible for extraction into the canonical envelope.

Public API:
    from core.extraction import run_extraction, ExtractionWorker, register_worker

    envelope = run_extraction(signal_dict)   # returns populated envelope

Architecture:
    core.extraction.base         — ExtractionWorker ABC
    core.extraction.envelope     — envelope build/get/set/validate helpers
    core.extraction.registry     — signal_type → worker class lookup
    core.extraction.runner       — orchestrator (cheap → gate → deep → log)
    core.extraction.extractors   — universal extractors (entity, place, topic,
                                   quote, narrative, garbage detection)
    core.extraction.workers      — concrete per-signal-type workers

The intelligence lives in the universal extractors. Workers are thin: they
translate a signal payload into the envelope shape and decide which
extractors are semantically applicable to their signal type.
"""

from core.extraction.base import ExtractionWorker
from core.extraction.registry import register_worker, get_worker
from core.extraction.runner import run_extraction

# Importing workers triggers their @register_worker side-effects so the
# registry is populated at package import time.
from core.extraction.workers import article  # noqa: F401

__all__ = [
    "ExtractionWorker",
    "register_worker",
    "get_worker",
    "run_extraction",
]
