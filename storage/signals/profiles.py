"""
Profile management — identities, tenants, user preferences.

Connects everything: who you are, what devices you have,
what you see, what projects you own, what data is yours.
"""

import json
import uuid
from typing import Optional

from storage.postgres.db import get_connection


# ─── Tenants ───────────────────────────────────────────────────────────────────

def create_tenant(
    tenant_id: str,
    display_name: str,
    node_id: str,
    dsn: Optional[str] = None,
) -> dict:
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO tenants (tenant_id, node_id, display_name)
                VALUES (%s, %s, %s)
                ON CONFLICT (tenant_id) DO NOTHING
            """, (tenant_id, node_id, display_name))
            conn.commit()
    return {"tenant_id": tenant_id, "display_name": display_name}


def get_tenant(tenant_id: str, dsn: Optional[str] = None) -> Optional[dict]:
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT tenant_id, node_id, display_name, status, created_at
                FROM tenants WHERE tenant_id = %s
            """, (tenant_id,))
            r = cur.fetchone()
            if not r:
                return None
            return {"tenant_id": r[0], "node_id": r[1], "display_name": r[2],
                    "status": r[3], "created_at": r[4].isoformat()}


def list_tenants(dsn: Optional[str] = None) -> list[dict]:
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tenant_id, display_name, status, created_at FROM tenants ORDER BY created_at")
            return [{"tenant_id": r[0], "display_name": r[1], "status": r[2],
                     "created_at": r[3].isoformat()} for r in cur.fetchall()]


# ─── Identities ───────────────────────────────────────────────────────────────

def create_identity(
    tenant_id: str,
    display_name: str,
    role: str = "member",
    metadata: Optional[dict] = None,
    dsn: Optional[str] = None,
) -> dict:
    identity_id = f"person_{display_name.lower().replace(' ', '_')}_{uuid.uuid4().hex[:8]}"
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Create identity
            cur.execute("""
                INSERT INTO identities (identity_id, tenant_id, display_name, role, metadata)
                VALUES (%s, %s, %s, %s, %s)
            """, (identity_id, tenant_id, display_name, role, json.dumps(metadata or {})))

            # Create default user preferences
            default_view = {"family": "map", "analyst": "alerts", "le": "cases"}.get(role, "dashboard")
            cur.execute("""
                INSERT INTO user_preferences (identity_id, tenant_id, role, default_view)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (identity_id, tenant_id, role, default_view))

            conn.commit()

    return {"identity_id": identity_id, "display_name": display_name, "role": role}


def get_identity(identity_id: str, tenant_id: str, dsn: Optional[str] = None) -> Optional[dict]:
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT identity_id, display_name, role, metadata, created_at
                FROM identities WHERE identity_id = %s AND tenant_id = %s
            """, (identity_id, tenant_id))
            r = cur.fetchone()
            if not r:
                return None
            return {"identity_id": r[0], "display_name": r[1], "role": r[2],
                    "metadata": r[3], "created_at": r[4].isoformat()}


def list_identities(tenant_id: str, dsn: Optional[str] = None) -> list[dict]:
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT identity_id, display_name, role, created_at
                FROM identities WHERE tenant_id = %s ORDER BY created_at
            """, (tenant_id,))
            return [{"identity_id": r[0], "display_name": r[1], "role": r[2],
                     "created_at": r[3].isoformat()} for r in cur.fetchall()]


# ─── Logto → Enki Identity Link ──────────────────────────────────────────────

def find_or_create_by_logto_sub(
    logto_sub: str,
    tenant_id: str,
    display_name: str = "New User",
    email: Optional[str] = None,
    dsn: Optional[str] = None,
) -> dict:
    """
    Look up an Enki identity by Logto sub. If none exists, create one.

    This is called on every authenticated request to resolve
    the Logto JWT sub → Enki identity_id.
    """
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Try to find existing identity
            cur.execute("""
                SELECT identity_id, display_name, role, metadata, created_at
                FROM identities WHERE logto_sub = %s AND tenant_id = %s
            """, (logto_sub, tenant_id))
            r = cur.fetchone()
            if r:
                return {
                    "identity_id": r[0], "display_name": r[1], "role": r[2],
                    "metadata": r[3], "created_at": r[4].isoformat(),
                    "is_new": False,
                }

            # First login — create identity
            # First user on a node gets admin role
            cur.execute("SELECT COUNT(*) FROM identities WHERE tenant_id = %s", (tenant_id,))
            count = cur.fetchone()[0]
            role = "admin" if count == 0 else "member"

            identity_id = f"person_{uuid.uuid4().hex[:12]}"
            metadata = {}
            if email:
                metadata["email"] = email

            cur.execute("""
                INSERT INTO identities (identity_id, tenant_id, display_name, role, logto_sub, metadata)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (identity_id, tenant_id, display_name, role, logto_sub, json.dumps(metadata)))

            # Create default preferences
            cur.execute("""
                INSERT INTO user_preferences (identity_id, tenant_id, role, default_view)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (identity_id, tenant_id, role, "dashboard"))

            conn.commit()

            return {
                "identity_id": identity_id, "display_name": display_name,
                "role": role, "metadata": metadata,
                "created_at": None, "is_new": True,
            }


# ─── User Preferences ─────────────────────────────────────────────────────────

def get_preferences(identity_id: str, dsn: Optional[str] = None) -> Optional[dict]:
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT identity_id, role, default_view, theme,
                       dashboard_profiles, active_dashboard_profile,
                       notification_preferences, learning_profile, timezone
                FROM user_preferences WHERE identity_id = %s
            """, (identity_id,))
            r = cur.fetchone()
            if not r:
                return None
            return {
                "identity_id": r[0], "role": r[1], "default_view": r[2],
                "theme": r[3], "dashboard_profiles": r[4],
                "active_dashboard_profile": r[5],
                "notification_preferences": r[6],
                "learning_profile": r[7], "timezone": r[8],
            }


def update_preferences(
    identity_id: str,
    default_view: Optional[str] = None,
    theme: Optional[str] = None,
    dashboard_profiles: Optional[list] = None,
    active_dashboard_profile: Optional[str] = None,
    timezone: Optional[str] = None,
    dsn: Optional[str] = None,
) -> bool:
    sets = ["updated_at = NOW()"]
    params: list = []

    if default_view:
        sets.append("default_view = %s")
        params.append(default_view)
    if theme:
        sets.append("theme = %s")
        params.append(theme)
    if dashboard_profiles is not None:
        sets.append("dashboard_profiles = %s")
        params.append(json.dumps(dashboard_profiles))
    if active_dashboard_profile:
        sets.append("active_dashboard_profile = %s")
        params.append(active_dashboard_profile)
    if timezone:
        sets.append("timezone = %s")
        params.append(timezone)

    params.append(identity_id)

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE user_preferences SET {', '.join(sets)} WHERE identity_id = %s", params)
            ok = cur.rowcount > 0
            conn.commit()
            return ok


# ─── Unified Profile ──────────────────────────────────────────────────────────

def get_full_profile(
    identity_id: str,
    tenant_id: str,
    dsn: Optional[str] = None,
) -> Optional[dict]:
    """
    Get the complete unified profile:
    identity + preferences + projects + signal stats + devices.

    This is THE endpoint that answers: "Who am I and what's mine?"
    """
    identity = get_identity(identity_id, tenant_id, dsn)
    if not identity:
        return None

    preferences = get_preferences(identity_id, dsn)

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Projects owned
            cur.execute("""
                SELECT project_id, name, status FROM projects
                WHERE identity_id = %s AND tenant_id = %s ORDER BY updated_at DESC
            """, (identity_id, tenant_id))
            projects = [{"project_id": r[0], "name": r[1], "status": r[2]} for r in cur.fetchall()]

            # Signal count
            cur.execute("""
                SELECT COUNT(*) FROM signals WHERE identity_id = %s AND tenant_id = %s
            """, (identity_id, tenant_id))
            signal_count = cur.fetchone()[0]

            # Unseen count
            cur.execute("""
                SELECT COUNT(*) FROM signals s
                WHERE s.tenant_id = %s
                  AND NOT EXISTS (SELECT 1 FROM signal_views sv WHERE sv.signal_rid = s.rid AND sv.identity_id = %s)
            """, (tenant_id, identity_id))
            unseen_count = cur.fetchone()[0]

            # Collections
            cur.execute("""
                SELECT collection_id, name FROM collections
                WHERE identity_id = %s ORDER BY updated_at DESC LIMIT 10
            """, (identity_id,))
            collections = [{"collection_id": r[0], "name": r[1]} for r in cur.fetchall()]

            # Cases owned
            cur.execute("""
                SELECT case_id, title, status FROM cases
                WHERE owner = %s AND tenant_id = %s AND status IN ('open', 'investigating')
                ORDER BY updated_at DESC LIMIT 10
            """, (identity_id, tenant_id))
            active_cases = [{"case_id": r[0], "title": r[1], "status": r[2]} for r in cur.fetchall()]

    return {
        "identity": identity,
        "preferences": preferences,
        "projects": projects,
        "collections": collections,
        "active_cases": active_cases,
        "stats": {
            "signal_count": signal_count,
            "unseen_count": unseen_count,
            "project_count": len(projects),
            "collection_count": len(collections),
            "active_case_count": len(active_cases),
        },
    }
