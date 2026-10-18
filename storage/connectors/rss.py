"""
RSS/Atom feed connector.

Pulls articles from RSS/Atom feeds and ingests them as signals.
Used for: news feeds, research feeds, daily image feeds, threat intel,
          any subscription-based content.

Signal types produced:
  signal.content.rss_entry     (articles, posts)
  signal.content.news_article  (if feed is news-classified)
  signal.interest.article      (if feed is user-subscribed interest)
"""

import json
import hashlib
import re
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

from storage.connectors.base import BaseConnector

log = logging.getLogger("enki.rss")

# Media RSS namespace
MEDIA_NS = "http://search.yahoo.com/mrss/"


class RssConnector(BaseConnector):
    connector_type = "pull"
    source_type = "rss_feed"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # RSS connectors always produce signal.content.rss_entry. The base
        # connector reads signal_type_hint from source_config; if a particular
        # RSS connector was created with a different hint (e.g. news_article)
        # we honor that, but otherwise we default-stamp rss_entry so the
        # ingestion pipeline doesn't route to LDU-P playground for unknown
        # JSON. The article worker registers for both rss_entry and
        # news_article so either id resolves correctly downstream.
        if isinstance(self.source_config, dict) and not self.source_config.get("signal_type_hint"):
            self.source_config["signal_type_hint"] = "signal.content.rss_entry"

    def fetch(self) -> list[dict]:
        url = self.source_config.get("url")
        if not url:
            raise ValueError("RSS connector requires 'url' in source_config")

        # Fetch feed
        req = Request(url, headers={"User-Agent": "Enki/1.0 (RSS Connector)"})
        try:
            with urlopen(req, timeout=30) as resp:
                feed_bytes = resp.read()
        except URLError as e:
            raise ConnectionError(f"Failed to fetch {url}: {e}")

        # Parse feed
        root = ET.fromstring(feed_bytes)
        entries = _parse_feed(root)

        # Filter by watermark (only new entries)
        last_seen = self.watermark.get("last_entry_id")
        if last_seen:
            new_entries = []
            found_watermark = False
            for entry in entries:
                if entry["id"] == last_seen:
                    found_watermark = True
                    continue
                if not found_watermark:
                    new_entries.append(entry)
            # If watermark not found (feed changed), take all
            if not found_watermark:
                new_entries = entries
            entries = new_entries

        # Limit per fetch
        max_items = self.source_config.get("max_items_per_fetch", 50)
        entries = entries[:max_items]

        # Enrich entries with article metadata and cached thumbnails
        enrich = self.source_config.get("enrich_articles", True)
        if enrich:
            entries = _enrich_entries(entries)

        # Inject connector metadata into each entry (category, region, source label)
        connector_label = self.source_config.get("label", "")
        connector_category = self.source_config.get("category", "")
        connector_region = self.source_config.get("region", "")

        # Convert to pipeline items
        items = []
        for entry in entries:
            if connector_label and not entry.get("source"):
                entry["source"] = connector_label
            if connector_category:
                entry["category"] = connector_category
            if connector_region:
                entry["region"] = connector_region
            raw = json.dumps(entry, ensure_ascii=False, default=str).encode("utf-8")
            items.append({
                "raw_bytes": raw,
                "content_type": "application/json",
                "source_type": "rss_feed",
            })

        return items

    def update_watermark(self, items: list[dict]) -> dict:
        if not items:
            return self.watermark

        # Use the first item's entry_id as watermark (feeds are newest-first)
        first_raw = items[0].get("raw_bytes", b"{}")
        try:
            first = json.loads(first_raw)
            entry_id = first.get("id", first.get("link", ""))
        except (json.JSONDecodeError, AttributeError):
            entry_id = ""

        return {
            "last_entry_id": entry_id,
            "last_fetch_at": datetime.now(timezone.utc).isoformat(),
            "items_fetched": len(items),
        }


def _parse_feed(root: ET.Element) -> list[dict]:
    """Parse RSS 2.0 or Atom feed into entry dicts."""
    entries = []

    # Try Atom format (<feed><entry>)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    atom_entries = root.findall("atom:entry", ns) or root.findall("entry")
    if atom_entries:
        for entry in atom_entries:
            entries.append(_parse_atom_entry(entry, ns))
        return entries

    # Try RSS 2.0 format (<rss><channel><item>)
    channel = root.find("channel")
    if channel is not None:
        for item in channel.findall("item"):
            entries.append(_parse_rss_item(item))
        return entries

    return entries


def _parse_atom_entry(entry: ET.Element, ns: dict) -> dict:
    """Parse a single Atom entry."""
    def _text(tag):
        el = entry.find(f"atom:{tag}", ns)
        if el is None:
            el = entry.find(tag)
        if el is None:
            # Try with full namespace URI
            el = entry.find(f"{{{ns.get('atom', '')}}}{tag}")
        return el.text.strip() if el is not None and el.text else ""

    link = ""
    link_el = entry.find("atom:link", ns)
    if link_el is None:
        link_el = entry.find("link")
    if link_el is None:
        link_el = entry.find(f"{{{ns.get('atom', '')}}}link")
    if link_el is not None:
        link = link_el.get("href", link_el.text or "")

    entry_id = _text("id") or link
    if not entry_id:
        entry_id = hashlib.md5((_text("title") + link).encode()).hexdigest()

    summary = _text("summary") or _text("content")

    return {
        "id": entry_id,
        "title": _text("title"),
        "link": link,
        "summary": summary,
        "published": _text("published") or _text("updated"),
        "author": _text("author"),
        "image_url": _extract_atom_image(entry, ns, summary),
    }


def _extract_atom_image(entry: ET.Element, ns: dict, summary: str) -> str:
    """Extract best image URL from Atom entry."""
    # media:content
    mc = entry.find(f"{{{MEDIA_NS}}}content")
    if mc is not None and mc.get("url"):
        mtype = mc.get("type", "")
        if not mtype or mtype.startswith("image"):
            return mc.get("url", "")

    # media:thumbnail
    mt = entry.find(f"{{{MEDIA_NS}}}thumbnail")
    if mt is not None and mt.get("url"):
        return mt.get("url", "")

    # link rel="enclosure" with image type
    for link_el in entry.findall("atom:link", ns) or entry.findall("link"):
        if link_el.get("rel") == "enclosure" and link_el.get("type", "").startswith("image"):
            return link_el.get("href", "")

    # img tag in summary/content
    if summary:
        m = re.search(r'<img[^>]+src=["\']([^"\']+)', summary, re.IGNORECASE)
        if m:
            return m.group(1)

    return ""


def _parse_rss_item(item: ET.Element) -> dict:
    """Parse a single RSS 2.0 item."""
    def _text(tag):
        el = item.find(tag)
        return el.text.strip() if el is not None and el.text else ""

    guid = _text("guid") or _text("link")
    if not guid:
        guid = hashlib.md5((_text("title") + _text("link")).encode()).hexdigest()

    return {
        "id": guid,
        "title": _text("title"),
        "link": _text("link"),
        "summary": _text("description"),
        "published": _text("pubDate"),
        "author": _text("author") or _text("dc:creator"),
        "image_url": _extract_rss_image(item, _text("description")),
    }


def _extract_rss_image(item: ET.Element, description: str) -> str:
    """Extract best image URL from RSS item via media:content, enclosure, or img tag."""
    # 1. media:content
    mc = item.find(f"{{{MEDIA_NS}}}content")
    if mc is not None and mc.get("url"):
        mtype = mc.get("type", "")
        if not mtype or mtype.startswith("image"):
            return mc.get("url", "")

    # 2. media:thumbnail
    mt = item.find(f"{{{MEDIA_NS}}}thumbnail")
    if mt is not None and mt.get("url"):
        return mt.get("url", "")

    # 3. enclosure with image type
    enc = item.find("enclosure")
    if enc is not None and enc.get("type", "").startswith("image"):
        return enc.get("url", "")

    # 4. First <img src="..."> in description HTML
    if description:
        m = re.search(r'<img[^>]+src=["\']([^"\']+)', description, re.IGNORECASE)
        if m:
            return m.group(1)

    return ""


def _enrich_entries(entries: list[dict]) -> list[dict]:
    """Enrich RSS entries with article metadata and cached thumbnails.

    For each entry with a link, fetches og:image/description/first paragraph
    and caches the thumbnail image locally for proxy serving.
    """
    from storage.connectors.article_extract import extract_article_metadata
    from storage.cache.thumbnails import cache_thumbnail

    for entry in entries:
        link = entry.get("link", "")
        if not link:
            continue

        try:
            meta = extract_article_metadata(link)

            # Fill in missing summary from og:description
            if meta.get("og_description") and not entry.get("summary"):
                entry["summary"] = meta["og_description"]

            # Fill in missing author
            if meta.get("author") and not entry.get("author"):
                entry["author"] = meta["author"]

            # Store full article text for in-platform reading
            if meta.get("full_text"):
                entry["full_text"] = meta["full_text"]
                entry["word_count"] = meta.get("word_count", 0)

            # Store first paragraph for card preview
            if meta.get("first_paragraph"):
                entry["first_paragraph"] = meta["first_paragraph"]

            # Store og:description separately (may differ from summary)
            if meta.get("og_description"):
                entry["og_description"] = meta["og_description"]

            # Determine best image: feed-provided > og:image
            image_url = entry.get("image_url") or meta.get("og_image")
            if image_url:
                cache_key = cache_thumbnail(image_url)
                if cache_key:
                    entry["cached_thumbnail"] = cache_key
                    entry["thumbnail_url"] = f"/api/thumbnails/{cache_key}"
                else:
                    # Fallback to external URL (may break due to hotlink protection)
                    entry["thumbnail_url"] = image_url

        except Exception as e:
            log.debug(f"Entry enrichment failed for {link}: {e}")

    return entries
