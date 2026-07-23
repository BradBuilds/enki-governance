"""
RSS / Atom connector handler.

Polls a feed URL and yields each NEW entry as a dict ready for
ingest_document. Uses stdlib xml.etree to avoid a feedparser dep —
handles the common subset of RSS 2.0 + Atom 1.0 we care about.

Config keys:
    url           (required)       feed URL
    user_agent    (optional)       HTTP User-Agent override
    body_strategy (optional)       'summary' | 'content' | 'fetch_link'
                                   default 'summary' — use the description
                                   field. 'fetch_link' GETs the entry's
                                   link and uses the response body.
    body_max_bytes (optional)      Cap fetched bodies (default 5 MB)
"""

from __future__ import annotations

import hashlib
import logging
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Iterator, Optional

log = logging.getLogger("enki.connectors.rss")

DEFAULT_UA = "ENKI-rss-connector/1.0 (https://example.com)"
DEFAULT_BODY_MAX = 5 * 1024 * 1024


# ── XML namespace helpers (RSS doesn't standardize them) ───────────────────

ATOM_NS = "http://www.w3.org/2005/Atom"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
DC_NS = "http://purl.org/dc/elements/1.1/"


def _localname(tag: str) -> str:
    """Strip namespace from a tag name."""
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _text(el: Optional[ET.Element]) -> Optional[str]:
    if el is None:
        return None
    t = "".join(el.itertext()).strip()
    return t or None


def _find_child(parent: ET.Element, *names: str) -> Optional[ET.Element]:
    """Find first child whose localname is in `names`."""
    for child in parent:
        if _localname(child.tag).lower() in names:
            return child
    return None


def _http_get(url: str, *, user_agent: str, max_bytes: int) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": user_agent,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read(max_bytes)


# ── Date parsing (RFC 822 + ISO 8601 + best-effort) ────────────────────────

def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    s = s.strip()
    # Try ISO 8601 first
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    # RFC 822 / 1123
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# ── Main fetch ─────────────────────────────────────────────────────────────

def fetch_recent(
    config: dict,
    *,
    since: Optional[datetime] = None,
) -> Iterator[dict]:
    """Yield {body, source_url, source_title, mime_type, item_id,
    published_at} for each entry in the feed newer than `since`."""
    url = config.get("url")
    if not url:
        log.warning("rss connector missing url")
        return

    ua = config.get("user_agent") or DEFAULT_UA
    max_bytes = int(config.get("body_max_bytes") or DEFAULT_BODY_MAX)
    body_strategy = (config.get("body_strategy") or "summary").lower()

    try:
        raw = _http_get(url, user_agent=ua, max_bytes=max_bytes)
    except Exception as e:
        log.warning("rss fetch failed for %s: %s", url, e)
        return

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        log.warning("rss parse failed for %s: %s", url, e)
        return

    # Atom: <feed><entry>...</entry></feed>
    # RSS:  <rss><channel><item>...</item></channel></rss>
    is_atom = _localname(root.tag).lower() == "feed"

    items = []
    if is_atom:
        items = [e for e in root if _localname(e.tag).lower() == "entry"]
    else:
        channel = _find_child(root, "channel")
        if channel is not None:
            items = [e for e in channel if _localname(e.tag).lower() == "item"]

    for item in items:
        try:
            yield from _yield_one(item, is_atom, ua, max_bytes, body_strategy, since)
        except Exception as e:
            log.debug("rss entry skipped: %s", e)
            continue


def _yield_one(
    item: ET.Element, is_atom: bool, ua: str, max_bytes: int,
    body_strategy: str, since: Optional[datetime],
) -> Iterator[dict]:
    # Title
    title = _text(_find_child(item, "title")) or "(untitled)"

    # Link / source_url
    link = None
    if is_atom:
        for child in item:
            if _localname(child.tag).lower() == "link":
                href = child.attrib.get("href")
                if href:
                    link = href
                    break
    else:
        link = _text(_find_child(item, "link"))

    if not link:
        return

    # Item GUID / id — used for dedup
    item_id = (
        _text(_find_child(item, "guid", "id"))
        or link
    )

    # Published time
    published_raw = (
        _text(_find_child(item, "pubdate", "published", "updated", "date"))
        or _text(_find_child(item, "{%s}date" % DC_NS))
    )
    published_at = _parse_dt(published_raw)

    if since and published_at and published_at <= since:
        return

    # Body
    body_text = None
    mime_type = "text/html"

    if body_strategy == "fetch_link":
        try:
            raw = _http_get(link, user_agent=ua, max_bytes=max_bytes)
            body_text = raw.decode("utf-8", errors="replace")
        except Exception as e:
            log.debug("rss fetch_link failed for %s: %s", link, e)
            # fall back to summary
    if body_text is None:
        # RSS: <description> or content:encoded
        body_el = (
            _find_child(item, "description", "summary", "subtitle")
            or item.find("{%s}encoded" % CONTENT_NS)
            or _find_child(item, "content")
        )
        body_text = _text(body_el) or title
        mime_type = "text/html" if "<" in body_text else "text/plain"

    if not body_text:
        return

    yield {
        "body": body_text.encode("utf-8"),
        "source_url": link,
        "source_title": title[:500],
        "mime_type": mime_type,
        "item_id": item_id,
        "published_at": published_at.isoformat() if published_at else None,
    }
