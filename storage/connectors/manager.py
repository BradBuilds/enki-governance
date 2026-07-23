"""
Connector manager — CRUD for connectors + manual trigger.

Secrets handling:
  Connector configs can reference named secrets using $secret:name syntax.
  When a connector is triggered, secrets are resolved to actual values.
  When connector configs are returned via API, secret refs are masked.
"""

import json
from typing import Optional

from storage.postgres.db import get_connection
from storage.connectors.base import BaseConnector, FetchResult
from storage.secrets import resolve_secrets, mask_secrets_in_config


# Registry of connector classes by source_type
_CONNECTOR_CLASSES: dict[str, type] = {}


def register_connector_class(source_type: str, cls: type):
    _CONNECTOR_CLASSES[source_type] = cls


def _get_connector_class(source_type: str) -> Optional[type]:
    return _CONNECTOR_CLASSES.get(source_type)


# Register known connectors
try:
    from storage.connectors.rss import RssConnector
    register_connector_class("rss_feed", RssConnector)
except ImportError:
    pass

try:
    from storage.connectors.http_api import HttpApiConnector
    register_connector_class("http_api", HttpApiConnector)
except ImportError:
    pass

try:
    from storage.connectors.database import DatabaseConnector
    register_connector_class("database", DatabaseConnector)
except ImportError:
    pass

try:
    from storage.connectors.imap import ImapConnector
    register_connector_class("imap", ImapConnector)
except ImportError:
    pass

try:
    from storage.connectors.mqtt import MqttConnector
    register_connector_class("mqtt", MqttConnector)
except ImportError:
    pass

try:
    from storage.connectors.traccar import TraccarPullConnector
    register_connector_class("traccar_pull", TraccarPullConnector)
except ImportError:
    pass

# Financial connectors
try:
    from storage.connectors.plaid import PlaidConnector
    register_connector_class("plaid_api", PlaidConnector)
except ImportError:
    pass

try:
    from storage.connectors.quickbooks import QuickBooksConnector
    register_connector_class("quickbooks_api", QuickBooksConnector)
except ImportError:
    pass

try:
    from storage.connectors.crypto import CryptoConnector
    register_connector_class("coingecko_api", CryptoConnector)
except ImportError:
    pass

try:
    from storage.connectors.stripe_connector import StripeConnector
    register_connector_class("stripe_api", StripeConnector)
except ImportError:
    pass


def create_connector(
    tenant_id: str,
    device_id: str,
    connector_type: str,
    source_type: str,
    source_config: dict,
    poll_interval: Optional[str] = None,
    created_by: str = "system",
    dsn: Optional[str] = None,
) -> dict:
    """Create a new connector."""
    import uuid
    connector_id = f"conn_{uuid.uuid4().hex[:12]}"
    config_bytes = json.dumps(source_config).encode("utf-8")

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO connectors
                    (connector_id, device_id, tenant_id, connector_type,
                     source_type, source_config, poll_interval)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                connector_id, device_id, tenant_id, connector_type,
                source_type, config_bytes, poll_interval,
            ))
            conn.commit()

    return {
        "connector_id": connector_id, "device_id": device_id,
        "source_type": source_type, "connector_type": connector_type,
        "status": "active",
    }


def get_connector(
    connector_id: str, tenant_id: str,
    mask: bool = True, dsn: Optional[str] = None,
) -> Optional[dict]:
    """Get a single connector. By default masks secret references in config."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT connector_id, device_id, tenant_id, connector_type,
                       source_type, source_config, poll_interval, watermark,
                       status, last_fetch_at, last_error, error_count,
                       items_ingested, created_at
                FROM connectors
                WHERE connector_id = %s AND tenant_id = %s
            """, (connector_id, tenant_id))
            r = cur.fetchone()
            if not r:
                return None
            raw = r[5]
            if isinstance(raw, memoryview):
                raw = bytes(raw)
            config = json.loads(raw) if raw else {}
            return {
                "connector_id": r[0], "device_id": r[1], "tenant_id": r[2],
                "connector_type": r[3], "source_type": r[4],
                "source_config": mask_secrets_in_config(config) if mask else config,
                "poll_interval": str(r[6]) if r[6] else None,
                "watermark": r[7], "status": r[8],
                "last_fetch_at": r[9].isoformat() if r[9] else None,
                "last_error": r[10], "error_count": r[11],
                "items_ingested": r[12],
                "created_at": r[13].isoformat(),
            }


def list_connectors(tenant_id: str, limit: int = 100, dsn: Optional[str] = None) -> list[dict]:
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT connector_id, device_id, connector_type, source_type,
                       status, items_ingested, last_fetch_at, error_count,
                       poll_interval
                FROM connectors
                WHERE tenant_id = %s
                ORDER BY created_at DESC LIMIT %s
            """, (tenant_id, min(limit, 1000)))
            return [
                {
                    "connector_id": r[0], "device_id": r[1],
                    "connector_type": r[2], "source_type": r[3],
                    "status": r[4], "items_ingested": r[5],
                    "last_fetch_at": r[6].isoformat() if r[6] else None,
                    "error_count": r[7],
                    # poll_interval as datetime.timedelta — scheduler reads it
                    # via hasattr(.., 'total_seconds'). Without this column the
                    # scheduler skipped every connector silently.
                    "poll_interval": r[8],
                }
                for r in cur.fetchall()
            ]


def trigger_connector(
    connector_id: str, tenant_id: str, dsn: Optional[str] = None,
) -> Optional[FetchResult]:
    """Manually trigger a connector fetch cycle.

    Secret references ($secret:name) in source_config are resolved
    to actual values before passing to the connector.
    """
    conn_data = get_connector(connector_id, tenant_id, mask=False, dsn=dsn)
    if not conn_data:
        return None

    cls = _get_connector_class(conn_data["source_type"])
    if not cls:
        return FetchResult(
            items_fetched=0, items_ingested=0,
            errors=[f"No connector class for source_type: {conn_data['source_type']}"],
        )

    # Resolve any $secret:name references to actual values
    resolved_config = resolve_secrets(conn_data["source_config"], tenant_id, dsn)

    connector = cls(
        connector_id=connector_id,
        device_id=conn_data["device_id"],
        tenant_id=tenant_id,
        source_config=resolved_config,
        watermark=conn_data.get("watermark"),
    )

    result = connector.run()

    # Update connector stats
    _update_connector_stats(connector_id, result, dsn)

    return result


def _update_connector_stats(
    connector_id: str, result: FetchResult, dsn: Optional[str] = None,
):
    """Update connector stats after a fetch cycle."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            if result.errors:
                cur.execute("""
                    UPDATE connectors SET
                        last_fetch_at = NOW(),
                        last_error = %s,
                        error_count = error_count + 1,
                        watermark = COALESCE(%s, watermark),
                        updated_at = NOW()
                    WHERE connector_id = %s
                """, (
                    result.errors[0],
                    json.dumps(result.new_watermark) if result.new_watermark else None,
                    connector_id,
                ))
            else:
                cur.execute("""
                    UPDATE connectors SET
                        last_fetch_at = NOW(),
                        last_error = NULL,
                        error_count = 0,
                        items_ingested = items_ingested + %s,
                        watermark = COALESCE(%s, watermark),
                        updated_at = NOW()
                    WHERE connector_id = %s
                """, (
                    result.items_ingested,
                    json.dumps(result.new_watermark) if result.new_watermark else None,
                    connector_id,
                ))
            conn.commit()
