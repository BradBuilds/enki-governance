"""
Case and alert storage operations.

Cases organize related data around a topic/investigation.
Alerts are conditions that need attention.
Both are domain-agnostic — same lifecycle for any data type.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from storage.postgres.db import get_connection


# ─── CASES ─────────────────────────────────────────────────────────────────────

def create_case(
    tenant_id: str,
    title: str,
    priority: str = "medium",
    description: Optional[str] = None,
    owner: Optional[str] = None,
    tags: Optional[list[str]] = None,
    created_by: str = "system",
    dsn: Optional[str] = None,
) -> dict:
    """Create a new case. Records creation event in audit trail."""
    case_id = f"case_{uuid.uuid4().hex[:12]}"

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO cases
                    (case_id, tenant_id, status, priority, title,
                     description, owner, tags)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                case_id, tenant_id, "open", priority, title,
                description, owner, tags or [],
            ))

            # Record creation event
            cur.execute("""
                INSERT INTO case_events
                    (tenant_id, case_id, event_type, actor_id, new_value)
                VALUES (%s, %s, %s, %s, %s)
            """, (
                tenant_id, case_id, "status_changed", created_by,
                json.dumps({"status": "open", "title": title}),
            ))

            conn.commit()

    return {
        "case_id": case_id,
        "tenant_id": tenant_id,
        "status": "open",
        "priority": priority,
        "title": title,
    }


def get_case(case_id: str, tenant_id: str, dsn: Optional[str] = None) -> Optional[dict]:
    """Get a case by ID with signal/alert counts."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.case_id, c.tenant_id, c.status, c.priority, c.title,
                       c.description, c.owner, c.tags,
                       c.resolution_type, c.resolution_summary,
                       c.created_at, c.updated_at,
                       (SELECT COUNT(*) FROM case_signals cs WHERE cs.case_id = c.case_id) as signal_count,
                       (SELECT COUNT(*) FROM case_alerts ca WHERE ca.case_id = c.case_id) as alert_count,
                       (SELECT COUNT(*) FROM case_observables co WHERE co.case_id = c.case_id AND co.active = TRUE) as observable_count
                FROM cases c
                WHERE c.case_id = %s AND c.tenant_id = %s
            """, (case_id, tenant_id))
            row = cur.fetchone()
            if not row:
                return None
            return {
                "case_id": row[0], "tenant_id": row[1], "status": row[2],
                "priority": row[3], "title": row[4], "description": row[5],
                "owner": row[6], "tags": row[7],
                "resolution_type": row[8], "resolution_summary": row[9],
                "created_at": row[10].isoformat(), "updated_at": row[11].isoformat(),
                "signal_count": row[12], "alert_count": row[13],
                "observable_count": row[14],
            }


def list_cases(
    tenant_id: str,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    owner: Optional[str] = None,
    limit: int = 100,
    dsn: Optional[str] = None,
) -> list[dict]:
    """List cases with optional filters."""
    conditions = ["tenant_id = %s"]
    params: list = [tenant_id]

    if status:
        conditions.append("status = %s")
        params.append(status)
    if priority:
        conditions.append("priority = %s")
        params.append(priority)
    if owner:
        conditions.append("owner = %s")
        params.append(owner)

    params.append(min(limit, 1000))
    where = " AND ".join(conditions)

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT case_id, status, priority, title, owner, tags,
                       created_at, updated_at
                FROM cases
                WHERE {where}
                ORDER BY updated_at DESC
                LIMIT %s
            """, params)
            return [
                {
                    "case_id": r[0], "status": r[1], "priority": r[2],
                    "title": r[3], "owner": r[4], "tags": r[5],
                    "created_at": r[6].isoformat(), "updated_at": r[7].isoformat(),
                }
                for r in cur.fetchall()
            ]


def update_case_status(
    case_id: str,
    tenant_id: str,
    new_status: str,
    actor_id: str,
    resolution_type: Optional[str] = None,
    resolution_summary: Optional[str] = None,
    dsn: Optional[str] = None,
) -> bool:
    """Update case status. Records event in audit trail."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Get current status
            cur.execute("SELECT status FROM cases WHERE case_id = %s AND tenant_id = %s",
                        (case_id, tenant_id))
            row = cur.fetchone()
            if not row:
                return False
            old_status = row[0]

            # Update
            if resolution_type:
                cur.execute("""
                    UPDATE cases SET status = %s, resolution_type = %s,
                        resolution_summary = %s, updated_at = NOW()
                    WHERE case_id = %s AND tenant_id = %s
                """, (new_status, resolution_type, resolution_summary, case_id, tenant_id))
            else:
                cur.execute("""
                    UPDATE cases SET status = %s, updated_at = NOW()
                    WHERE case_id = %s AND tenant_id = %s
                """, (new_status, case_id, tenant_id))

            # Audit trail
            cur.execute("""
                INSERT INTO case_events
                    (tenant_id, case_id, event_type, actor_id, old_value, new_value)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                tenant_id, case_id, "status_changed", actor_id,
                json.dumps({"status": old_status}),
                json.dumps({"status": new_status}),
            ))

            conn.commit()
            return True


# ─── CASE OBSERVABLES (watchlist) ──────────────────────────────────────────────

def add_case_observable(
    case_id: str,
    tenant_id: str,
    observable_type: str,
    observable_value: str,
    added_by: str,
    notes: Optional[str] = None,
    dsn: Optional[str] = None,
) -> dict:
    """Add an observable to a case's watchlist. Enables auto-correlation."""
    # Normalize the value (basic — more sophisticated per type in future)
    normalized = observable_value.strip().lower()

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO case_observables
                    (tenant_id, case_id, observable_type, observable_value,
                     normalized_value, added_by, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (case_id, observable_type, normalized_value) DO NOTHING
                RETURNING rid
            """, (
                tenant_id, case_id, observable_type, observable_value,
                normalized, added_by, notes,
            ))
            row = cur.fetchone()

            # Audit trail
            cur.execute("""
                INSERT INTO case_events
                    (tenant_id, case_id, event_type, actor_id, new_value)
                VALUES (%s, %s, %s, %s, %s)
            """, (
                tenant_id, case_id, "observable_added", added_by,
                json.dumps({"type": observable_type, "value": observable_value}),
            ))

            conn.commit()

    return {
        "case_id": case_id,
        "observable_type": observable_type,
        "observable_value": observable_value,
        "normalized_value": normalized,
        "added": row is not None,
    }


def list_case_observables(
    case_id: str, tenant_id: str, dsn: Optional[str] = None,
) -> list[dict]:
    """List active observables on a case's watchlist."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT observable_type, observable_value, normalized_value,
                       added_by, added_at, notes
                FROM case_observables
                WHERE case_id = %s AND tenant_id = %s AND active = TRUE
                ORDER BY added_at DESC
            """, (case_id, tenant_id))
            return [
                {
                    "observable_type": r[0], "observable_value": r[1],
                    "normalized_value": r[2], "added_by": r[3],
                    "added_at": r[4].isoformat(), "notes": r[5],
                }
                for r in cur.fetchall()
            ]


def get_case_timeline(
    case_id: str, tenant_id: str, limit: int = 100, dsn: Optional[str] = None,
) -> list[dict]:
    """Get case events timeline (audit trail)."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT event_type, actor_id, old_value, new_value, commit_time
                FROM case_events
                WHERE case_id = %s AND tenant_id = %s
                ORDER BY commit_time DESC
                LIMIT %s
            """, (case_id, tenant_id, min(limit, 1000)))
            return [
                {
                    "event_type": r[0], "actor_id": r[1],
                    "old_value": r[2], "new_value": r[3],
                    "timestamp": r[4].isoformat(),
                }
                for r in cur.fetchall()
            ]


# ─── ALERTS ────────────────────────────────────────────────────────────────────

def get_alert(alert_id: str, tenant_id: str, dsn: Optional[str] = None) -> Optional[dict]:
    """Get a single alert by ID."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT alert_id, tenant_id, rule_id, signal_rids,
                       status, severity, category, title, description,
                       assigned_to, enrichment, suppression_reason,
                       created_at, updated_at
                FROM alerts
                WHERE alert_id = %s AND tenant_id = %s
            """, (alert_id, tenant_id))
            row = cur.fetchone()
            if not row:
                return None
            return {
                "alert_id": row[0], "tenant_id": row[1], "rule_id": row[2],
                "signal_rids": row[3], "status": row[4], "severity": row[5],
                "category": row[6], "title": row[7], "description": row[8],
                "assigned_to": row[9], "enrichment": row[10],
                "suppression_reason": row[11],
                "created_at": row[12].isoformat(), "updated_at": row[13].isoformat(),
            }


def list_alerts(
    tenant_id: str,
    status: Optional[str] = None,
    severity: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = 100,
    dsn: Optional[str] = None,
) -> list[dict]:
    """List alerts with filters."""
    conditions = ["tenant_id = %s"]
    params: list = [tenant_id]

    if status:
        conditions.append("status = %s")
        params.append(status)
    if severity:
        conditions.append("severity = %s")
        params.append(severity)
    if category:
        conditions.append("category = %s")
        params.append(category)

    params.append(min(limit, 1000))
    where = " AND ".join(conditions)

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT alert_id, rule_id, status, severity, category,
                       title, created_at, updated_at
                FROM alerts
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT %s
            """, params)
            return [
                {
                    "alert_id": r[0], "rule_id": r[1], "status": r[2],
                    "severity": r[3], "category": r[4], "title": r[5],
                    "created_at": r[6].isoformat(), "updated_at": r[7].isoformat(),
                }
                for r in cur.fetchall()
            ]


def update_alert_status(
    alert_id: str,
    tenant_id: str,
    new_status: str,
    actor_id: str,
    suppression_reason: Optional[str] = None,
    assigned_to: Optional[str] = None,
    dsn: Optional[str] = None,
) -> bool:
    """Update alert status (triage, suppress, escalate, etc.)."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            sets = ["status = %s", "updated_at = NOW()"]
            params: list = [new_status]

            if suppression_reason:
                sets.append("suppression_reason = %s")
                params.append(suppression_reason)
            if assigned_to:
                sets.append("assigned_to = %s")
                params.append(assigned_to)

            params.extend([alert_id, tenant_id])

            cur.execute(f"""
                UPDATE alerts SET {', '.join(sets)}
                WHERE alert_id = %s AND tenant_id = %s
            """, params)

            updated = cur.rowcount > 0
            conn.commit()
            return updated


def escalate_alert_to_case(
    alert_id: str,
    tenant_id: str,
    actor_id: str,
    case_title: Optional[str] = None,
    dsn: Optional[str] = None,
) -> Optional[dict]:
    """Escalate an alert to a new case. Links alert to case."""
    alert = get_alert(alert_id, tenant_id, dsn)
    if not alert:
        return None

    title = case_title or alert["title"]
    case = create_case(
        tenant_id=tenant_id,
        title=title,
        priority=alert["severity"],
        description=f"Escalated from alert {alert_id}",
        created_by=actor_id,
        dsn=dsn,
    )

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Link alert to case
            cur.execute("""
                INSERT INTO case_alerts (case_id, alert_id, linked_by)
                VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (case["case_id"], alert_id, actor_id))

            # Link alert's signals to case
            if alert.get("signal_rids"):
                for rid in alert["signal_rids"]:
                    cur.execute("""
                        INSERT INTO case_signals (case_id, signal_rid, linked_by, link_reason)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                    """, (case["case_id"], rid, actor_id, "alert_escalation"))

            # Update alert status
            cur.execute("""
                UPDATE alerts SET status = 'escalated', updated_at = NOW()
                WHERE alert_id = %s AND tenant_id = %s
            """, (alert_id, tenant_id))

            conn.commit()

    return case
