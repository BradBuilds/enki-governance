"""
Invite code management.

Codes gate access to the platform. No valid code = no signup.

Flow:
  1. Admin generates code:  generate_invite_code("ENKI-XXXX-XXXX", tier="free")
  2. Admin sends code to customer
  3. Customer enters code on signup page
  4. validate_invite_code() checks it's valid
  5. redeem_invite_code() marks it used + creates tenant + identity
"""

from __future__ import annotations

import secrets
import uuid
import json
from typing import Optional
from datetime import datetime, timezone

from storage.postgres.db import get_connection


def _generate_code() -> str:
    """Generate a human-readable invite code: ENKI-XXXX-XXXX."""
    chars = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # no 0/O/1/I confusion
    part1 = "".join(secrets.choice(chars) for _ in range(4))
    part2 = "".join(secrets.choice(chars) for _ in range(4))
    return f"ENKI-{part1}-{part2}"


def generate_invite_code(
    tier: str = "free",
    max_users: int = 5,
    email: Optional[str] = None,
    note: Optional[str] = None,
    created_by: str = "admin",
    expires_in_days: Optional[int] = None,
    dsn: Optional[str] = None,
) -> dict:
    """Generate a new invite code. Returns the code details."""
    code = _generate_code()

    expires_at = None
    if expires_in_days:
        from datetime import timedelta
        expires_at = datetime.now(timezone.utc) + timedelta(days=expires_in_days)

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO invite_codes (code, email, tier, max_users, created_by, note, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING code, created_at
            """, (code, email, tier, max_users, created_by, note, expires_at))
            row = cur.fetchone()
            conn.commit()

    return {
        "code": row[0],
        "tier": tier,
        "max_users": max_users,
        "email": email,
        "note": note,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "created_at": row[1].isoformat(),
    }


def validate_invite_code(
    code: str,
    email: Optional[str] = None,
    dsn: Optional[str] = None,
) -> dict:
    """
    Check if an invite code is valid.
    Returns {"valid": True/False, "reason": "...", ...}
    """
    code = code.strip().upper()

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT code, email, tier, max_users, status, expires_at
                FROM invite_codes WHERE code = %s
            """, (code,))
            row = cur.fetchone()

    if not row:
        return {"valid": False, "reason": "INVALID_CODE"}

    db_code, db_email, tier, max_users, status, expires_at = row

    if status == "redeemed":
        return {"valid": False, "reason": "ALREADY_REDEEMED"}
    if status == "revoked":
        return {"valid": False, "reason": "CODE_REVOKED"}
    if status == "expired":
        return {"valid": False, "reason": "CODE_EXPIRED"}
    if expires_at and datetime.now(timezone.utc) > expires_at:
        return {"valid": False, "reason": "CODE_EXPIRED"}
    if db_email and email and db_email.lower() != email.lower():
        return {"valid": False, "reason": "EMAIL_MISMATCH"}

    return {
        "valid": True,
        "code": db_code,
        "tier": tier,
        "max_users": max_users,
        "email": db_email,
    }


def redeem_invite_code(
    code: str,
    logto_sub: str,
    display_name: str,
    email: Optional[str] = None,
    dsn: Optional[str] = None,
) -> dict:
    """
    Redeem an invite code. Creates tenant + identity.
    Returns the new tenant_id and identity.
    """
    code = code.strip().upper()

    # Validate first
    check = validate_invite_code(code, email, dsn)
    if not check["valid"]:
        return {"error": check["reason"]}

    tenant_id = f"tenant_{uuid.uuid4().hex[:12]}"
    identity_id = f"person_{uuid.uuid4().hex[:12]}"
    tier = check["tier"]
    max_users = check["max_users"]

    metadata = {"email": email} if email else {}
    metadata["tier"] = tier
    metadata["max_users"] = max_users
    metadata["invite_code"] = code

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Create tenant
            try:
                from storage.node.identity import load_node_id
                node_id = load_node_id()
            except Exception:
                node_id = "local"

            cur.execute("""
                INSERT INTO tenants (tenant_id, node_id, display_name, metadata)
                VALUES (%s, %s, %s, %s)
            """, (tenant_id, node_id, display_name + "'s Node", json.dumps({"tier": tier, "max_users": max_users})))

            # Create identity (admin — they're the first user on their tenant)
            cur.execute("""
                INSERT INTO identities (identity_id, tenant_id, display_name, role, logto_sub, metadata)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (identity_id, tenant_id, display_name, "admin", logto_sub, json.dumps(metadata)))

            # Create default preferences
            cur.execute("""
                INSERT INTO user_preferences (identity_id, tenant_id, role, default_view)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (identity_id, tenant_id, "admin", "dashboard"))

            # Mark code as redeemed
            cur.execute("""
                UPDATE invite_codes
                SET status = 'redeemed', redeemed_by = %s, tenant_id = %s, redeemed_at = now()
                WHERE code = %s
            """, (logto_sub, tenant_id, code))

            conn.commit()

    return {
        "tenant_id": tenant_id,
        "identity_id": identity_id,
        "display_name": display_name,
        "role": "admin",
        "tier": tier,
    }


def list_invite_codes(
    status_filter: Optional[str] = None,
    dsn: Optional[str] = None,
) -> list[dict]:
    """List all invite codes (admin view)."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            if status_filter:
                cur.execute("""
                    SELECT code, email, tier, max_users, status, note, tenant_id, redeemed_by,
                           created_at, redeemed_at, expires_at
                    FROM invite_codes WHERE status = %s ORDER BY created_at DESC
                """, (status_filter,))
            else:
                cur.execute("""
                    SELECT code, email, tier, max_users, status, note, tenant_id, redeemed_by,
                           created_at, redeemed_at, expires_at
                    FROM invite_codes ORDER BY created_at DESC
                """)
            return [{
                "code": r[0], "email": r[1], "tier": r[2], "max_users": r[3],
                "status": r[4], "note": r[5], "tenant_id": r[6], "redeemed_by": r[7],
                "created_at": r[8].isoformat() if r[8] else None,
                "redeemed_at": r[9].isoformat() if r[9] else None,
                "expires_at": r[10].isoformat() if r[10] else None,
            } for r in cur.fetchall()]


def revoke_invite_code(code: str, dsn: Optional[str] = None) -> bool:
    """Revoke an unused invite code."""
    code = code.strip().upper()
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE invite_codes SET status = 'revoked'
                WHERE code = %s AND status = 'pending'
            """, (code,))
            ok = cur.rowcount > 0
            conn.commit()
            return ok
