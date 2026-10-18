"""
Email parser — handles RFC 5322 email messages.

Extracts headers, body text, and attachment metadata.
Observables: email addresses, domain names from addresses.
"""

import email
import email.policy
from typing import Optional

from storage.ingest.parsers import ParseResult, register_parser
from storage.ingest.parsers.base import BaseParser
from storage.signals.models import Observable


@register_parser
class EmailParser(BaseParser):
    name = "email"
    format_id = "email/rfc5322"

    def can_parse(self, raw_bytes: bytes, content_type: Optional[str] = None) -> float:
        if content_type and "message/rfc822" in content_type.lower():
            return 0.95

        try:
            text = raw_bytes[:2000].decode("utf-8", errors="ignore")
        except Exception:
            return 0.0

        # Check for RFC 5322 headers
        header_patterns = ["From:", "To:", "Subject:", "Date:", "Received:", "MIME-Version:"]
        matches = sum(1 for p in header_patterns if p in text)

        if matches >= 3:
            return 0.90
        if matches >= 2:
            return 0.70
        return 0.0

    def parse(self, raw_bytes: bytes) -> ParseResult:
        msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)

        from_addr = msg.get("From", "")
        to_addrs = msg.get("To", "")
        cc_addrs = msg.get("Cc", "")
        subject = msg.get("Subject", "")
        date = msg.get("Date", "")
        message_id = msg.get("Message-ID", "")

        # Extract body text
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        body = payload.decode("utf-8", errors="replace")
                        break
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                body = payload.decode("utf-8", errors="replace")

        # Extract attachments metadata (NOT the file content)
        attachments = []
        if msg.is_multipart():
            for part in msg.walk():
                filename = part.get_filename()
                if filename:
                    attachments.append({
                        "filename": filename,
                        "content_type": part.get_content_type(),
                        "size": len(part.get_payload(decode=True) or b""),
                    })

        parsed = {
            "from": from_addr,
            "to": _parse_address_list(to_addrs),
            "cc": _parse_address_list(cc_addrs),
            "subject": subject,
            "date": date,
            "message_id": message_id,
            "body_preview": body[:1000] if body else "",
            "body_length": len(body),
            "attachment_count": len(attachments),
            "attachments": attachments,
        }

        # Extract observables
        observables = []
        all_addresses = _extract_email_addresses(from_addr) + _extract_email_addresses(to_addrs)
        all_addresses += _extract_email_addresses(cc_addrs)

        seen = set()
        for addr, field_name in all_addresses:
            norm = addr.lower()
            if norm not in seen:
                seen.add(norm)
                observables.append(Observable(
                    observable_type="email_address",
                    observable_value=addr,
                    normalized_value=norm,
                    source_field=field_name,
                ))
                # Also extract domain
                domain = norm.split("@")[-1] if "@" in norm else None
                if domain and domain not in seen:
                    seen.add(domain)
                    observables.append(Observable(
                        observable_type="domain_name",
                        observable_value=domain,
                        normalized_value=domain,
                        source_field=field_name,
                    ))

        return ParseResult(
            format_detected="email/rfc5322",
            confidence=0.90,
            parsed=parsed,
            metadata={"has_attachments": len(attachments) > 0},
            observables=observables,
            signal_type_hint="signal.communications.email",
            parser_name=self.name,
        )


def _parse_address_list(header_value: str) -> list[str]:
    """Parse comma-separated email addresses from header."""
    if not header_value:
        return []
    return [addr.strip() for addr in header_value.split(",") if addr.strip()]


def _extract_email_addresses(header_value: str) -> list[tuple[str, str]]:
    """Extract (email, field_name) tuples from header value."""
    import re
    results = []
    if not header_value:
        return results
    # Find email addresses in "Name <email>" or bare "email" format
    pattern = re.compile(r'[\w.+-]+@[\w.-]+\.\w+')
    for match in pattern.finditer(header_value):
        results.append((match.group(), "from" if "From" in header_value[:10] else "to"))
    return results
