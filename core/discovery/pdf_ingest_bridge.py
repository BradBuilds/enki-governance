"""
PDF ingest bridge — feed acquired PDFs into the existing document
ingestion + extraction pipeline.

For each bibliographic_reference with acquisition_status='acquired',
call core.documents.pdf_ingester.ingest_pdf, link the resulting
document_id back onto the reference, then add the document to the
library_members table. Mark acquisition_status='ingested' on success.

Designed to be polite (one PDF at a time, with short delays between)
because PDF parsing + OCR + paragraph segmentation is heavy.
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Optional

sys.path.insert(0, "/app")

from core.documents.pdf_ingester import ingest_pdf
from storage.postgres.db import get_connection

log = logging.getLogger("enki.discovery.pdf_ingest_bridge")


def _attach_to_library(library_id: int, document_id: int, reference_id: int) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Link document → library
            cur.execute(
                """
                INSERT INTO library_members
                    (library_id, member_kind, member_id, member_address, added_by)
                VALUES (%s, 'document', %s,
                        (SELECT base60_address FROM bibliographic_references WHERE reference_id = %s),
                        'pdf_ingest_bridge')
                ON CONFLICT (library_id, member_kind, member_id) DO NOTHING
                """,
                (library_id, document_id, reference_id),
            )
            # Increment library.document_count
            cur.execute(
                """
                UPDATE libraries
                   SET document_count = (SELECT COUNT(*) FROM library_members
                                          WHERE library_id = %s AND member_kind = 'document'),
                       last_updated_at = NOW()
                 WHERE library_id = %s
                """,
                (library_id, library_id),
            )
        conn.commit()


def ingest_acquired_reference(reference_id: int) -> dict:
    """Run the existing PDF ingestion pipeline on one acquired
    reference. Returns the result dict."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT library_id, title, file_path, acquisition_status,
                       acquired_url, identifiers, document_id,
                       publication_year, publisher, authors, citation_text
                  FROM bibliographic_references WHERE reference_id = %s
                """,
                (reference_id,),
            )
            row = cur.fetchone()
    if not row:
        return {"ok": False, "error": "reference_not_found"}
    (library_id, title, file_path, status, acquired_url,
     identifiers, existing_doc_id, year, publisher, authors, citation) = row

    if status not in ("acquired",):
        return {"ok": False, "error": f"not_in_acquired_status:{status}"}
    if not file_path:
        return {"ok": False, "error": "no_file_path"}

    # Build a dataset_id scoped to the library so cross-library
    # corpora stay clean and addressable.
    dataset_id = f"library_{library_id}"

    # Mark ingesting
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bibliographic_references SET acquisition_status = 'ingesting' WHERE reference_id = %s",
                (reference_id,),
            )
        conn.commit()

    # release_date column is DATE; year-only refs must be widened to
    # an ISO YYYY-01-01 string (or omitted).
    release_date_str: Optional[str] = None
    if year:
        try:
            release_date_str = f"{int(year):04d}-01-01"
        except (TypeError, ValueError):
            release_date_str = None

    try:
        result = ingest_pdf(
            path=file_path,
            dataset_id=dataset_id,
            tenant_id="default",
            title=title or citation or f"reference_{reference_id}",
            author=", ".join(authors[:3]) if authors else None,
            publisher=publisher,
            release_date=release_date_str,
            source_url=acquired_url,
            trust_class="mentioned",
            metadata={
                "library_id": library_id,
                "reference_id": reference_id,
                "discovered_via_reference": True,
                "identifiers": identifiers or {},
            },
        )
    except Exception as e:
        log.exception("ingest_pdf crashed for ref %d", reference_id)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE bibliographic_references
                       SET acquisition_status = 'acquired',
                           error_message = %s
                     WHERE reference_id = %s
                    """,
                    (f"ingest_exception:{e}", reference_id),
                )
            conn.commit()
        return {"ok": False, "error": str(e)}

    if not result.get("ok") and not result.get("document_id"):
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE bibliographic_references
                       SET acquisition_status = 'acquired',
                           error_message = %s
                     WHERE reference_id = %s
                    """,
                    (f"ingest_failed:{result.get('error')}", reference_id),
                )
            conn.commit()
        return result

    doc_id = result.get("document_id")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE bibliographic_references
                   SET acquisition_status = 'ingested',
                       document_id        = %s,
                       error_message      = NULL
                 WHERE reference_id = %s
                """,
                (doc_id, reference_id),
            )
        conn.commit()
    if library_id and doc_id:
        _attach_to_library(library_id, doc_id, reference_id)
        # Pull in any entities/assessments/events already linked to
        # this document so the library_members table stays in sync.
        try:
            from core.discovery.library_member_sync import sync_library
            sync_library(library_id)
        except Exception:
            log.exception("library_member_sync failed (non-fatal) for library %d", library_id)
    return {"ok": True, "document_id": doc_id, "reference_id": reference_id}


def run_queue(library_id: Optional[int] = None, limit: int = 10,
              between_items_delay_s: float = 1.0) -> dict:
    """Ingest up to `limit` acquired-but-not-yet-ingested references."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            sql = """
                SELECT reference_id FROM bibliographic_references
                 WHERE acquisition_status = 'acquired'
                   AND file_path IS NOT NULL
                """
            params: list = []
            if library_id is not None:
                sql += " AND library_id = %s"
                params.append(library_id)
            sql += " ORDER BY reference_id LIMIT %s"
            params.append(limit)
            cur.execute(sql, params)
            ids = [r[0] for r in cur.fetchall()]
    stats = {"scanned": len(ids), "ingested": 0, "failed": 0, "errors": []}
    for rid in ids:
        try:
            r = ingest_acquired_reference(rid)
            if r.get("ok"):
                stats["ingested"] += 1
            else:
                stats["failed"] += 1
                stats["errors"].append({"ref": rid, "error": r.get("error")})
        except Exception as e:
            stats["failed"] += 1
            stats["errors"].append({"ref": rid, "error": str(e)})
        time.sleep(between_items_delay_s)
    return stats


if __name__ == "__main__":
    import argparse, json
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--library", type=int, default=None)
    p.add_argument("--limit", type=int, default=10)
    args = p.parse_args()
    print(json.dumps(run_queue(args.library, args.limit), indent=2))
