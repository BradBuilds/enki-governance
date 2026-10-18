"""
HTML Parser — extracts text, links, title, and metadata from HTML pages.

Uses BeautifulSoup for extraction. Handles web pages, saved articles, reports.
"""

from __future__ import annotations

import hashlib
from typing import Optional

from storage.ingest.parsers import ParseResult, register_parser
from storage.ingest.parsers.base import BaseParser
from storage.signals.models import Observable


@register_parser
class HtmlParser(BaseParser):
    name = "html"

    def can_parse(self, raw_bytes: bytes, content_type: Optional[str] = None) -> float:
        if content_type and "html" in content_type.lower():
            return 0.92
        # Check for HTML-like content
        try:
            start = raw_bytes[:500].decode("utf-8", errors="ignore").lower().strip()
            if start.startswith("<!doctype html") or start.startswith("<html"):
                return 0.90
            if "<head" in start and "<body" in start:
                return 0.85
            if start.count("<") > 3 and ("<div" in start or "<p " in start or "<a " in start):
                return 0.70
        except Exception:
            pass
        return 0.0

    def parse(self, raw_bytes: bytes) -> ParseResult:
        file_hash = hashlib.sha256(raw_bytes).hexdigest()

        try:
            from bs4 import BeautifulSoup
            from charset_normalizer import from_bytes

            # Detect encoding
            result = from_bytes(raw_bytes)
            best = result.best()
            text_content = str(best) if best else raw_bytes.decode("utf-8", errors="replace")

            soup = BeautifulSoup(text_content, "html.parser")

            # Title
            title = ""
            if soup.title and soup.title.string:
                title = soup.title.string.strip()

            # Meta tags
            meta = {}
            for tag in soup.find_all("meta"):
                name = tag.get("name", tag.get("property", "")).lower()
                content = tag.get("content", "")
                if name and content:
                    meta[name] = content[:500]

            # Text content (strip tags)
            body = soup.find("body")
            body_text = body.get_text(separator="\n", strip=True) if body else soup.get_text(separator="\n", strip=True)

            # Links
            links = []
            for a in soup.find_all("a", href=True)[:50]:
                href = a["href"]
                link_text = a.get_text(strip=True)[:100]
                if href.startswith("http"):
                    links.append({"url": href, "text": link_text})

            # Images
            images = []
            for img in soup.find_all("img", src=True)[:20]:
                images.append({"src": img["src"], "alt": img.get("alt", "")[:100]})

            # Observables
            observables = []
            seen = set()
            for link in links:
                url = link["url"]
                if url not in seen:
                    observables.append(Observable(observable_type="url", observable_value=url, normalized_value=url, source_field="html_link"))
                    seen.add(url)

            import re
            for match in re.finditer(r'[\w.+-]+@[\w.-]+\.\w+', body_text[:10000]):
                val = match.group().lower()
                if val not in seen:
                    observables.append(Observable(observable_type="email_address", observable_value=match.group(), normalized_value=val, source_field="html_text"))
                    seen.add(val)

            parsed = {
                "title": title or meta.get("og:title", meta.get("title", "Untitled")),
                "description": meta.get("description", meta.get("og:description", ""))[:500],
                "author": meta.get("author", ""),
                "text": body_text[:3000],
                "full_text_length": len(body_text),
                "link_count": len(links),
                "image_count": len(images),
                "links": links[:10],
                "file_sha256": file_hash,
                "size_bytes": len(raw_bytes),
            }

            return ParseResult(
                format_detected="html",
                confidence=0.90,
                parsed=parsed,
                metadata={
                    "title": title,
                    "link_count": len(links),
                    "image_count": len(images),
                    "encoding": str(best.encoding) if best else "utf-8",
                },
                observables=observables[:50],
                signal_type_hint="signal.content.web_page",
                parser_name="html",
            )

        except Exception as e:
            return ParseResult(
                format_detected="html",
                confidence=0.40,
                parsed={"file_sha256": file_hash, "size_bytes": len(raw_bytes), "parse_error": str(e)},
                metadata={"parse_error": str(e)},
                observables=[Observable(observable_type="file_hash_sha256", observable_value=file_hash, normalized_value=file_hash, source_field="file")],
                signal_type_hint="signal.content.web_page",
                parser_name="html",
            )
