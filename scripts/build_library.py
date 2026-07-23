#!/usr/bin/env python3
"""
build_library.py — orchestrator for the OSINT discovery loop.

Given a focal term (e.g. "Roswell", "Khufu", "Acme") this script:
  1. Creates or finds a library node at address 13.X.Y.
  2. Discovers references via:
       - Google Books search
       - archive.org search
       - Wikipedia bibliography parse (for the matching article title)
  3. Persists each reference as a bibliographic_references row.
  4. Runs the acquisition worker over the pending references.
  5. (Future: ingest acquired PDFs, extract citations, loop.)

Idempotent: re-running adds newly-discovered refs but doesn't duplicate.

Run inside the api container:
    docker exec app-api-1 python /app/scripts/build_library.py "Roswell" \\
        --subtype topic_library --max-refs 80

Args:
    focal_term            (positional)
    --subtype             one of entity_library/relationship_library/event_library/topic_library/corpus_library
    --max-refs            cap total references per source (default 40)
    --skip-acquire        only discover, don't try to download
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, "/app")

from core.discovery import (
    google_books, archive_org, wikipedia_bibliography, openlibrary,
    cia_reading_room, fbi_vault, acquirer, purchase_links,
    pdf_ingest_bridge,
)
from core.discovery.rate_limiter import all_stats
from core.gdu.address import assign_address
from storage.postgres.db import get_connection

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("build_library")


# ─── Library find-or-create ──────────────────────────────────────────

def find_or_create_library(
    *, focal_term: str, subtype: str = "topic_library",
    tenant_id: str = "default",
) -> tuple[int, str, bool]:
    """Returns (library_id, base60_address, is_new)."""
    canonical_name = f"{subtype}/{focal_term}"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT library_id, base60_address FROM libraries WHERE canonical_name = %s",
                (canonical_name,),
            )
            row = cur.fetchone()
            if row:
                return row[0], row[1], False

            addr = assign_address(conn, "library", subtype)
            if not addr:
                raise RuntimeError(f"Could not mint address for library {subtype}")
            cur.execute(
                """
                INSERT INTO libraries
                    (base60_address, library_subtype, canonical_name,
                     focal_term, tenant_id, description)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING library_id
                """,
                (addr, subtype, canonical_name, focal_term, tenant_id,
                 f"Library for topic: {focal_term}. Auto-built by build_library.py."),
            )
            lib_id = cur.fetchone()[0]
        conn.commit()
    return lib_id, addr, True


# ─── Reference persistence ──────────────────────────────────────────

def persist_reference(library_id: int, ref: dict) -> Optional[tuple[int, bool]]:
    """Insert one reference. Returns (reference_id, is_new) or None if dedup hit."""
    identifiers = ref.get("identifiers") or {}
    dedup_key = (identifiers.get("doi")
                  or identifiers.get("isbn")
                  or identifiers.get("ol_id")
                  or identifiers.get("archive_id")
                  or identifiers.get("google_books_id")
                  or ref.get("title", ""))
    if not dedup_key:
        return None

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT reference_id FROM bibliographic_references
                 WHERE library_id = %s AND
                       COALESCE(identifiers->>'doi',
                                identifiers->>'isbn',
                                identifiers->>'ol_id',
                                identifiers->>'archive_id',
                                title) = %s
                """,
                (library_id, dedup_key),
            )
            existing = cur.fetchone()
            if existing:
                return existing[0], False

            addr = assign_address(conn, "document", "bibliographic_reference")
            # Build standardized acquisition links so the reference is
            # actionable even when we can't auto-download it.
            links = purchase_links.build_links(
                identifiers=identifiers,
                title=ref.get("title"),
                authors=ref.get("authors") or [],
                publication_year=ref.get("publication_year"),
                reference_type=ref.get("reference_type"),
            )
            cur.execute(
                """
                INSERT INTO bibliographic_references
                    (base60_address, library_id, citation_text, title,
                     authors, publication_year, publisher, reference_type,
                     identifiers, discovered_via, discovery_query,
                     purchase_links)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb)
                RETURNING reference_id
                """,
                (addr, library_id,
                 ref.get("citation_text") or ref.get("title") or "(unnamed)",
                 ref.get("title"), ref.get("authors") or [],
                 ref.get("publication_year"), ref.get("publisher"),
                 ref.get("reference_type") or "unknown",
                 json.dumps(identifiers),
                 ref.get("discovered_via"), ref.get("discovery_query"),
                 json.dumps(links)),
            )
            rid = cur.fetchone()[0]
        conn.commit()
    return rid, True


# ─── Discovery passes ────────────────────────────────────────────────

def discover_google_books(focal_term: str, library_id: int, max_results: int = 40) -> dict:
    log.info("[google_books] searching '%s'", focal_term)
    refs = google_books.search(focal_term, max_results=max_results)
    log.info("[google_books] %d refs returned", len(refs))
    new_count = 0
    dedup_count = 0
    for r in refs:
        result = persist_reference(library_id, r)
        if not result:
            continue
        _, is_new = result
        if is_new:
            new_count += 1
        else:
            dedup_count += 1
    return {"source": "google_books", "total": len(refs),
            "new": new_count, "dedup": dedup_count}


def discover_archive_org(focal_term: str, library_id: int, max_results: int = 30) -> dict:
    log.info("[archive_org] searching '%s'", focal_term)
    refs = archive_org.search(focal_term, max_results=max_results)
    log.info("[archive_org] %d refs returned", len(refs))
    new_count = 0
    dedup_count = 0
    for r in refs:
        result = persist_reference(library_id, r)
        if not result:
            continue
        _, is_new = result
        if is_new:
            new_count += 1
        else:
            dedup_count += 1
    return {"source": "archive_org", "total": len(refs),
            "new": new_count, "dedup": dedup_count}


def discover_wikipedia(focal_term: str, library_id: int) -> dict:
    log.info("[wikipedia] fetching article '%s'", focal_term)
    refs = wikipedia_bibliography.fetch_and_parse(focal_term)
    log.info("[wikipedia] %d refs parsed from article bibliography", len(refs))
    new_count = 0
    dedup_count = 0
    for r in refs:
        result = persist_reference(library_id, r)
        if not result:
            continue
        _, is_new = result
        if is_new:
            new_count += 1
        else:
            dedup_count += 1
    return {"source": "wikipedia", "total": len(refs),
            "new": new_count, "dedup": dedup_count}


def discover_openlibrary(focal_term: str, library_id: int, max_results: int = 40) -> dict:
    log.info("[openlibrary] searching '%s'", focal_term)
    refs = openlibrary.search(focal_term, max_results=max_results)
    log.info("[openlibrary] %d refs returned", len(refs))
    new_count = dedup_count = 0
    for r in refs:
        result = persist_reference(library_id, r)
        if not result:
            continue
        _, is_new = result
        if is_new: new_count += 1
        else:      dedup_count += 1
    return {"source": "openlibrary", "total": len(refs),
            "new": new_count, "dedup": dedup_count}


def discover_cia_reading_room(focal_term: str, library_id: int, max_results: int = 30) -> dict:
    log.info("[cia_reading_room] searching '%s'", focal_term)
    refs = cia_reading_room.search(focal_term, max_results=max_results)
    log.info("[cia_reading_room] %d refs returned", len(refs))
    new_count = dedup_count = 0
    for r in refs:
        result = persist_reference(library_id, r)
        if not result:
            continue
        _, is_new = result
        if is_new: new_count += 1
        else:      dedup_count += 1
    return {"source": "cia_reading_room", "total": len(refs),
            "new": new_count, "dedup": dedup_count}


def discover_fbi_vault(focal_term: str, library_id: int, max_results: int = 30) -> dict:
    log.info("[fbi_vault] searching '%s'", focal_term)
    refs = fbi_vault.search(focal_term, max_results=max_results)
    log.info("[fbi_vault] %d refs returned", len(refs))
    new_count = dedup_count = 0
    for r in refs:
        result = persist_reference(library_id, r)
        if not result:
            continue
        _, is_new = result
        if is_new: new_count += 1
        else:      dedup_count += 1
    return {"source": "fbi_vault", "total": len(refs),
            "new": new_count, "dedup": dedup_count}


# ─── Aggregate counts on the library row ────────────────────────────

def refresh_library_counts(library_id: int) -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE libraries l
                   SET reference_count   = sub.total,
                       acquired_count    = sub.acquired,
                       pending_count     = sub.pending,
                       unavailable_count = sub.unavailable,
                       last_updated_at   = NOW(),
                       last_refreshed_at = NOW()
                  FROM (
                    SELECT COUNT(*) AS total,
                           COUNT(*) FILTER (WHERE acquisition_status IN ('acquired','ingested')) AS acquired,
                           COUNT(*) FILTER (WHERE acquisition_status = 'pending') AS pending,
                           COUNT(*) FILTER (WHERE acquisition_status = 'unavailable') AS unavailable
                      FROM bibliographic_references WHERE library_id = %s
                  ) sub
                 WHERE l.library_id = %s
                 RETURNING reference_count, acquired_count, pending_count, unavailable_count
                """,
                (library_id, library_id),
            )
            row = cur.fetchone()
        conn.commit()
    return {"total": row[0], "acquired": row[1], "pending": row[2], "unavailable": row[3]}


# ─── Entry point ────────────────────────────────────────────────────

def build(focal_term: str, *, subtype: str = "topic_library",
          max_refs_per_source: int = 40, skip_acquire: bool = False,
          acquire_limit: int = 20, ingest_limit: int = 0) -> dict:
    log.info("=" * 70)
    log.info("Building library: '%s'  (subtype=%s)", focal_term, subtype)
    log.info("=" * 70)

    lib_id, addr, is_new = find_or_create_library(
        focal_term=focal_term, subtype=subtype)
    log.info("Library: id=%d address=%s is_new=%s", lib_id, addr, is_new)

    discoveries = []
    for fn, name in [
        (lambda: discover_google_books(focal_term, lib_id, max_refs_per_source), "google_books"),
        (lambda: discover_archive_org(focal_term, lib_id, max_refs_per_source), "archive_org"),
        (lambda: discover_openlibrary(focal_term, lib_id, max_refs_per_source), "openlibrary"),
        (lambda: discover_wikipedia(focal_term, lib_id), "wikipedia"),
        (lambda: discover_cia_reading_room(focal_term, lib_id, max_refs_per_source), "cia_reading_room"),
        (lambda: discover_fbi_vault(focal_term, lib_id, max_refs_per_source), "fbi_vault"),
    ]:
        try:
            d = fn()
            discoveries.append(d)
            log.info("  %s: %s", name, d)
        except Exception:
            log.exception("discovery source %s failed", name)
            discoveries.append({"source": name, "error": "see logs"})

    counts = refresh_library_counts(lib_id)
    log.info("Library counts: %s", counts)

    acquisition = {"skipped": True}
    if not skip_acquire:
        log.info("Running acquisition queue (limit=%d)…", acquire_limit)
        acquisition = acquirer.run_queue(library_id=lib_id, limit=acquire_limit)
        log.info("Acquisition: %s", acquisition)
        counts = refresh_library_counts(lib_id)

    ingestion = {"skipped": True}
    if ingest_limit > 0:
        log.info("Running PDF ingest bridge (limit=%d)…", ingest_limit)
        ingestion = pdf_ingest_bridge.run_queue(library_id=lib_id, limit=ingest_limit)
        log.info("Ingestion: %s", ingestion)
        counts = refresh_library_counts(lib_id)

    return {
        "library_id": lib_id,
        "address":    addr,
        "is_new":     is_new,
        "discoveries": discoveries,
        "counts":      counts,
        "acquisition": acquisition,
        "ingestion":   ingestion,
        "rate_limiter_stats": all_stats(),
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("focal_term")
    p.add_argument("--subtype", default="topic_library",
                   choices=["entity_library", "relationship_library",
                            "event_library", "topic_library", "corpus_library"])
    p.add_argument("--max-refs", type=int, default=40)
    p.add_argument("--skip-acquire", action="store_true")
    p.add_argument("--acquire-limit", type=int, default=20)
    p.add_argument("--ingest-limit", type=int, default=0,
                   help="If > 0, run PDF ingest bridge on up to N acquired PDFs after acquisition")
    args = p.parse_args()
    result = build(
        focal_term=args.focal_term,
        subtype=args.subtype,
        max_refs_per_source=args.max_refs,
        skip_acquire=args.skip_acquire,
        acquire_limit=args.acquire_limit,
        ingest_limit=args.ingest_limit,
    )
    print(json.dumps(result, indent=2, default=str))
