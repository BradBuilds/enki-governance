"""
Worker registry — strict signal_type → worker class lookup.

Registration happens at import time via the @register_worker decorator.
Lookups validate the requested type against the canonical signalvocab
(loaded via core.gdu.signal_vocabulary) so workers can only claim types
the GDU has admitted.

The runner uses this registry as the ONLY dispatch point. There is no
content-sniffing fallback — if a signal arrives with a type that has
no registered worker, the runner logs a no_worker_for_type failure and
returns the envelope unchanged. That visibility is the feature: it
tells us which vocab types still need workers built.
"""

from __future__ import annotations

import logging
from typing import Optional, Type

from core.extraction.base import ExtractionWorker

log = logging.getLogger("enki.extraction.registry")

# signal_type string → worker class
_REGISTRY: dict[str, Type[ExtractionWorker]] = {}


def register_worker(signal_type: str, *aliases: str):
    """Class decorator. Registers a worker class under its signal_type
    and any aliases. The worker's class-level `signal_type` field is
    set to the canonical type id (the first argument), and `aliases`
    is set to the tuple of additional types.

    Usage:
        @register_worker("signal.content.news_article",
                         "signal.content.rss_entry")
        class ArticleWorker(ExtractionWorker):
            ...

    Registration is validated against the canonical signalvocab. If the
    GDU context is degraded (non-canonical) the registration is still
    allowed so dev environments can iterate, but a warning is logged.
    """

    def _decorator(cls: Type[ExtractionWorker]) -> Type[ExtractionWorker]:
        cls.signal_type = signal_type
        cls.aliases = tuple(aliases)

        # Validate against canonical signalvocab if reachable.
        try:
            from core.gdu.signal_vocabulary import (
                get_signal_types,
                is_canonical,
            )
            valid_types = get_signal_types()
            if not is_canonical():
                log.warning(
                    "registering worker %s under degraded GDU "
                    "(signalvocab non-canonical) — type=%s",
                    cls.__name__, signal_type,
                )
            for tid in (signal_type, *aliases):
                if valid_types and tid not in valid_types:
                    log.warning(
                        "worker %s claims signal_type %r which is NOT in "
                        "the canonical signalvocab — registration refused",
                        cls.__name__, tid,
                    )
                    return cls  # refuse without crashing import
        except Exception as e:  # governance unreachable — permit + warn
            log.warning(
                "signalvocab unreachable during registration of %s: %s",
                cls.__name__, e,
            )

        for tid in (signal_type, *aliases):
            if tid in _REGISTRY and _REGISTRY[tid] is not cls:
                log.warning(
                    "signal_type %r already registered by %s; overriding with %s",
                    tid, _REGISTRY[tid].__name__, cls.__name__,
                )
            _REGISTRY[tid] = cls

        log.debug(
            "registered worker %s for %s (+%d aliases)",
            cls.__name__, signal_type, len(aliases),
        )
        return cls

    return _decorator


def get_worker(signal_type: str) -> Optional[ExtractionWorker]:
    """Return an instance of the worker registered for this signal_type,
    or None if no worker is registered. New instance per call — workers
    should be stateless, but we don't memoize in case a worker ever
    carries per-call state."""
    cls = _REGISTRY.get(signal_type)
    if cls is None:
        return None
    return cls()


def registered_types() -> list[str]:
    """Return the sorted list of signal_types with registered workers."""
    return sorted(_REGISTRY.keys())
