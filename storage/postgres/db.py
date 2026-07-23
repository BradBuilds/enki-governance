"""
PostgreSQL database connection and schema initialization.

Supports two modes:
  1. Pooled (API server): call initialize_pool() at startup, close_pool() at shutdown.
     get_connection() pulls from the pool and returns connections automatically.
  2. Direct (scripts, one-off tools): skip initialize_pool(). get_connection() falls
     back to a fresh psycopg2.connect() per call — same interface, no pool needed.
"""

import logging
import os
import psycopg2
import psycopg2.pool
from contextlib import contextmanager
from pathlib import Path
from typing import Optional
from storage.config import get_db_dsn

log = logging.getLogger("enki.db")

_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None


def initialize_pool(dsn: Optional[str] = None, minconn: Optional[int] = None, maxconn: Optional[int] = None):
    """Create the shared connection pool. Call once at API startup.

    Pool size is env-tunable so we don't have to patch code to scale up:
      ENKI_DB_POOL_MIN  (default 2)
      ENKI_DB_POOL_MAX  (default 50)
    Bumped to 50 from 20 after Jun-2026 observation that api startup
    tasks + workers can briefly hold ~25 connections together, exhausting
    the old 20-slot pool and 500-ing /system/depth until they drained.
    """
    global _pool
    if minconn is None:
        minconn = int(os.environ.get("ENKI_DB_POOL_MIN", "2"))
    if maxconn is None:
        maxconn = int(os.environ.get("ENKI_DB_POOL_MAX", "50"))
    connection_string = dsn if dsn is not None else get_db_dsn()
    _pool = psycopg2.pool.ThreadedConnectionPool(minconn, maxconn, connection_string)
    log.info(f"Connection pool initialized (min={minconn}, max={maxconn})")


def close_pool():
    """Close all pooled connections. Call at API shutdown."""
    global _pool
    if _pool is not None:
        _pool.closeall()
        log.info("Connection pool closed")
        _pool = None


@contextmanager
def get_connection(dsn: Optional[str] = None):
    """
    Get a PostgreSQL connection.

    If the pool is initialized and no explicit dsn is given, pulls from the pool.
    Otherwise falls back to a direct psycopg2.connect() (scripts, tests, etc.).

    Yields:
        psycopg2 connection object
    """
    if _pool is not None and dsn is None:
        conn = _pool.getconn()
        try:
            yield conn
        finally:
            # Discard any uncommitted/failed transaction state before
            # returning to the pool. Without this, a worker that leaves
            # InFailedSqlTransaction state poisons the next caller.
            try:
                conn.rollback()
            except Exception:
                pass
            _pool.putconn(conn)
    else:
        connection_string = dsn if dsn is not None else get_db_dsn()
        conn = psycopg2.connect(connection_string)
        try:
            yield conn
        finally:
            try:
                conn.rollback()
            except Exception:
                pass
            conn.close()


def initialize_schema():
    """
    Initialize database schema from schema_v2.sql.

    Idempotent: uses IF NOT EXISTS, safe to run multiple times.
    Includes all tables for governance spine + command center pipeline.
    """
    schema_path = Path(__file__).parent / 'schema_v2.sql'
    sql = schema_path.read_text(encoding='utf-8')

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
