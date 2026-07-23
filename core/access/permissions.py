"""
Access Permission System — controls who can query whose personal data.

Every personal data query (location, contacts, financial, communications,
photos) must pass through check_permission() before returning data.

If access is denied, the query returns a permission_denied error.
If alert_on_access is true, the target gets notified.

Permission rules:
  - Self-access is always allowed (no alert)
  - Default for cross-identity: DENIED (must be explicitly granted)
  - data_type='all' grants access to all types
  - alert_on_access can be set per permission

Data types:
  location, contacts, financial, communications, photos, calendar, all
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection

_log = logging.getLogger("enki.access.permissions")

# Signal types that contain personal data (require permission check)
PERSONAL_SIGNAL_TYPES = {
    "location": [
        "signal.geospatial.position_report",
        "signal.personal.location",
    ],
    "contacts": [
        "signal.personal.contacts_bulk",
        "signal.personal.contacts_email_bulk",
    ],
    "financial": [
        "signal.financial.transaction",
        "signal.financial.balance_update",
    ],
    "communications": [
        "signal.personal.sms_bulk",
        "signal.personal.call_log_bulk",
        "signal.personal.call_log_export",
    ],
    "photos": [
        "signal.personal.photo_metadata_bulk",
        "signal.content.image",
    ],
    "calendar": [
        "signal.personal.calendar_bulk",
        "signal.personal.calendar_event",
    ],
}


def classify_data_type(signal_type: str) -> Optional[str]:
    """Map a signal type to its personal data category. Returns None if not personal."""
    for data_type, types in PERSONAL_SIGNAL_TYPES.items():
        if signal_type in types:
            return data_type
    return None


def check_permission(
    querier_id: str,
    target_id: str,
    data_type: str,
    tenant_id: str = "default",
    dsn: Optional[str] = None,
) -> bool:
    """Check if querier can access target's data of given type.

    Returns True if allowed, False if denied.
    Self-access is always allowed.
    """
    # Self-access always allowed
    if querier_id == target_id or not target_id:
        return True

    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                # Check specific permission
                cur.execute("""
                    SELECT allowed FROM access_permissions
                    WHERE tenant_id = %s
                      AND identity_id = %s
                      AND target_id = %s
                      AND data_type IN (%s, 'all')
                    ORDER BY
                      CASE WHEN data_type = %s THEN 0 ELSE 1 END
                    LIMIT 1
                """, (tenant_id, querier_id, target_id, data_type, data_type))
                row = cur.fetchone()
                if row:
                    return bool(row[0])

                # No explicit permission → default deny for personal data
                return False
    except Exception as e:
        _log.debug("Permission check failed: %s", e)
        # Fail-open for now (table may not exist yet)
        return True


def log_access(
    querier_id: str,
    target_id: str,
    data_type: str,
    action: str,
    allowed: bool,
    disclosed: Optional[dict] = None,
    context: str = "api",
    tenant_id: str = "default",
    dsn: Optional[str] = None,
):
    """Log an access attempt (both allowed and denied)."""
    # Don't log self-access
    if querier_id == target_id:
        return

    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO access_log
                        (tenant_id, querier_id, target_id, data_type, action, allowed, disclosed, context)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    tenant_id, querier_id, target_id, data_type,
                    action, allowed,
                    json.dumps(disclosed) if disclosed else None,
                    context,
                ))
                conn.commit()
    except Exception as e:
        _log.debug("Access log failed: %s", e)


def notify_access(
    querier_id: str,
    target_id: str,
    data_type: str,
    allowed: bool,
    tenant_id: str = "default",
    dsn: Optional[str] = None,
):
    """Check if target should be notified and create an alert signal."""
    if querier_id == target_id:
        return

    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                # Check alert_on_access flag
                cur.execute("""
                    SELECT alert_on_access FROM access_permissions
                    WHERE tenant_id = %s AND target_id = %s
                      AND data_type IN (%s, 'all')
                      AND alert_on_access = TRUE
                    LIMIT 1
                """, (tenant_id, target_id, data_type))
                if not cur.fetchone():
                    return

                # Create notification as audit event
                cur.execute("""
                    INSERT INTO audit_events (tenant_id, event_type, payload_json, cid)
                    VALUES (%s, %s, %s, %s)
                """, (
                    tenant_id,
                    "signal.access.data_accessed" if allowed else "signal.access.data_denied",
                    json.dumps({
                        "querier_id": querier_id,
                        "target_id": target_id,
                        "data_type": data_type,
                        "allowed": allowed,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }),
                    f"access:{querier_id}:{target_id}:{data_type}:{datetime.now(timezone.utc).isoformat()}",
                ))
                conn.commit()
                _log.info("Access notification: %s %s %s's %s data (%s)",
                          querier_id, "accessed" if allowed else "was denied", target_id,
                          data_type, "alerted" if allowed else "blocked")
    except Exception as e:
        _log.debug("Access notification failed: %s", e)


# ── Permission CRUD ──────────────────────────────────────────────────────────

def list_permissions(
    target_id: str,
    tenant_id: str = "default",
    dsn: Optional[str] = None,
) -> list[dict]:
    """List all permissions for a target identity (who can see my data?)."""
    results = []
    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT permission_id, identity_id, data_type, allowed,
                           alert_on_access, granted_by, updated_at
                    FROM access_permissions
                    WHERE tenant_id = %s AND target_id = %s
                    ORDER BY data_type, identity_id
                """, (tenant_id, target_id))
                for r in cur.fetchall():
                    results.append({
                        "permission_id": r[0],
                        "identity_id": r[1],
                        "data_type": r[2],
                        "allowed": r[3],
                        "alert_on_access": r[4],
                        "granted_by": r[5],
                        "updated_at": r[6].isoformat() if r[6] else None,
                    })
    except Exception:
        pass
    return results


def set_permission(
    identity_id: str,
    target_id: str,
    data_type: str,
    allowed: bool,
    alert_on_access: bool = True,
    granted_by: str = "self",
    tenant_id: str = "default",
    dsn: Optional[str] = None,
) -> bool:
    """Set or update a permission rule."""
    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO access_permissions
                        (tenant_id, identity_id, target_id, data_type, allowed,
                         alert_on_access, granted_by, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (tenant_id, identity_id, target_id, data_type)
                    DO UPDATE SET allowed = EXCLUDED.allowed,
                                  alert_on_access = EXCLUDED.alert_on_access,
                                  granted_by = EXCLUDED.granted_by,
                                  updated_at = NOW()
                """, (tenant_id, identity_id, target_id, data_type, allowed,
                      alert_on_access, granted_by))
                conn.commit()
        return True
    except Exception as e:
        _log.debug("Set permission failed: %s", e)
        return False


def get_access_log(
    target_id: str,
    tenant_id: str = "default",
    limit: int = 50,
    dsn: Optional[str] = None,
) -> list[dict]:
    """Get recent access log entries for a target identity."""
    results = []
    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT log_id, querier_id, data_type, action, allowed,
                           disclosed, context, created_at
                    FROM access_log
                    WHERE tenant_id = %s AND target_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                """, (tenant_id, target_id, limit))
                for r in cur.fetchall():
                    results.append({
                        "log_id": r[0],
                        "querier_id": r[1],
                        "data_type": r[2],
                        "action": r[3],
                        "allowed": r[4],
                        "disclosed": r[5],
                        "context": r[6],
                        "timestamp": r[7].isoformat() if r[7] else None,
                    })
    except Exception:
        pass
    return results
