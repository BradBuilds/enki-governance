"""
Database pull connector — connect to any SQL database and ingest rows as signals.

Supports PostgreSQL, MySQL, SQLite. Reads only (never writes to external DB).
Each row becomes a signal. Watermark tracks last-seen ID or timestamp
so only new rows are fetched on each cycle.

source_config:
  {
    "db_type": "postgresql",           # postgresql, mysql, sqlite
    "host": "db.company.com",
    "port": 5432,
    "database": "sales_db",
    "username": "readonly_user",
    "password": "encrypted_password",
    "query": "SELECT * FROM transactions WHERE id > :watermark ORDER BY id LIMIT 1000",
    "watermark_field": "id",           # field to track position
    "signal_type_hint": "signal.financial.transaction"
  }
"""

import json
from datetime import datetime, timezone
from typing import Optional

from storage.connectors.base import BaseConnector
from storage.connectors.manager import register_connector_class


class DatabaseConnector(BaseConnector):
    connector_type = "pull"
    source_type = "database_query"

    def fetch(self) -> list[dict]:
        config = self.source_config
        db_type = config.get("db_type", "postgresql")
        query = config.get("query", "")

        if not query:
            raise ValueError("Database connector requires 'query' in source_config")

        # Get watermark value
        watermark_field = config.get("watermark_field", "id")
        watermark_value = self.watermark.get("last_value", "0")

        # Substitute watermark into query
        resolved_query = query.replace(":watermark", str(watermark_value))
        resolved_query = resolved_query.replace(":last_sync", str(watermark_value))

        # Connect and fetch
        rows = _execute_query(db_type, config, resolved_query)

        if not rows:
            return []

        # Convert rows to pipeline items
        signal_type_hint = config.get("signal_type_hint")
        items = []
        for row in rows:
            payload = dict(row)
            if signal_type_hint:
                payload["_signal_type_hint"] = signal_type_hint
            raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            items.append({
                "raw_bytes": raw,
                "content_type": "application/json",
                "source_type": "database_query",
            })

        return items

    def update_watermark(self, items: list[dict]) -> dict:
        if not items:
            return self.watermark

        watermark_field = self.source_config.get("watermark_field", "id")

        # Get the last row's watermark value
        try:
            last_row = json.loads(items[-1]["raw_bytes"])
            last_value = last_row.get(watermark_field, "")
        except (json.JSONDecodeError, KeyError):
            last_value = ""

        return {
            "last_value": str(last_value),
            "last_fetch_at": datetime.now(timezone.utc).isoformat(),
            "rows_fetched": len(items),
        }


def _execute_query(db_type: str, config: dict, query: str) -> list[dict]:
    """Execute a read-only query against an external database."""
    if db_type == "postgresql":
        return _query_postgresql(config, query)
    elif db_type == "mysql":
        return _query_mysql(config, query)
    elif db_type == "sqlite":
        return _query_sqlite(config, query)
    else:
        raise ValueError(f"Unsupported database type: {db_type}")


def _query_postgresql(config: dict, query: str) -> list[dict]:
    """Query PostgreSQL. Uses psycopg2 (already installed for Enki)."""
    import psycopg2
    import psycopg2.extras

    dsn = (
        f"host={config['host']} port={config.get('port', 5432)} "
        f"dbname={config['database']} user={config['username']} "
        f"password={config['password']}"
    )

    conn = psycopg2.connect(dsn)
    try:
        conn.set_session(readonly=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query)
            rows = cur.fetchall()
            return [dict(row) for row in rows]
    finally:
        conn.close()


def _query_mysql(config: dict, query: str) -> list[dict]:
    """Query MySQL. Requires pymysql (optional dependency)."""
    try:
        import pymysql
        import pymysql.cursors
    except ImportError:
        raise ImportError("MySQL support requires pymysql: pip install pymysql")

    conn = pymysql.connect(
        host=config["host"],
        port=config.get("port", 3306),
        database=config["database"],
        user=config["username"],
        password=config["password"],
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(query)
            return cur.fetchall()
    finally:
        conn.close()


def _query_sqlite(config: dict, query: str) -> list[dict]:
    """Query SQLite file (local, read-only)."""
    import sqlite3

    db_path = config.get("database", config.get("path", ""))
    if not db_path:
        raise ValueError("SQLite requires 'database' or 'path' in config")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(query)
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def test_database_connection(config: dict) -> dict:
    """Test a database connection without ingesting. Returns preview rows."""
    db_type = config.get("db_type", "postgresql")
    query = config.get("query", "SELECT 1")

    # Replace watermark with 0 for test
    query = query.replace(":watermark", "0").replace(":last_sync", "'1970-01-01'")

    # Add LIMIT for safety
    if "LIMIT" not in query.upper():
        query += " LIMIT 5"

    try:
        rows = _execute_query(db_type, config, query)
        fields = list(rows[0].keys()) if rows else []
        return {
            "success": True,
            "row_count": len(rows),
            "fields": fields,
            "preview": rows[:3],
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# Register
register_connector_class("database_query", DatabaseConnector)
