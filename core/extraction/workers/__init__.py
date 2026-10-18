"""Concrete extraction workers.

Two kinds of workers live here:

1. Hand-written Python workers — e.g. article.py — for semantically
   complex cases that benefit from explicit code (garbage-title deep-
   pass gating, sentence-level narrative synthesis, etc.).

2. Declarative mapping workers — loaded from YAML files under
   workers/mappings/ (hand-written) and storage/mappings/learned/
   (auto-learned and promoted). All of these run through the single
   DeclarativeWorker engine. This is the scalable path: new signal
   types get coverage by writing or learning a YAML file, not by
   writing Python.

This module does the registration bootstrap:
    - article.py self-registers on import (Python worker)
    - every discovered YAML mapping is wrapped in a DeclarativeWorker
      instance and registered via the same registry

The registry is the single source of truth. The runner doesn't know
or care whether a worker came from code or a YAML file.
"""

from __future__ import annotations

import logging

from core.extraction.mappings import discover_mappings
from core.extraction.registry import _REGISTRY  # noqa: F401 — explicit insertion
from core.extraction.workers import article  # noqa: F401 — side-effect registration
from core.extraction.workers import research_finding  # noqa: F401 — side-effect registration
from core.extraction.workers.declarative import DeclarativeWorker

log = logging.getLogger("enki.extraction.workers")


def _register_declarative_mappings() -> int:
    """Load every YAML mapping file and install a DeclarativeWorker for
    each. Returns the number of mappings registered.

    Conflicts: if a signal_type is already registered (e.g. by a Python
    worker like article.py), a YAML mapping for the same type logs a
    warning and does NOT override — Python workers win over YAML for
    hand-crafted semantics. Override order is intentional.
    """
    mappings = discover_mappings()
    count = 0

    for mdoc in mappings:
        worker_instance = DeclarativeWorker(mdoc)

        # Bind this instance's class to a unique subclass so the registry's
        # "one class per type" expectation holds. In practice we store the
        # instance-factory directly into the registry dict so each type
        # gets a fresh DeclarativeWorker with its own mapping.
        for type_id in mdoc.all_types():
            if type_id in _REGISTRY:
                existing = _REGISTRY[type_id]
                log.info(
                    "YAML mapping for %s (%s) deferring to existing worker %s",
                    type_id, mdoc.source_path, getattr(existing, "__name__", existing),
                )
                continue

            # Wrap in a lambda-class so get_worker() can instantiate it.
            # We use a closure factory instead of subclassing to avoid
            # dynamic class pollution.
            class _BoundDeclarative(DeclarativeWorker):
                signal_type = type_id
                aliases = mdoc.aliases
                _bound_mapping = mdoc

                def __init__(self):
                    super().__init__(self._bound_mapping)

            _BoundDeclarative.__name__ = f"Declarative_{type_id.replace('.', '_')}"
            _REGISTRY[type_id] = _BoundDeclarative
            count += 1
            log.debug(
                "registered declarative worker for %s from %s",
                type_id, mdoc.source_path,
            )

    if count:
        log.info("registered %d declarative mapping workers", count)
    return count


_register_declarative_mappings()
