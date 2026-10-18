"""
Dashboard aggregation queries.

Provides summary stats for the dashboard view.
All queries scoped to tenant_id.
"""

from typing import Optional
from storage.postgres.db import get_connection


def get_dashboard_summary(tenant_id: str, dsn: Optional[str] = None) -> dict:
    """Get high-level stats for the dashboard."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Signal counts
            cur.execute("""
                SELECT COUNT(*),
                       COUNT(*) FILTER (WHERE timestamp > NOW() - INTERVAL '24 hours'),
                       COUNT(*) FILTER (WHERE timestamp > NOW() - INTERVAL '1 hour')
                FROM signals WHERE tenant_id = %s
            """, (tenant_id,))
            sig = cur.fetchone()

            # Alert counts by status
            cur.execute("""
                SELECT status, COUNT(*) FROM alerts
                WHERE tenant_id = %s GROUP BY status
            """, (tenant_id,))
            alert_by_status = {r[0]: r[1] for r in cur.fetchall()}

            # Case counts by status
            cur.execute("""
                SELECT status, COUNT(*) FROM cases
                WHERE tenant_id = %s GROUP BY status
            """, (tenant_id,))
            case_by_status = {r[0]: r[1] for r in cur.fetchall()}

            # LDU-P pending count
            cur.execute("""
                SELECT COUNT(*) FROM ldu_p_entries
                WHERE tenant_id = %s AND resolution = 'pending_review'
            """, (tenant_id,))
            ldu_p_pending = cur.fetchone()[0]

            # Top signal types (last 24h)
            cur.execute("""
                SELECT signal_type, COUNT(*) FROM signals
                WHERE tenant_id = %s AND timestamp > NOW() - INTERVAL '24 hours'
                GROUP BY signal_type ORDER BY COUNT(*) DESC LIMIT 10
            """, (tenant_id,))
            top_types = [{"type": r[0], "count": r[1]} for r in cur.fetchall()]

            return {
                "signals": {
                    "total": sig[0],
                    "last_24h": sig[1],
                    "last_1h": sig[2],
                },
                "alerts": {
                    "total": sum(alert_by_status.values()),
                    "by_status": alert_by_status,
                    "new": alert_by_status.get("new", 0),
                },
                "cases": {
                    "total": sum(case_by_status.values()),
                    "by_status": case_by_status,
                    "open": case_by_status.get("open", 0) + case_by_status.get("investigating", 0),
                },
                "ldu_p_pending": ldu_p_pending,
                "top_signal_types_24h": top_types,
            }
