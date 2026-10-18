"""
Signal organization — automatic filing and categorization.

When signals are ingested, they're classified by signal_type.
This module provides organized VIEWS of that data — grouped by
domain, source, time period, and auto-generated tags.

The organization is DERIVED, not stored. It's a query layer over
the signals table, not a separate data structure. This means:
  - No duplication
  - Always up to date
  - No sync issues
  - Organization changes instantly when vocabulary changes

The user sees their data like this:

  📁 Financial (47 signals)
  ├── 💰 Transactions (32)
  ├── 📄 Invoices (10)
  └── 💳 Payments (5)

  📁 Communications (123 signals)
  ├── ✉️ Email (89)
  ├── 💬 Chat Messages (30)
  └── 📱 SMS (4)

  📁 Security (15 signals)
  ├── 🔒 Auth Failures (8)
  ├── 🛡️ Intrusion Detected (4)
  └── ⚠️ Policy Violation (3)

  📁 Content (200 signals)
  ├── 🖼️ Images (150)
  ├── 📄 Documents (35)
  └── 📰 Articles (15)
"""

from typing import Optional
from storage.postgres.db import get_connection


def get_organized_view(
    tenant_id: str,
    dsn: Optional[str] = None,
) -> list[dict]:
    """
    Get signals organized by domain with counts and recent items.

    Returns a list of domain groups, each with sub-type counts
    and the most recent signal per sub-type.
    """
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Get counts by signal_type, grouped by domain
            cur.execute("""
                SELECT
                    signal_type,
                    COUNT(*) as count,
                    MAX(timestamp) as latest,
                    COUNT(*) FILTER (WHERE timestamp > NOW() - INTERVAL '24 hours') as last_24h
                FROM signals
                WHERE tenant_id = %s
                GROUP BY signal_type
                ORDER BY count DESC
            """, (tenant_id,))

            type_counts = {}
            for row in cur.fetchall():
                signal_type = row[0]
                parts = signal_type.split(".")
                domain = parts[1] if len(parts) >= 2 else "unknown"
                specific = parts[2] if len(parts) >= 3 else signal_type

                if domain not in type_counts:
                    type_counts[domain] = {
                        "domain": domain,
                        "total": 0,
                        "last_24h": 0,
                        "types": [],
                    }

                type_counts[domain]["total"] += row[1]
                type_counts[domain]["last_24h"] += row[3]
                type_counts[domain]["types"].append({
                    "signal_type": signal_type,
                    "label": specific.replace("_", " ").title(),
                    "count": row[1],
                    "last_24h": row[3],
                    "latest": row[2].isoformat() if row[2] else None,
                })

    # Sort domains by total count
    domains = sorted(type_counts.values(), key=lambda d: d["total"], reverse=True)
    return domains


def get_domain_signals(
    tenant_id: str,
    domain: str,
    signal_type: Optional[str] = None,
    limit: int = 100,
    dsn: Optional[str] = None,
) -> list[dict]:
    """
    Get signals within a specific domain (e.g., "financial", "communications").
    Optionally filter to a specific type within the domain.
    """
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            if signal_type:
                cur.execute("""
                    SELECT signal_id, signal_type, source_domain, severity,
                           timestamp, device_id, payload, cid
                    FROM signals
                    WHERE tenant_id = %s AND signal_type = %s
                    ORDER BY timestamp DESC LIMIT %s
                """, (tenant_id, signal_type, min(limit, 1000)))
            else:
                cur.execute("""
                    SELECT signal_id, signal_type, source_domain, severity,
                           timestamp, device_id, payload, cid
                    FROM signals
                    WHERE tenant_id = %s AND signal_type LIKE %s
                    ORDER BY timestamp DESC LIMIT %s
                """, (tenant_id, f"signal.{domain}.%", min(limit, 1000)))

            return [
                {
                    "signal_id": r[0], "signal_type": r[1],
                    "source_domain": r[2], "severity": r[3],
                    "timestamp": r[4].isoformat(),
                    "device_id": r[5], "payload": r[6], "cid": r[7],
                }
                for r in cur.fetchall()
            ]


def get_source_breakdown(
    tenant_id: str,
    dsn: Optional[str] = None,
) -> list[dict]:
    """
    Get signals organized by SOURCE (which device/connector sent them).
    """
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT device_id, source_domain,
                       COUNT(*) as count,
                       COUNT(DISTINCT signal_type) as type_count,
                       MAX(timestamp) as latest,
                       COUNT(*) FILTER (WHERE timestamp > NOW() - INTERVAL '24 hours') as last_24h
                FROM signals
                WHERE tenant_id = %s
                GROUP BY device_id, source_domain
                ORDER BY count DESC
                LIMIT 100
            """, (tenant_id,))

            return [
                {
                    "device_id": r[0], "source_domain": r[1],
                    "signal_count": r[2], "type_count": r[3],
                    "latest": r[4].isoformat() if r[4] else None,
                    "last_24h": r[5],
                }
                for r in cur.fetchall()
            ]


def get_timeline_view(
    tenant_id: str,
    hours: int = 24,
    bucket_minutes: int = 60,
    dsn: Optional[str] = None,
) -> list[dict]:
    """
    Get signal volume over time, bucketed by interval.
    Returns time-series data for charts.
    """
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    date_trunc('hour', timestamp) as bucket,
                    COUNT(*) as count,
                    COUNT(DISTINCT signal_type) as type_count,
                    COUNT(DISTINCT device_id) as device_count
                FROM signals
                WHERE tenant_id = %s
                  AND timestamp > NOW() - make_interval(hours => %s)
                GROUP BY bucket
                ORDER BY bucket ASC
            """, (tenant_id, hours))

            return [
                {
                    "timestamp": r[0].isoformat(),
                    "signal_count": r[1],
                    "type_count": r[2],
                    "device_count": r[3],
                }
                for r in cur.fetchall()
            ]


def auto_tag_signal(signal_type: str, payload: dict) -> list[str]:
    """
    Generate automatic tags from signal type and content.

    Tags are lightweight labels for filtering and grouping.
    They're derived, not stored — computed at query time
    or at ingestion time and stored in the signal's metadata.

    Examples:
      signal.financial.transaction + amount > 10000 → ["high_value"]
      signal.communications.email + attachment_count > 0 → ["has_attachments"]
      signal.security.* → ["security"]
      signal.personal.location → ["location", "gps"]
    """
    tags = []

    # Domain tag
    parts = signal_type.split(".")
    if len(parts) >= 2:
        tags.append(parts[1])  # "financial", "security", etc.

    # Content-based tags
    if isinstance(payload, dict):
        # Financial
        amount = payload.get("amount")
        if amount is not None:
            try:
                if float(amount) > 10000:
                    tags.append("high_value")
                if float(amount) > 50000:
                    tags.append("very_high_value")
            except (ValueError, TypeError):
                pass

        # Email
        if payload.get("attachment_count", 0) > 0:
            tags.append("has_attachments")
        if payload.get("to") and isinstance(payload["to"], list) and len(payload["to"]) > 5:
            tags.append("mass_recipient")

        # Location
        if payload.get("lat") or payload.get("latitude"):
            tags.append("has_location")

        # Media
        if payload.get("format") in ("jpeg", "png", "gif", "webp"):
            tags.append("image")
        if payload.get("format") == "pdf":
            tags.append("document")

        # Security
        if payload.get("severity") in ("critical", "high"):
            tags.append("high_severity")

    return tags
