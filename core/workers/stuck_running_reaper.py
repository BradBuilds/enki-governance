"""
Stuck-running reaper.

When a worker crashes mid-job, the queue row stays in `running` forever.
The dedup UNIQUE indexes on these queues (e.g. idx_enrichment_queue_dedup)
then block re-queueing the same (entity_id, type) pair, so the entity
becomes permanently un-enrichable.

This module resets stuck `running` rows back to `pending` so a future
worker can pick them up. Called at worker startup and periodically from
the worker loop.
"""
from __future__ import annotations

import logging
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.stuck_running_reaper")

DEFAULT_AGE_HOURS = 6


def reap_stuck_rows(
    table: str,
    *,
    started_col: str = "started_at",
    status_col: str = "status",
    age_hours: int = DEFAULT_AGE_HOURS,
    tenant_id: Optional[str] = "default",
) -> int:
    """Reset rows stuck in `running` longer than `age_hours` back to `pending`.

    Returns count of rows reset. Safe to call repeatedly; idempotent.
    """
    if not table or not table.replace("_", "").isalnum():
        raise ValueError(f"refusing to reap from suspicious table name: {table!r}")

    where = [f"{status_col} = 'running'",
             f"{started_col} < NOW() - make_interval(hours => %s)"]
    params: list = [age_hours]
    if tenant_id is not None:
        where.append("tenant_id = %s")
        params.append(tenant_id)

    sql = (
        f"UPDATE {table} SET {status_col} = 'pending' "
        f"WHERE {' AND '.join(where)}"
    )

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                count = cur.rowcount
            conn.commit()
        if count > 0:
            log.info("reaped %d stuck-running rows from %s", count, table)
        return count
    except Exception as e:
        log.warning("reaper failed for %s: %s", table, e)
        return 0
