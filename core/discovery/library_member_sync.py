"""
library_member_sync — keep library_members and library counters in sync
with reality.

After any ingest / extraction pass, the entities, assessments, and
events produced from a library's documents should be reflected as
library_members rows. This module provides both a one-shot backfill
(sync_library) and a hook the workers can call after each new write.
"""

from __future__ import annotations

import logging
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.discovery.library_member_sync")


def sync_library(library_id: int) -> dict:
    """Reconcile library_members for one library by walking every
    document attached to it and pulling in:
      - every entity referenced by mentions in those docs
      - every assessment built from those docs' statements
      - every event whose originating_statement_ids overlap

    Idempotent — re-running adds new members, does NOT remove existing
    ones (a member can have been explicitly added for other reasons)."""
    stats = {"docs": 0, "entities_added": 0, "assessments_added": 0,
             "events_added": 0}
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT member_id FROM library_members
                 WHERE library_id = %s AND member_kind = 'document'
                """,
                (library_id,),
            )
            doc_ids = [r[0] for r in cur.fetchall()]
    stats["docs"] = len(doc_ids)
    if not doc_ids:
        return stats

    # Entities — every distinct entity_id mentioned in any of these docs
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO library_members (library_id, member_kind, member_id,
                                              member_address, added_by)
                SELECT %s, 'entity', DISTINCT_ENTITIES.entity_id,
                       e.base60_address, 'library_member_sync'
                  FROM (SELECT DISTINCT entity_id
                          FROM document_mentions
                         WHERE document_id = ANY(%s) AND entity_id IS NOT NULL) AS DISTINCT_ENTITIES
                  JOIN entities e ON e.entity_id = DISTINCT_ENTITIES.entity_id
                ON CONFLICT (library_id, member_kind, member_id) DO NOTHING
                """,
                (library_id, doc_ids),
            )
            stats["entities_added"] = cur.rowcount

            # Assessments built from statements in these docs
            cur.execute(
                """
                INSERT INTO library_members (library_id, member_kind, member_id,
                                              member_address, added_by)
                SELECT %s, 'assessment', a.assessment_id, a.base60_address,
                       'library_member_sync'
                  FROM assessments a
                 WHERE EXISTS (
                       SELECT 1 FROM statements s
                        WHERE s.document_id = ANY(%s)
                          AND s.statement_id = ANY(a.source_statement_ids)
                       )
                ON CONFLICT (library_id, member_kind, member_id) DO NOTHING
                """,
                (library_id, doc_ids),
            )
            stats["assessments_added"] = cur.rowcount

            # Events whose originating_statement_ids overlap any statement
            # in these docs. Use && (array overlap) when events table has
            # that column populated.
            cur.execute(
                """
                INSERT INTO library_members (library_id, member_kind, member_id,
                                              member_address, added_by)
                SELECT %s, 'event', e.event_id, e.base60_address,
                       'library_member_sync'
                  FROM events e
                 WHERE e.originating_statement_ids IS NOT NULL
                   AND EXISTS (
                       SELECT 1 FROM statements s
                        WHERE s.statement_id = ANY(e.originating_statement_ids)
                          AND s.document_id  = ANY(%s)
                       )
                ON CONFLICT (library_id, member_kind, member_id) DO NOTHING
                """,
                (library_id, doc_ids),
            )
            stats["events_added"] = cur.rowcount

            # Refresh library counters — all sourced from authoritative tables
            # so they stay in sync as the pipeline runs.
            cur.execute(
                """
                UPDATE libraries l
                   SET reference_count   = (SELECT COUNT(*) FROM bibliographic_references
                                             WHERE library_id=l.library_id),
                       acquired_count    = (SELECT COUNT(*) FROM bibliographic_references
                                             WHERE library_id=l.library_id AND document_id IS NOT NULL),
                       pending_count     = (SELECT COUNT(*) FROM bibliographic_references
                                             WHERE library_id=l.library_id AND document_id IS NULL),
                       unavailable_count = (SELECT COUNT(*) FROM bibliographic_references
                                             WHERE library_id=l.library_id AND acquisition_status='unavailable'),
                       document_count    = (SELECT COUNT(*) FROM library_members
                                             WHERE library_id=l.library_id AND member_kind='document'),
                       entity_count      = (SELECT COUNT(*) FROM library_members
                                             WHERE library_id=l.library_id AND member_kind='entity'),
                       fact_count        = (SELECT COUNT(*) FROM library_members
                                             WHERE library_id=l.library_id AND member_kind='assessment'),
                       last_updated_at   = NOW()
                 WHERE l.library_id = %s
                """,
                (library_id,),
            )
        conn.commit()
    return stats


def sync_all_libraries() -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT library_id FROM libraries")
            ids = [r[0] for r in cur.fetchall()]
    totals = {"libraries": len(ids), "per_library": []}
    for lid in ids:
        s = sync_library(lid)
        s["library_id"] = lid
        totals["per_library"].append(s)
    return totals


if __name__ == "__main__":
    import argparse, json
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--library", type=int, default=None)
    args = p.parse_args()
    if args.library:
        r = sync_library(args.library)
    else:
        r = sync_all_libraries()
    print(json.dumps(r, indent=2, default=str))
