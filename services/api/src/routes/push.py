"""
Web Push subscription endpoints (hub-only).

Public:
  GET  /push/vapid-public-key    — browser fetches this to subscribe
  POST /push/subscribe           — browser posts its subscription tied to a status_token
  POST /push/unsubscribe         — browser revokes a subscription
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from storage.postgres.db import get_connection
from services.api.src.lib import push as push_sender

logger = logging.getLogger(__name__)

router = APIRouter(tags=["push"])


class SubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class SubscribeRequest(BaseModel):
    status_token: str           # ties this subscription to a specific preregistration
    endpoint: str
    keys: SubscriptionKeys


class UnsubscribeRequest(BaseModel):
    status_token: str
    endpoint: str


@router.get("/push/vapid-public-key")
def get_vapid_public_key():
    """Browser fetches the VAPID public key to pass to PushManager.subscribe."""
    pub = push_sender.public_key()
    if not pub:
        raise HTTPException(status_code=503, detail="Push not configured on this hub")
    return {"public_key": pub}


@router.post("/push/subscribe", status_code=201)
def subscribe(body: SubscribeRequest, request: Request):
    """Bind a browser push subscription to a preregistration row.

    Idempotent on (preregistration_id, endpoint) — re-subscribing with the
    same endpoint updates the keys instead of erroring.
    """
    if not push_sender.is_enabled():
        raise HTTPException(status_code=503, detail="Push not configured on this hub")

    user_agent = request.headers.get("user-agent", "")[:500]

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM preregistrations WHERE status_token = %s",
                (body.status_token,),
            )
            prereg = cur.fetchone()
            if not prereg:
                raise HTTPException(status_code=404, detail="Unknown status_token")
            prereg_id = prereg[0]

            cur.execute(
                """INSERT INTO push_subscriptions
                       (preregistration_id, endpoint, p256dh, auth, user_agent)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (preregistration_id, endpoint) DO UPDATE
                      SET p256dh = EXCLUDED.p256dh,
                          auth = EXCLUDED.auth,
                          user_agent = EXCLUDED.user_agent,
                          last_error = NULL
                   RETURNING id""",
                (prereg_id, body.endpoint, body.keys.p256dh, body.keys.auth, user_agent),
            )
            sub_id = cur.fetchone()[0]
        conn.commit()

    return {"id": sub_id, "status": "subscribed"}


@router.post("/push/unsubscribe")
def unsubscribe(body: UnsubscribeRequest):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """DELETE FROM push_subscriptions
                    USING preregistrations p
                    WHERE push_subscriptions.preregistration_id = p.id
                      AND p.status_token = %s
                      AND push_subscriptions.endpoint = %s
                 RETURNING push_subscriptions.id""",
                (body.status_token, body.endpoint),
            )
            row = cur.fetchone()
        conn.commit()

    return {"removed": row[0] if row else None}
