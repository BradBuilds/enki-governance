"""
Extraction mappings — declarative payload→envelope rules.

A mapping is a YAML file (or dict) that tells a DeclarativeWorker how to
turn a specific signal_type's payload into the canonical envelope. It is
NOT a GDU pack and does NOT live under libraries/. Mappings reference
envelope vocabulary (field paths) but they do not define vocabulary.

Isolation from GDU:
    - Mappings are loaded by this module, NOT by core.gdu.runtime
    - Mappings never touch libraries/
    - Mappings never modify the active_set or any pack
    - Mappings ARE stamped with LDU lineage (the active_set version that
      was admitted when the mapping was loaded) so we can audit which
      rule version was in effect at any point in time
    - If a future active_set rejects a previously-valid mapping (e.g.
      the referenced signal_type is removed), the loader refuses to
      register it, logs the reason, and leaves the fallback
      no_worker_for_type path in place

Two load paths:
    1. Hand-written mappings under core/extraction/workers/mappings/*.yaml
       — shipped with releases, version-controlled, reviewed in git
    2. Learned mappings exported to storage/mappings/learned/*.yaml
       — deployment-local, gitignored, written by the promoter after
       a learned candidate passes review

Both paths converge here. The runtime sees no difference between a
hand-written and a learned mapping — they execute under the same engine.
"""

from core.extraction.mappings.loader import (
    MappingDoc,
    load_mapping_file,
    load_mapping_dir,
    discover_mappings,
)

__all__ = [
    "MappingDoc",
    "load_mapping_file",
    "load_mapping_dir",
    "discover_mappings",
]
