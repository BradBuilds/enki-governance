"""
Preregistration / early access waitlist endpoints (hub-only).

Public:
  POST /preregister              — submit form, returns status_url
  GET  /status/{status_token}    — poll own status, install command appears here on approval

Hub-admin:
  GET  /preregistrations         — full waitlist
  POST /preregistrations/{id}/reject  — mark rejected
  (approval lives in routes/hub.py — POST /hub/nodes/approve)
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from storage.postgres.db import get_connection
from services.api.src.routes.hub import require_hub_admin
from services.api.src.lib import email as email_sender

logger = logging.getLogger(__name__)

router = APIRouter(tags=["preregister"])


# ─── Models ─────────────────────────────────────────────────────────────────

class PreregisterRequest(BaseModel):
    email: str
    name: Optional[str] = None
    organization: Optional[str] = None
    country: Optional[str] = None         # "CA", "US", or ISO 3166-1 alpha-2
    use_case: Optional[str] = None
    message: Optional[str] = None


class RejectRequest(BaseModel):
    reason: Optional[str] = None


# ─── Public endpoints ───────────────────────────────────────────────────────

@router.post("/preregister", status_code=201)
def preregister(body: PreregisterRequest):
    """Public — accept early access signup, return status_url for the registrant."""
    email = body.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Valid email required")

    country = (body.country or "").strip().upper() or None
    if country and len(country) != 2:
        raise HTTPException(status_code=400, detail="Country must be ISO 3166-1 alpha-2 (e.g. CA, US)")

    status_token = secrets.token_urlsafe(48)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO preregistrations
                        (email, name, organization, country, use_case, message, status_token)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (email) DO UPDATE SET
                        name         = COALESCE(EXCLUDED.name,         preregistrations.name),
                        organization = COALESCE(EXCLUDED.organization, preregistrations.organization),
                        country      = COALESCE(EXCLUDED.country,      preregistrations.country),
                        use_case     = COALESCE(EXCLUDED.use_case,     preregistrations.use_case),
                        message      = COALESCE(EXCLUDED.message,      preregistrations.message)
                    RETURNING id, status_token, created_at
                    """,
                    (email, body.name, body.organization, country, body.use_case, body.message, status_token),
                )
                row = cur.fetchone()
            conn.commit()
    except Exception:
        logger.exception("preregister insert failed")
        raise HTTPException(status_code=500, detail="Registration failed")

    prereg_id, returned_token, created_at = row

    if email_sender.send_confirmation(email, returned_token):
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE preregistrations SET confirmation_email_sent_at = NOW() WHERE id = %s",
                        (prereg_id,),
                    )
                conn.commit()
        except Exception:
            logger.exception("failed to record confirmation_email_sent_at for id=%s", prereg_id)

    return {
        "status": "registered",
        "email": email,
        "id": prereg_id,
        "status_token": returned_token,
        "status_url": f"/status/{returned_token}",
        "created_at": created_at.isoformat(),
    }


@router.get("/status/{status_token}")
def get_registration_status(status_token: str):
    """Public — registrant polls their own status URL.

    Returns pending / approved / rejected. If approved, includes the install
    command and its expiry. The token IS the bearer secret — anyone with the
    URL can see this row's install command, by design.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, email, name, country, status, created_at,
                       approved_at, rejected_at,
                       install_command, install_command_expires_at,
                       node_id
                FROM preregistrations
                WHERE status_token = %s
                """,
                (status_token,),
            )
            row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Unknown status token")

    (prereg_id, email, name, country, status, created_at,
     approved_at, rejected_at, install_cmd, install_expires, node_id) = row

    now = datetime.now(timezone.utc)
    expired = bool(install_expires and install_expires < now)

    return {
        "id": prereg_id,
        "email": email,
        "name": name,
        "country": country,
        "status": status,
        "created_at": created_at.isoformat(),
        "approved_at": approved_at.isoformat() if approved_at else None,
        "rejected_at": rejected_at.isoformat() if rejected_at else None,
        "node_id": node_id,
        "install_command": install_cmd if (install_cmd and not expired) else None,
        "install_command_expires_at": install_expires.isoformat() if install_expires else None,
        "install_command_expired": expired,
    }


# ─── Admin endpoints ────────────────────────────────────────────────────────

@router.get("/preregistrations")
def list_preregistrations(
    status: Optional[str] = None,
    country: Optional[str] = None,
    admin: dict = Depends(require_hub_admin),
):
    """Hub-admin — list waitlist. Optional ?status=pending&country=CA filters."""
    sql = """
        SELECT id, email, name, organization, country, use_case, message,
               status, created_at, approved_at, rejected_at,
               node_id, install_command_expires_at
        FROM preregistrations
        WHERE 1=1
    """
    params: list = []
    if status:
        sql += " AND status = %s"
        params.append(status)
    if country:
        sql += " AND country = %s"
        params.append(country.upper())
    sql += " ORDER BY created_at DESC"

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    return {
        "total": len(rows),
        "registrations": [
            {
                "id": r[0],
                "email": r[1],
                "name": r[2],
                "organization": r[3],
                "country": r[4],
                "use_case": r[5],
                "message": r[6],
                "status": r[7],
                "created_at": r[8].isoformat() if r[8] else None,
                "approved_at": r[9].isoformat() if r[9] else None,
                "rejected_at": r[10].isoformat() if r[10] else None,
                "node_id": r[11],
                "install_command_expires_at": r[12].isoformat() if r[12] else None,
            }
            for r in rows
        ],
    }


@router.post("/preregistrations/{prereg_id}/reject")
def reject_preregistration(
    prereg_id: int,
    body: RejectRequest,
    admin: dict = Depends(require_hub_admin),
):
    """Hub-admin — mark a preregistration rejected. Visible on the status URL."""
    now = datetime.now(timezone.utc)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE preregistrations
                   SET status = 'rejected', rejected_at = %s,
                       message = COALESCE(%s, message)
                 WHERE id = %s
                 RETURNING id, email, status_token
                """,
                (now, body.reason, prereg_id),
            )
            row = cur.fetchone()
        conn.commit()

    if not row:
        raise HTTPException(status_code=404, detail="Preregistration not found")

    pid, to_email, token = row
    if email_sender.send_rejection(to_email, token, body.reason):
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE preregistrations SET rejection_email_sent_at = NOW() WHERE id = %s",
                        (pid,),
                    )
                conn.commit()
        except Exception:
            logger.exception("failed to record rejection_email_sent_at for id=%s", pid)

    return {"id": pid, "email": to_email, "status": "rejected"}
