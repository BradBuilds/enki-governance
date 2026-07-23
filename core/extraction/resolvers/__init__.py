"""
Resolvers — dedupe envelope items against the master tables.

Phase 3 of the extraction pipeline. After a worker has populated an
envelope with entity mentions, place_text, and topic labels, the
resolvers:

    1. Look up each item in its master table (entities, places, topics)
    2. If found: increment activity counters, return existing node_id
    3. If not found: INSERT a new row (LDU-stamped) and return the new id
    4. Stamp the envelope item with node_id = "entity:<id>" /
       "place:<id>" / "topic:<id>"

Resolvers never modify envelope content beyond stamping node_ids. They
never create knowledge_nodes (that's Phase 5, the auto-node hook).
They never touch GDU packs.

Resolution is idempotent: running resolve_all twice on the same envelope
produces the same node_ids and does not insert duplicates. The natural
dedup keys are:
    - entities: (tenant_id, identifier_hash)  UNIQUE
    - places:   (tenant_id, normalized_name)  — soft dedup, no constraint
    - topics:   (tenant_id, normalized_name)  — soft dedup, no constraint

Failures per-item are isolated: one bad entity row does not abort the
rest of the resolution pass. Each resolver logs its own failures.
"""

from core.extraction.resolvers.runner import resolve_all
from core.extraction.resolvers.entity_resolver import resolve_entities
from core.extraction.resolvers.place_resolver import resolve_places
from core.extraction.resolvers.topic_resolver import resolve_topics

__all__ = [
    "resolve_all",
    "resolve_entities",
    "resolve_places",
    "resolve_topics",
]
