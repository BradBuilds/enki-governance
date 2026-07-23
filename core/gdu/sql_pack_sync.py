"""
Sync the GDU pack registry into the SQL `gdu_pack_positions` table.

The Python pack_registry is the source of truth for category/subtype indices.
SQL triggers (notably the mirror_*_to_knowledge_node triggers from migration
015 + the gdu_assign_address function from migration 032) need those same
indices to assign addresses inline.

Rather than have triggers reach across to Python, we mirror the indices into
a SQL table the triggers can SELECT from. This module is responsible for
keeping that table in sync with the active pack.

Call sync_pack_positions_to_sql() at:
  - Server boot (after pack_registry loads)
  - Pack admission ceremony (after a new pack version is admitted)
  - Anytime the active set changes

Idempotent. Replaces the entire table in a single transaction.
"""
from __future__ import annotations

import logging
from typing import Optional

from core.gdu.pack_registry import (
    get_category_index,
    get_subtype_index,
)

log = logging.getLogger("enki.gdu.sql_sync")


def _iter_pack_positions():
    """Yield (category, subtype, cat_idx, sub_idx, pack_version) tuples
    from the active pack registry.

    Uses public pack_registry helpers only."""
    from core.gdu.pack_registry import (
        get_valid_categories,
        get_valid_subtypes,
        get_node_taxonomy,
    )

    taxonomy = get_node_taxonomy() or {}
    pack_version = "unknown"
    if isinstance(taxonomy, dict):
        # The taxonomy dict may carry the version on a metadata key when the
        # pack registry exposes it. Be defensive about its shape.
        version_meta = taxonomy.get("__pack_version__") or taxonomy.get("version")
        if isinstance(version_meta, str):
            pack_version = version_meta

    for cat_name in sorted(get_valid_categories()):
        cat_idx = get_category_index(cat_name)
        if cat_idx < 0:
            continue
        for sub_name in sorted(get_valid_subtypes(cat_name)):
            sub_idx = get_subtype_index(cat_name, sub_name)
            if sub_idx < 0:
                continue
            yield cat_name, sub_name, cat_idx, sub_idx, pack_version


def sync_pack_positions_to_sql(conn, *, log_summary: bool = True) -> dict:
    """Replace gdu_pack_positions with the current pack registry contents.

    Args:
        conn: psycopg2 connection. Caller manages commit/rollback.
        log_summary: if True, log a one-line summary at INFO.

    Returns:
        dict with keys: rows_written, categories, pack_version
    """
    rows = list(_iter_pack_positions())

    pack_version = rows[0][4] if rows else "unknown"
    categories = sorted({r[0] for r in rows})

    with conn.cursor() as cur:
        # Verify the table exists. If migration 032 hasn't been applied yet,
        # this is a no-op and we log a warning rather than raise.
        cur.execute("""
            SELECT to_regclass('public.gdu_pack_positions') IS NOT NULL
        """)
        exists = cur.fetchone()[0]
        if not exists:
            log.warning(
                "gdu_pack_positions table does not exist (migration 032 not applied?). "
                "Skipping SQL sync."
            )
            return {"rows_written": 0, "categories": [], "pack_version": pack_version,
                    "skipped": True}

        cur.execute("DELETE FROM gdu_pack_positions")
        cur.executemany(
            """
            INSERT INTO gdu_pack_positions
                (category, subtype, cat_idx, sub_idx, pack_version, synced_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
            """,
            rows,
        )

    if log_summary:
        log.info(
            "Synced %d pack positions to SQL (categories: %s, pack=%s)",
            len(rows), ",".join(categories), pack_version,
        )

    return {
        "rows_written": len(rows),
        "categories": categories,
        "pack_version": pack_version,
        "skipped": False,
    }


def sync_pack_positions_via_dsn(dsn: Optional[str] = None) -> dict:
    """Convenience wrapper — opens a connection, runs the sync, commits.

    Pass a DSN or read from ENKI_DB_DSN env."""
    import psycopg2
    from storage.config import get_db_dsn

    conn = psycopg2.connect(dsn or get_db_dsn())
    try:
        result = sync_pack_positions_to_sql(conn)
        conn.commit()
        return result
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    result = sync_pack_positions_via_dsn()
    if result.get("skipped"):
        print("SKIPPED: gdu_pack_positions table does not exist yet")
        print("Apply migration 032 first.")
        sys.exit(1)
    print(f"Synced {result['rows_written']} positions for pack {result['pack_version']}")
    print(f"Categories: {', '.join(result['categories'])}")
