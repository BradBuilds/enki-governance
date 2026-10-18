"""
Resolver runner — orchestrates entity/place/topic resolution for one
envelope.

Called by core.extraction.runner at the resolution stage, replacing the
Phase-2 stub. Idempotent: re-running on the same envelope does not
create duplicate rows because every individual resolver uses either a
UNIQUE constraint or a lookup-before-insert pattern.

Returns a summary dict the runner folds into its provenance pass log.
"""

from __future__ import annotations

import logging
from typing import Optional

from core.extraction.resolvers.entity_resolver import resolve_entities
from core.extraction.resolvers.place_resolver import resolve_places
from core.extraction.resolvers.topic_resolver import resolve_topics

log = logging.getLogger("enki.extraction.resolvers")


def resolve_all(
    envelope: dict,
    conn,
    *,
    tenant_id: str = "default",
    ldu_t_id: Optional[int] = None,
) -> dict:
    """Run all three resolvers against one envelope. Mutates envelope
    in place (stamps node_ids, writes resolved_places).

    Returns:
        {
            "entities": {"resolved": n, "created": n, "skipped": n, "failed": n},
            "places":   {...},
            "topics":   {...},
            "fields_filled": [list of envelope paths touched],
        }
    """
    ent_stats   = resolve_entities(envelope, conn, tenant_id=tenant_id, ldu_t_id=ldu_t_id)
    place_stats = resolve_places(envelope,   conn, tenant_id=tenant_id, ldu_t_id=ldu_t_id)
    topic_stats = resolve_topics(envelope,   conn, tenant_id=tenant_id, ldu_t_id=ldu_t_id)

    fields: list[str] = []
    if ent_stats["resolved"]:
        fields.append("entities[].node_id")
    if place_stats["resolved"]:
        fields.append("spatial.resolved_places")
    if topic_stats["resolved"]:
        fields.append("topics[].node_id")

    return {
        "entities":      ent_stats,
        "places":        place_stats,
        "topics":        topic_stats,
        "fields_filled": fields,
    }
