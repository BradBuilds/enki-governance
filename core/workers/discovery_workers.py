"""
Discovery workers — autonomous engine layer for the OSINT pipeline.

Four workers, each a function that runs one tick of work and exits.
Designed to be invoked from cron / systemd / a supervisor every few
minutes. None are infinite-loop; each does bounded work then returns.

Workers:
  library_refresh    — for each library whose next_refresh_at <= NOW,
                       re-run the 6-source discovery pass and update
                       refresh timestamps.
  acquisition_retry  — for each pending bibliographic_reference whose
                       next_retry_at <= NOW, call the acquirer.
  citation_cascade   — for each newly-ingested doc that hasn't yet had
                       citations harvested, run citation_extractor.
  predicate_promote  — surface candidate verb→edge_type pairs that
                       appear N+ times in collapse_decisions but aren't
                       in predicate_dictionary yet.

Run individually:
  python -m core.workers.discovery_workers library_refresh
  python -m core.workers.discovery_workers acquisition_retry
  python -m core.workers.discovery_workers citation_cascade
  python -m core.workers.discovery_workers predicate_promote
  python -m core.workers.discovery_workers all     # all four in sequence
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

sys.path.insert(0, "/app")

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.discovery")


# ─── 4A. library_refresh ──────────────────────────────────────────────

def library_refresh(max_libraries_per_tick: int = 3) -> dict:
    """Walk libraries with next_refresh_at <= NOW(). For each, re-run
    the 6-source discovery pass (without acquisition) and bump
    timestamps. Bounded so a single tick doesn't run forever."""
    from scripts.build_library import (
        discover_google_books, discover_archive_org, discover_openlibrary,
        discover_wikipedia, discover_cia_reading_room, discover_fbi_vault,
        refresh_library_counts,
    )

    stats = {"libraries_refreshed": 0, "new_refs": 0, "errors": 0,
             "per_library": []}

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT library_id, focal_term, library_subtype,
                       refresh_interval_hours
                  FROM libraries
                 WHERE discovery_status = 'active'
                   AND (next_refresh_at IS NULL OR next_refresh_at <= NOW())
                 ORDER BY COALESCE(next_refresh_at, '1970-01-01'::timestamptz),
                          library_id
                 LIMIT %s
                """,
                (max_libraries_per_tick,),
            )
            rows = cur.fetchall()

    for library_id, focal_term, subtype, refresh_h in rows:
        try:
            log.info("Refreshing library %d (%s)", library_id, focal_term)
            entry = {"library_id": library_id, "focal_term": focal_term}
            new_count = 0
            for fn, name in [
                (lambda: discover_google_books(focal_term, library_id, 20), "google_books"),
                (lambda: discover_archive_org(focal_term, library_id, 20), "archive_org"),
                (lambda: discover_openlibrary(focal_term, library_id, 20), "openlibrary"),
                (lambda: discover_wikipedia(focal_term, library_id), "wikipedia"),
                (lambda: discover_cia_reading_room(focal_term, library_id, 15), "cia"),
                (lambda: discover_fbi_vault(focal_term, library_id, 15), "fbi"),
            ]:
                try:
                    d = fn()
                    new_count += d.get("new", 0)
                except Exception:
                    log.exception("discovery source %s failed", name)
            refresh_library_counts(library_id)
            # Bump next_refresh_at
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE libraries
                           SET last_refreshed_at = NOW(),
                               next_refresh_at = NOW() + (%s || ' hours')::interval,
                               last_updated_at = NOW()
                         WHERE library_id = %s
                        """,
                        (refresh_h or 168, library_id),
                    )
                conn.commit()
            entry["new_refs"] = new_count
            stats["libraries_refreshed"] += 1
            stats["new_refs"] += new_count
            stats["per_library"].append(entry)
        except Exception:
            log.exception("library_refresh failed for %d", library_id)
            stats["errors"] += 1
    return stats


# ─── 4B. acquisition_retry ────────────────────────────────────────────

def acquisition_retry(max_attempts_per_tick: int = 10) -> dict:
    """Try to acquire any pending references whose next_retry_at <= NOW
    (or has never been attempted). Delegates to the acquirer module."""
    from core.discovery import acquirer
    return acquirer.run_queue(library_id=None, limit=max_attempts_per_tick)


# ─── 4C. citation_cascade ─────────────────────────────────────────────

def citation_cascade(max_docs_per_tick: int = 5) -> dict:
    """Find recently-ingested documents that have NOT yet had citation
    extraction run, and run it. Marker: a doc is 'cascade-done' when at
    least one citation_emissions row points at it (heuristic) OR when
    we've explicitly noted it processed."""
    from core.discovery import citation_extractor

    stats = {"docs_processed": 0, "new_refs": 0, "errors": 0}
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Documents attached to a library that have no citation_emissions
            # AND have been ingested for ≥10 minutes (give the rest of the
            # ingest pipeline time to settle).
            cur.execute(
                """
                SELECT DISTINCT lm.member_id AS document_id, lm.library_id
                  FROM library_members lm
                  JOIN documents d ON d.document_id = lm.member_id
                 WHERE lm.member_kind = 'document'
                   AND d.created_at < NOW() - INTERVAL '5 minutes'
                   AND NOT EXISTS (
                     SELECT 1 FROM citation_emissions ce
                      WHERE ce.source_document_id = lm.member_id
                   )
                 LIMIT %s
                """,
                (max_docs_per_tick,),
            )
            pairs = cur.fetchall()

    for doc_id, lib_id in pairs:
        try:
            r = citation_extractor.extract_from_document(doc_id, lib_id)
            stats["new_refs"] += r.get("new_refs", 0)
            stats["docs_processed"] += 1
        except Exception:
            log.exception("citation cascade failed for doc %d", doc_id)
            stats["errors"] += 1
    return stats


# ─── 4D. predicate_promote ───────────────────────────────────────────

def predicate_promote(min_occurrences: int = 5) -> dict:
    """Surface verb lemmas that appear in N+ rejected statements
    (rejected_no_predicate) so analysts can add them to the dictionary
    or auto-add the most confident ones.

    For now this is a read-only candidate view: it inserts rows into
    a `predicate_candidates` audit table (we'll create it lazily here
    if missing) so the analyst dashboard can show them.
    """
    stats = {"candidates_found": 0, "candidates_new": 0}
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Lazy-create the candidates view (idempotent)
            cur.execute("""
                CREATE OR REPLACE VIEW predicate_candidates AS
                SELECT predicate_lemma AS lemma,
                       COUNT(*) AS occurrences,
                       MIN(created_at) AS first_seen,
                       MAX(created_at) AS last_seen,
                       (ARRAY_AGG(DISTINCT predicate))[1:5] AS surface_forms
                  FROM statements
                 WHERE validation_status = 'rejected'
                   AND validation_reason LIKE 'no_predicate_mapping:%%'
                   AND predicate_lemma IS NOT NULL
                   AND predicate_lemma <> ''
                 GROUP BY predicate_lemma
                 HAVING COUNT(*) >= %s
                 ORDER BY occurrences DESC
            """, (min_occurrences,))
            conn.commit()

            cur.execute("SELECT lemma, occurrences FROM predicate_candidates")
            rows = cur.fetchall()
            stats["candidates_found"] = len(rows)
            stats["candidate_lemmas"] = [{"lemma": r[0], "occurrences": r[1]} for r in rows[:20]]

    return stats


# ─── Combined entry ──────────────────────────────────────────────────

def run_all_workers() -> dict:
    """Run each worker once in sequence. Useful for cron one-shot."""
    started = time.time()
    out = {"started_at": datetime.now(timezone.utc).isoformat()}
    try:
        out["library_refresh"] = library_refresh()
    except Exception as e:
        out["library_refresh"] = {"error": str(e)}
    try:
        out["acquisition_retry"] = acquisition_retry()
    except Exception as e:
        out["acquisition_retry"] = {"error": str(e)}
    try:
        out["citation_cascade"] = citation_cascade()
    except Exception as e:
        out["citation_cascade"] = {"error": str(e)}
    try:
        out["predicate_promote"] = predicate_promote()
    except Exception as e:
        out["predicate_promote"] = {"error": str(e)}
    out["elapsed_s"] = round(time.time() - started, 1)
    return out


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("worker", default="all",
                   choices=["library_refresh", "acquisition_retry",
                            "citation_cascade", "predicate_promote", "all"])
    args = p.parse_args()
    if args.worker == "all":
        print(json.dumps(run_all_workers(), indent=2, default=str))
    elif args.worker == "library_refresh":
        print(json.dumps(library_refresh(), indent=2, default=str))
    elif args.worker == "acquisition_retry":
        print(json.dumps(acquisition_retry(), indent=2, default=str))
    elif args.worker == "citation_cascade":
        print(json.dumps(citation_cascade(), indent=2, default=str))
    elif args.worker == "predicate_promote":
        print(json.dumps(predicate_promote(), indent=2, default=str))
