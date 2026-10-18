"""
Web Push (VAPID) sender — wraps pywebpush.

Env:
  VAPID_PRIVATE_KEY    — base64url-encoded P-256 private key (DER)
  VAPID_PUBLIC_KEY     — base64url-encoded P-256 public key (raw, 65 bytes uncompressed)
  VAPID_SUBJECT        — mailto: or https: URI of admin contact (RFC 8292)
                         default: mailto:admin@example.com

Generate keys with scripts/gen_vapid_keys.py (one-time, then put in
/etc/enki/hub.conf).

All sends are best-effort. Dead subscriptions (410 Gone) are deleted from
the DB so they don't pile up.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_SUBJECT = "mailto:admin@example.com"


def _config() -> tuple[Optional[str], Optional[str], str]:
    priv = os.getenv("VAPID_PRIVATE_KEY", "").strip() or None
    pub = os.getenv("VAPID_PUBLIC_KEY", "").strip() or None
    subj = os.getenv("VAPID_SUBJECT", "").strip() or DEFAULT_SUBJECT
    return priv, pub, subj


def is_enabled() -> bool:
    """True iff VAPID keys are configured. Used by the subscribe endpoint
    to refuse subscriptions when push isn't actually set up."""
    priv, pub, _ = _config()
    return bool(priv and pub)


def public_key() -> Optional[str]:
    """The public key the browser needs to subscribe."""
    return _config()[1]


def _send_one(subscription: dict, payload: str) -> tuple[bool, Optional[int], Optional[str]]:
    """Returns (ok, status_code, error_msg)."""
    priv, _, subj = _config()
    if not priv:
        return (False, None, "VAPID_PRIVATE_KEY not configured")

    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        logger.warning("pywebpush not installed; skipping push send")
        return (False, None, "pywebpush not installed")

    try:
        webpush(
            subscription_info=subscription,
            data=payload,
            vapid_private_key=priv,
            vapid_claims={"sub": subj},
            ttl=86400,
        )
        return (True, 201, None)
    except WebPushException as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        return (False, code, str(e)[:300])
    except Exception as e:
        return (False, None, str(e)[:300])


def notify_approval(preregistration_id: int) -> bool:
    """Fan out a push to every subscription registered against this prereg.

    Returns True if at least one subscription was delivered. Removes dead
    subscriptions (410 Gone, 404 Not Found) from the DB as a side effect.
    """
    if not is_enabled():
        return False

    # Lazy import so the route module doesn't pay this on import.
    from storage.postgres.db import get_connection

    payload = json.dumps({
        "type": "approval",
        "title": "Your Enki node is approved",
        "body": "Tap to view your install command.",
        "url": None,  # filled per-row below
    })

    delivered = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT s.id, s.endpoint, s.p256dh, s.auth, p.status_token
                     FROM push_subscriptions s
                     JOIN preregistrations p ON p.id = s.preregistration_id
                    WHERE s.preregistration_id = %s""",
                (preregistration_id,),
            )
            rows = cur.fetchall()

        for sub_id, endpoint, p256dh, auth, status_token in rows:
            per_payload = json.dumps({
                "type": "approval",
                "title": "Your Enki node is approved",
                "body": "Tap to view your install command.",
                "url": f"/status/{status_token}",
            })
            ok, status, err = _send_one(
                {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}},
                per_payload,
            )
            with conn.cursor() as cur:
                if ok:
                    cur.execute(
                        "UPDATE push_subscriptions SET last_used_at = NOW(), last_error = NULL WHERE id = %s",
                        (sub_id,),
                    )
                    delivered += 1
                elif status in (404, 410):
                    cur.execute("DELETE FROM push_subscriptions WHERE id = %s", (sub_id,))
                    logger.info("removed dead push subscription %s (status %s)", sub_id, status)
                else:
                    cur.execute(
                        "UPDATE push_subscriptions SET last_error = %s WHERE id = %s",
                        (err, sub_id),
                    )
                    logger.warning("push send failed for sub %s: %s", sub_id, err)
        conn.commit()

    return delivered > 0
