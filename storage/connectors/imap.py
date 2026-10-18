"""
IMAP email connector — pull emails from any IMAP server.

Fetches new emails since last watermark, converts each to a JSON
payload with from/to/subject/body/attachments, and feeds into
the ingestion pipeline.

source_config:
  {
    "host": "imap.gmail.com",
    "port": 993,
    "username": "user@gmail.com",
    "password": "app-password",
    "use_ssl": true,
    "folder": "INBOX",
    "search_criteria": "UNSEEN",
    "max_messages": 50
  }
"""

import email
import imaplib
import json
import base64
from datetime import datetime, timezone
from email.header import decode_header
from typing import Optional

from storage.connectors.base import BaseConnector
from storage.connectors.manager import register_connector_class


class ImapConnector(BaseConnector):
    connector_type = "pull"
    source_type = "imap"

    def fetch(self) -> list[dict]:
        config = self.source_config
        host = config.get("host")
        port = config.get("port", 993)
        username = config.get("username")
        password = config.get("password")
        use_ssl = config.get("use_ssl", True)
        folder = config.get("folder", "INBOX")
        search_criteria = config.get("search_criteria", "UNSEEN")
        max_messages = config.get("max_messages", 50)

        if not host or not username or not password:
            raise ValueError("IMAP connector requires host, username, and password")

        # Connect
        if use_ssl:
            conn = imaplib.IMAP4_SSL(host, port)
        else:
            conn = imaplib.IMAP4(host, port)

        try:
            conn.login(username, password)
            conn.select(folder, readonly=True)

            # Search for messages
            # If we have a watermark UID, search since that UID
            last_uid = self.watermark.get("last_uid")
            if last_uid:
                criteria = f"UID {int(last_uid) + 1}:*"
                status, data = conn.uid("search", None, criteria)
            else:
                status, data = conn.search(None, search_criteria)

            if status != "OK" or not data[0]:
                return []

            msg_ids = data[0].split()[:max_messages]
            items = []

            for msg_id in msg_ids:
                try:
                    if last_uid:
                        status, msg_data = conn.uid("fetch", msg_id, "(RFC822)")
                    else:
                        status, msg_data = conn.fetch(msg_id, "(RFC822)")

                    if status != "OK" or not msg_data[0]:
                        continue

                    raw_email = msg_data[0][1]
                    parsed = _parse_email(raw_email)
                    parsed["_imap_uid"] = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)

                    raw = json.dumps(parsed, ensure_ascii=False, default=str).encode("utf-8")
                    items.append({
                        "raw_bytes": raw,
                        "content_type": "message/rfc822",
                        "source_type": "imap",
                    })
                except Exception:
                    continue

            return items

        finally:
            try:
                conn.close()
                conn.logout()
            except Exception:
                pass

    def update_watermark(self, items: list[dict]) -> dict:
        if not items:
            return self.watermark

        # Track the last UID we saw
        last_uid = None
        for item in items:
            try:
                parsed = json.loads(item["raw_bytes"])
                uid = parsed.get("_imap_uid")
                if uid:
                    last_uid = uid
            except (json.JSONDecodeError, KeyError):
                pass

        return {
            "last_uid": last_uid or self.watermark.get("last_uid"),
            "last_fetch_at": datetime.now(timezone.utc).isoformat(),
            "items_fetched": len(items),
        }


def _parse_email(raw_bytes: bytes) -> dict:
    """Parse raw RFC822 email bytes into a structured dict."""
    msg = email.message_from_bytes(raw_bytes)

    # Decode headers
    from_addr = _decode_header(msg.get("From", ""))
    to_addr = _decode_header(msg.get("To", ""))
    subject = _decode_header(msg.get("Subject", ""))
    date_str = msg.get("Date", "")
    message_id = msg.get("Message-ID", "")
    cc = _decode_header(msg.get("Cc", ""))
    reply_to = _decode_header(msg.get("Reply-To", ""))

    # Extract body
    body_text = ""
    body_html = ""
    attachments = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))

            if "attachment" in content_disposition:
                filename = part.get_filename()
                if filename:
                    payload = part.get_payload(decode=True)
                    attachments.append({
                        "filename": _decode_header(filename),
                        "content_type": content_type,
                        "size": len(payload) if payload else 0,
                    })
            elif content_type == "text/plain" and not body_text:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body_text = payload.decode(charset, errors="replace")
            elif content_type == "text/html" and not body_html:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body_html = payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            body_text = payload.decode(charset, errors="replace")

    # Truncate body for signal payload (full body in LDU-T)
    body_preview = (body_text or body_html or "")[:2000]

    return {
        "from": from_addr,
        "to": to_addr,
        "cc": cc,
        "reply_to": reply_to,
        "subject": subject,
        "date": date_str,
        "message_id": message_id,
        "body_preview": body_preview,
        "body_text_length": len(body_text),
        "body_html_length": len(body_html),
        "attachment_count": len(attachments),
        "attachments": attachments,
        "has_attachments": len(attachments) > 0,
    }


def _decode_header(value: str) -> str:
    """Decode RFC2047 encoded header value."""
    if not value:
        return ""
    try:
        parts = decode_header(value)
        decoded = []
        for part, charset in parts:
            if isinstance(part, bytes):
                decoded.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                decoded.append(part)
        return " ".join(decoded)
    except Exception:
        return str(value)


# Register
register_connector_class("imap", ImapConnector)
