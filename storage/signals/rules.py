"""
Detection rule storage operations.

Rules define conditions that trigger alerts on incoming data.
Any data type, any field, any condition.
"""

import json
import uuid
from typing import Optional

from storage.postgres.db import get_connection


def create_rule(
    tenant_id: str,
    name: str,
    rule_type: str,
    severity: str,
    condition: dict,
    description: Optional[str] = None,
    category: Optional[str] = None,
    actions: Optional[dict] = None,
    created_by: str = "system",
    dsn: Optional[str] = None,
) -> dict:
    """Create a detection rule."""
    rule_id = f"rule_{uuid.uuid4().hex[:12]}"

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO detection_rules
                    (rule_id, tenant_id, name, description, rule_type,
                     severity, category, condition, actions, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                rule_id, tenant_id, name, description, rule_type,
                severity, category, json.dumps(condition),
                json.dumps(actions or {}), created_by,
            ))
            conn.commit()

    return {"rule_id": rule_id, "name": name, "rule_type": rule_type,
            "severity": severity, "enabled": True}


def get_rule(rule_id: str, tenant_id: str, dsn: Optional[str] = None) -> Optional[dict]:
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT rule_id, name, description, rule_type, enabled,
                       severity, category, condition, actions,
                       match_count, last_matched_at, created_by,
                       created_at, updated_at
                FROM detection_rules
                WHERE rule_id = %s AND tenant_id = %s
            """, (rule_id, tenant_id))
            r = cur.fetchone()
            if not r:
                return None
            return {
                "rule_id": r[0], "name": r[1], "description": r[2],
                "rule_type": r[3], "enabled": r[4], "severity": r[5],
                "category": r[6], "condition": r[7], "actions": r[8],
                "match_count": r[9],
                "last_matched_at": r[10].isoformat() if r[10] else None,
                "created_by": r[11],
                "created_at": r[12].isoformat(), "updated_at": r[13].isoformat(),
            }


def list_rules(
    tenant_id: str, enabled_only: bool = False,
    rule_type: Optional[str] = None, limit: int = 100,
    dsn: Optional[str] = None,
) -> list[dict]:
    conditions = ["tenant_id = %s"]
    params: list = [tenant_id]
    if enabled_only:
        conditions.append("enabled = TRUE")
    if rule_type:
        conditions.append("rule_type = %s")
        params.append(rule_type)
    params.append(min(limit, 1000))

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT rule_id, name, rule_type, enabled, severity, category,
                       match_count, created_at
                FROM detection_rules
                WHERE {" AND ".join(conditions)}
                ORDER BY created_at DESC LIMIT %s
            """, params)
            return [
                {"rule_id": r[0], "name": r[1], "rule_type": r[2],
                 "enabled": r[3], "severity": r[4], "category": r[5],
                 "match_count": r[6], "created_at": r[7].isoformat()}
                for r in cur.fetchall()
            ]


def update_rule(
    rule_id: str, tenant_id: str,
    enabled: Optional[bool] = None,
    condition: Optional[dict] = None,
    severity: Optional[str] = None,
    dsn: Optional[str] = None,
) -> bool:
    sets = ["updated_at = NOW()"]
    params: list = []
    if enabled is not None:
        sets.append("enabled = %s")
        params.append(enabled)
    if condition is not None:
        sets.append("condition = %s")
        params.append(json.dumps(condition))
    if severity is not None:
        sets.append("severity = %s")
        params.append(severity)
    params.extend([rule_id, tenant_id])

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE detection_rules SET {", ".join(sets)}
                WHERE rule_id = %s AND tenant_id = %s
            """, params)
            ok = cur.rowcount > 0
            conn.commit()
            return ok


def delete_rule(rule_id: str, tenant_id: str, dsn: Optional[str] = None) -> bool:
    """Soft delete (disable) a rule."""
    return update_rule(rule_id, tenant_id, enabled=False, dsn=dsn)
