"""
Email sender — Resend.com transactional API.

Env (read at call time, not import time, so the hub can hot-reload them):
  RESEND_API_KEY      — full-access API key from resend.com/api-keys
  RESEND_FROM         — verified sender address, e.g. "Enki Systems <noreply@example.com>"
                        Defaults to "Enki <noreply@example.com>".

The Resend HTTP API is plain JSON; we use `requests` rather than the SDK to
keep the dep footprint small (requests is already in requirements.txt).

All sends are best-effort: failure is logged and swallowed so the calling
endpoint (preregister, approve, reject) still succeeds. The DB row's
*_email_sent_at column is only set when delivery to Resend succeeded.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"
DEFAULT_FROM = "Enki <noreply@example.com>"
DEFAULT_PUBLIC_URL = "https://example.com"


def _config() -> tuple[Optional[str], str, str]:
    api_key = os.getenv("RESEND_API_KEY", "").strip() or None
    from_addr = os.getenv("RESEND_FROM", "").strip() or DEFAULT_FROM
    public_url = os.getenv("ENKI_PUBLIC_URL", "").strip().rstrip("/") or DEFAULT_PUBLIC_URL
    return api_key, from_addr, public_url


def is_enabled() -> bool:
    """True iff RESEND_API_KEY is set. Used by callers to skip email entirely
    in dev / when the operator hasn't configured a provider."""
    return bool(_config()[0])


def send(to: str, subject: str, html: str, text: Optional[str] = None) -> bool:
    """Send one transactional email via Resend.

    Returns True on 2xx, False otherwise. Never raises — calling code must
    treat email as best-effort, not load-bearing.
    """
    api_key, from_addr, _ = _config()
    if not api_key:
        logger.info("email skip (RESEND_API_KEY unset): to=%s subject=%r", to, subject)
        return False

    payload = {
        "from": from_addr,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text

    try:
        r = requests.post(
            RESEND_URL,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
    except requests.RequestException as e:
        logger.warning("email send failed (network): to=%s err=%s", to, e)
        return False

    if r.status_code >= 300:
        logger.warning("email send failed: to=%s status=%s body=%s",
                       to, r.status_code, r.text[:300])
        return False
    return True


# ─── Template helpers ───────────────────────────────────────────────────────
# Inline HTML, no template engine — these are 5 emails total and copy is
# locked in. If a sixth lands, factor out at that point, not before.

def _shell(body_html: str) -> str:
    """Wrap a body in the standard Enki email shell. Inline CSS only —
    Gmail / Outlook strip <style> blocks."""
    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#0a0a0a;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#e4e4e7;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0a0a0a;padding:32px 16px;">
    <tr><td align="center">
      <table width="540" cellpadding="0" cellspacing="0" style="max-width:540px;background:#111;border:1px solid #222;border-radius:12px;">
        <tr><td style="padding:32px;">
          <div style="font-size:20px;font-weight:600;color:#fff;margin-bottom:24px;">Enki Systems</div>
          {body_html}
          <hr style="border:none;border-top:1px solid #222;margin:32px 0 16px;">
          <div style="font-size:11px;color:#666;line-height:1.5;">
            You're receiving this because you submitted a request at <a href="https://example.com" style="color:#888;">example.com</a>.
            Reply to this email if anything looks wrong.
          </div>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


def send_confirmation(to: str, status_token: str) -> bool:
    """Sent on POST /preregister — "you're on the list" + status URL."""
    _, _, public_url = _config()
    status_url = f"{public_url}/status/{status_token}"
    body = f"""
      <h1 style="font-size:18px;color:#fff;margin:0 0 16px;">You're on the list</h1>
      <p style="font-size:14px;line-height:1.6;margin:0 0 20px;">
        Thanks for requesting an Enki node. Your request is in the review queue.
      </p>
      <p style="font-size:14px;line-height:1.6;margin:0 0 24px;">
        Bookmark your personal status page — you'll see your install command
        appear here automatically when approved:
      </p>
      <p style="margin:0 0 24px;">
        <a href="{status_url}"
           style="display:inline-block;padding:12px 20px;background:#0a84ff;color:#fff;text-decoration:none;border-radius:8px;font-weight:500;">
          Open status page
        </a>
      </p>
      <p style="font-size:12px;color:#888;line-height:1.5;margin:0;">
        Or paste this URL directly:<br>
        <span style="font-family:monospace;color:#aaa;word-break:break-all;">{status_url}</span>
      </p>
    """
    text = (
        f"You're on the list.\n\n"
        f"Thanks for requesting an Enki node. Your request is in the review queue.\n\n"
        f"Your personal status page (bookmark this — your install command will appear here when approved):\n\n"
        f"  {status_url}\n\n"
        f"— Enki Systems\n"
    )
    return send(to, "You're on the Enki waitlist", _shell(body), text)


def send_approval(to: str, status_token: str, install_command: str, expires_iso: str) -> bool:
    """Sent on POST /hub/nodes/approve — install command + status URL."""
    _, _, public_url = _config()
    status_url = f"{public_url}/status/{status_token}"
    expires_short = expires_iso.split("T")[0] if "T" in expires_iso else expires_iso
    body = f"""
      <h1 style="font-size:18px;color:#fff;margin:0 0 16px;">Your Enki node is approved</h1>
      <p style="font-size:14px;line-height:1.6;margin:0 0 20px;">
        Open your install guide — we built a walk-through with a live assistant
        that answers install questions (Linux, WSL on Windows, cloud VPS, common
        errors) and waits for your node to come online.
      </p>
      <p style="margin:0 0 24px;">
        <a href="{status_url}"
           style="display:inline-block;padding:12px 22px;background:#0a84ff;color:#fff;text-decoration:none;border-radius:8px;font-weight:500;font-size:14px;">
          Open install guide
        </a>
      </p>
      <p style="font-size:12px;color:#888;line-height:1.5;margin:0 0 8px;">
        Prefer to skip the guide? Run this on your server directly (Ubuntu 22.04+):
      </p>
      <pre style="background:#000;border:1px solid #222;border-radius:8px;padding:16px;font-size:12px;color:#9aff9a;overflow-x:auto;white-space:pre-wrap;word-break:break-all;margin:0 0 12px;">{install_command}</pre>
      <p style="font-size:12px;color:#888;margin:0;">
        Token expires {expires_short}. One-time use. Tied to your account.
      </p>
    """
    text = (
        f"Your Enki node is approved.\n\n"
        f"Open your install guide — it has a walk-through and a live assistant\n"
        f"for install questions, plus waits for your node to come online:\n\n"
        f"  {status_url}\n\n"
        f"Or run this directly (Ubuntu 22.04+):\n\n"
        f"  {install_command}\n\n"
        f"Token expires {expires_short}. One-time use.\n\n"
        f"— Enki Systems\n"
    )
    return send(to, "Your Enki node is approved — open your install guide", _shell(body), text)


def send_rejection(to: str, status_token: str, reason: Optional[str]) -> bool:
    """Sent on POST /preregistrations/{id}/reject — polite decline."""
    _, _, public_url = _config()
    status_url = f"{public_url}/status/{status_token}"
    reason_block = (
        f"<p style='font-size:13px;color:#aaa;margin:0 0 20px;'><em>Note from the reviewer:</em><br>{reason}</p>"
        if reason else ""
    )
    body = f"""
      <h1 style="font-size:18px;color:#fff;margin:0 0 16px;">About your Enki request</h1>
      <p style="font-size:14px;line-height:1.6;margin:0 0 16px;">
        We're not able to approve your request for the current invitation round.
        This isn't permanent — feel free to resubmit with more context about
        your use case at any time.
      </p>
      {reason_block}
      <p style="font-size:12px;color:#888;margin:0;">
        Status page: <a href="{status_url}" style="color:#888;">{status_url}</a>
      </p>
    """
    text = (
        f"About your Enki request.\n\n"
        f"We're not able to approve your request for the current invitation round. "
        f"This isn't permanent — feel free to resubmit with more context at any time.\n\n"
        + (f"Note from the reviewer: {reason}\n\n" if reason else "")
        + f"— Enki Systems\n"
    )
    return send(to, "About your Enki request", _shell(body), text)
