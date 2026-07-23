"""
Event spawner — convert action statements into canonical event rows.

A statement like "Baldwin had lunch with McCone at the Pentagon on
1961-08-29" carries:
  - participants (Baldwin, McCone)
  - location (Pentagon)
  - event_time (1961-08-29)
  - predicate (had → had_meeting_with)

This module looks at every promoted statement, decides whether its
predicate belongs to an action class that warrants an event row
(meetings, travel, transactions, statements, sightings, etc.), and
either INSERTS a new event or merges into an existing one with the
same (participants, time-bucket, location).

The events table already exists with the right columns (event_time,
event_end_time, location_entity_id, actor_entity_ids[],
originating_statement_ids[], temporal_precision). We're just wiring
the statement→event population path.

Why events, not just edges:
  - Edges are timeless. Events are time- and place-anchored.
  - Multiple statements about the same encounter collapse into ONE
    event (e.g. 3 docs describing the same meeting).
  - Timelines + maps are queries OVER events.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from core.gdu.address import assign_address
from storage.postgres.db import get_connection

log = logging.getLogger("enki.documents.event_spawner")


# Predicate (edge_type) → event subtype map.
# Determines which edge types graduate to event rows. Other predicates
# (alleged, accused, owns, member_of) are edges only, not events.
_PREDICATE_TO_EVENT_SUBTYPE = {
    "met_with":              "meeting",
    "had_meeting_with":      "meeting",
    "communicated_with":     "statement",
    "flew_with":             "departure",
    "traveled_with":         "departure",
    "accompanied":           "departure",
    "departed_from":         "departure",
    "traveled_from":         "departure",   # PNR / GDS booking record
    "arrived_at":            "arrival",
    "traveled_to":           "arrival",
    "visited":               "arrival",
    "attended":              "meeting",
    "hosted":                "meeting",
    "participated_in":       "meeting",
    "paid":                  "transaction",
    "paid_fare":             "transaction", # PNR / fare audit lines
    "received_from":         "transaction",
    "transferred_to":        "transaction",
    "donor_to":              "transaction",
    "contracted_with":       "contract",
    "appointed":             "appointment",
    "spouse_of":             None,        # relational, not event
    "child_of":              None,
    "parent_of_person":      None,
    "owns":                  None,
    "owned_by":              None,
    "officer_of":            None,
    "founded_by":            "appointment",
    "testified_against":     "statement",
    "witnessed":             "sighting",
}


def _time_bucket(dt: Optional[datetime], precision: str = "day") -> Optional[datetime]:
    """Round event_time to the nearest day so two statements asserting
    the same day collapse into one event."""
    if dt is None:
        return None
    if precision == "day":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    return dt


def _find_or_create_event(
    *,
    tenant_id: str,
    event_subtype: str,
    participants: list[int],
    event_time: Optional[datetime],
    location_entity_id: Optional[int],
    statement_id: int,
    confidence: float,
) -> tuple[int, bool]:
    """Returns (event_id, is_new)."""
    if not participants:
        return None, False

    sorted_participants = sorted(participants)
    bucket = _time_bucket(event_time, "day")

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Look for existing event with same actor set + same day +
            # same location.
            cur.execute(
                """
                SELECT event_id, originating_statement_ids
                  FROM events
                 WHERE event_type = %s
                   AND actor_entity_ids @> %s::bigint[]
                   AND actor_entity_ids <@ %s::bigint[]
                   AND (
                     (event_time IS NULL AND %s IS NULL) OR
                     (DATE_TRUNC('day', event_time) = DATE_TRUNC('day', %s::timestamptz))
                   )
                   AND (
                     (location_entity_id IS NULL AND %s IS NULL) OR
                     (location_entity_id = %s)
                   )
                 LIMIT 1
                """,
                (event_subtype, sorted_participants, sorted_participants,
                 bucket, bucket, location_entity_id, location_entity_id),
            )
            row = cur.fetchone()
            if row:
                event_id, existing_ids = row
                existing = list(existing_ids or [])
                if statement_id not in existing:
                    existing.append(statement_id)
                    cur.execute(
                        """
                        UPDATE events
                           SET originating_statement_ids = %s,
                               source_count = ARRAY_LENGTH(%s, 1),
                               updated_at = NOW(),
                               confidence = GREATEST(COALESCE(confidence, 0), %s)
                         WHERE event_id = %s
                        """,
                        (existing, existing, confidence, event_id),
                    )
                    conn.commit()
                return event_id, False

            # Mint a new event. Legacy events.node_id column is NOT NULL
            # and represents the old LDU dot-string address; we reuse
            # the new base60_address as its value so the column has a
            # canonical, addressable value.
            addr = assign_address(conn, "event", event_subtype)
            if addr is None:
                # nodevocab has no slot for ('event', event_subtype). Skip
                # rather than insert a canonical row the future events
                # governance trigger will reject. Caller can re-emit with
                # a recognized subtype.
                return None, False
            # Compose a human-readable title from participant names. Verb
            # comes from event_subtype, names from canonical_name lookup.
            # Falls back to the generic "{verb} of N entities" only if no
            # names are resolvable.
            cur.execute(
                "SELECT entity_id, canonical_name FROM entities WHERE entity_id = ANY(%s)",
                (sorted_participants,),
            )
            name_by_id = {r[0]: r[1] for r in cur.fetchall()}
            names = [name_by_id.get(pid) for pid in sorted_participants if name_by_id.get(pid)]
            if names:
                verb = event_subtype.replace("_", " ")
                if len(names) == 1:
                    title = f"{verb}: {names[0]}"
                elif len(names) == 2:
                    title = f"{verb}: {names[0]} & {names[1]}"
                else:
                    title = f"{verb}: {', '.join(names[:-1])} & {names[-1]}"
                # Soft cap to keep card layouts sane
                if len(title) > 180:
                    title = title[:177] + "…"
            else:
                title = f"{event_subtype} of {len(sorted_participants)} entities"
            cur.execute(
                """
                INSERT INTO events
                    (tenant_id, node_id, event_type, title, event_time, temporal_precision,
                     actor_entity_ids, location_entity_id,
                     originating_statement_ids, source_count,
                     confidence, base60_address, gdu_canonical)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                RETURNING event_id
                """,
                (tenant_id, addr, event_subtype,
                 title,
                 bucket, "day" if bucket else None,
                 sorted_participants, location_entity_id,
                 [statement_id], 1, confidence, addr),
            )
            event_id = cur.fetchone()[0]
            conn.commit()
            return event_id, True


def spawn_from_statement(statement_id: int) -> Optional[dict]:
    """Spawn (or update) an event row from one statement. Returns
    dict with event_id + is_new, or None if statement doesn't warrant
    an event."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tenant_id, speaker_entity_id, object_entity_id,
                       predicate, event_time, event_location_entity_id,
                       confidence
                  FROM statements
                 WHERE statement_id = %s AND validation_status = 'promoted'
                """,
                (statement_id,),
            )
            row = cur.fetchone()
    if not row:
        return None
    t_id, subj, obj, predicate, evt_time, loc_id, conf = row

    # Resolve predicate → event subtype.
    #
    # Two paths:
    #   1. Direct match — the predicate IS already a canonical edge_type
    #      we know about (PNR / domain extractors emit predicates by name,
    #      e.g. 'traveled_from', 'paid_fare'). This is the fast path and
    #      doesn't require a predicate_dictionary row.
    #   2. Verb lookup — the predicate is a natural-language verb the
    #      generic LLM extractor surfaced (e.g. 'flew', 'met'). We resolve
    #      it through predicate_dictionary to its canonical edge_type
    #      first, then map.
    #
    # Direct match wins; the LLM-verb path is the fallback. Either way
    # the final answer must be in _PREDICATE_TO_EVENT_SUBTYPE — otherwise
    # the statement doesn't graduate to an event.
    event_subtype = _PREDICATE_TO_EVENT_SUBTYPE.get(predicate)
    if event_subtype is None:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT edge_type FROM predicate_dictionary
                     WHERE canonical_lemma = %s ORDER BY confidence DESC LIMIT 1
                    """,
                    (predicate,),
                )
                r = cur.fetchone()
                if r:
                    event_subtype = _PREDICATE_TO_EVENT_SUBTYPE.get(r[0])
    if not event_subtype:
        return None

    participants = [p for p in (subj, obj) if p is not None]
    if len(participants) < 1:
        return None

    event_id, is_new = _find_or_create_event(
        tenant_id=t_id or "default",
        event_subtype=event_subtype,
        participants=participants,
        event_time=evt_time,
        location_entity_id=loc_id,
        statement_id=statement_id,
        confidence=float(conf or 0.6),
    )

    if event_id:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE statements SET spawned_event_id = %s WHERE statement_id = %s",
                    (event_id, statement_id),
                )
            conn.commit()
        return {"event_id": event_id, "is_new": is_new,
                "subtype": event_subtype, "participants": participants}
    return None


def spawn_for_corpus(dataset_id: Optional[str] = None) -> dict:
    """Walk every promoted statement that hasn't yet spawned an event
    and try to create one. Idempotent."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            sql = """
                SELECT s.statement_id
                  FROM statements s
                  JOIN documents d ON d.document_id = s.document_id
                 WHERE s.validation_status = 'promoted'
                   AND s.spawned_event_id IS NULL
                """
            params: list = []
            if dataset_id:
                sql += " AND d.dataset_id = %s"
                params.append(dataset_id)
            cur.execute(sql, params)
            ids = [r[0] for r in cur.fetchall()]
    stats = {"scanned": len(ids), "events_new": 0, "events_merged": 0,
             "skipped": 0, "by_subtype": {}}
    for sid in ids:
        try:
            r = spawn_from_statement(sid)
            if r is None:
                stats["skipped"] += 1
                continue
            if r["is_new"]:
                stats["events_new"] += 1
            else:
                stats["events_merged"] += 1
            stats["by_subtype"][r["subtype"]] = stats["by_subtype"].get(r["subtype"], 0) + 1
        except Exception:
            log.exception("event spawn failed for statement %d", sid)
            stats["skipped"] += 1
    return stats


if __name__ == "__main__":
    import argparse, json
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", default=None)
    args = p.parse_args()
    print(json.dumps(spawn_for_corpus(args.dataset), indent=2))
