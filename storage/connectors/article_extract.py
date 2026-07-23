"""
Article content extractor.

Fetches a source URL and extracts the full readable article text,
OpenGraph metadata (image, description), and author. Used during
RSS/news signal ingest so analysts read content in-platform without
ever navigating to the source site.

Approach: regex-based extraction (no BeautifulSoup dependency).
Targets <article>/<main> containers, strips nav/sidebar/footer/ads,
extracts all substantial <p> tags as the article body.
"""

import re
import logging
import time
from collections import defaultdict
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

log = logging.getLogger("enki.article_extract")

# Per-domain rate limiter: min 1s between requests to same domain
_domain_last_request: dict[str, float] = defaultdict(float)
_RATE_LIMIT_GAP = 1.0  # seconds

# Image magic bytes for validation
_IMAGE_MAGIC = {
    b"\xff\xd8": "image/jpeg",
    b"\x89PNG": "image/png",
    b"RIFF": "image/webp",
    b"GIF8": "image/gif",
}

# Tags to strip entirely (content and all)
_STRIP_TAGS = re.compile(
    r'<(script|style|nav|footer|aside|header|form|iframe|noscript|svg|button|input|select|textarea)'
    r'[^>]*>.*?</\1>',
    re.DOTALL | re.IGNORECASE,
)

# Common ad/sidebar class patterns to strip
_NOISE_PATTERNS = re.compile(
    r'<(?:div|section|aside)[^>]*class="[^"]*(?:sidebar|advert|promo|related|share|social|comment|signup|newsletter|popup|modal|cookie|consent|paywall)[^"]*"[^>]*>.*?</(?:div|section|aside)>',
    re.DOTALL | re.IGNORECASE,
)


def _extract_domain(url: str) -> str:
    """Extract domain from URL for rate limiting."""
    try:
        return url.split("//", 1)[1].split("/")[0].split(":")[0]
    except (IndexError, AttributeError):
        return "unknown"


def _rate_limit(domain: str):
    """Simple per-domain rate limiter."""
    now = time.monotonic()
    elapsed = now - _domain_last_request[domain]
    if elapsed < _RATE_LIMIT_GAP:
        time.sleep(_RATE_LIMIT_GAP - elapsed)
    _domain_last_request[domain] = time.monotonic()


def extract_article_metadata(url: str, timeout: int = 8, max_bytes: int = 1048576) -> dict:
    """Fetch URL and extract full article content + metadata.

    Returns dict with keys:
        og_image, og_description, full_text, first_paragraph, author, word_count
    Returns empty/partial dict on failure (never raises).
    """
    result = {
        "og_image": "",
        "og_description": "",
        "full_text": "",
        "first_paragraph": "",
        "author": "",
        "word_count": 0,
    }

    if not url or not url.startswith("http"):
        return result

    try:
        domain = _extract_domain(url)
        _rate_limit(domain)

        req = Request(url, headers={
            "User-Agent": "Enki/1.0 (Article Extractor)",
            "Accept": "text/html,application/xhtml+xml",
        })
        with urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" not in content_type and "xhtml" not in content_type:
                return result
            html = resp.read(max_bytes).decode("utf-8", errors="replace")

        # Extract OpenGraph and meta tags
        result["og_image"] = (
            _extract_meta(html, 'property="og:image"')
            or _extract_meta(html, "property='og:image'")
        )
        result["og_description"] = (
            _extract_meta(html, 'property="og:description"')
            or _extract_meta(html, "property='og:description'")
            or _extract_meta(html, 'name="description"')
            or ""
        )

        # Author
        result["author"] = (
            _extract_meta(html, 'name="author"')
            or _extract_meta(html, 'property="article:author"')
            or _extract_author_tag(html)
            or ""
        )

        # Full article text extraction
        full_text = _extract_full_text(html)
        result["full_text"] = full_text
        result["word_count"] = len(full_text.split()) if full_text else 0

        # First paragraph is the first chunk of the full text
        if full_text:
            paragraphs = full_text.split("\n\n")
            result["first_paragraph"] = paragraphs[0][:500] if paragraphs else ""
        else:
            result["first_paragraph"] = ""

    except (URLError, HTTPError, TimeoutError, OSError) as e:
        log.debug(f"Article extract failed for {url}: {e}")
    except Exception as e:
        log.debug(f"Article extract unexpected error for {url}: {e}")

    return result


def _extract_meta(html: str, attr_match: str) -> str:
    """Extract content attribute from a meta tag matching the given attribute."""
    pattern = rf'<meta\s[^>]*?{re.escape(attr_match)}[^>]*?content=["\']([^"\']*)["\'][^>]*?>'
    m = re.search(pattern, html, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    # Try reversed order (content before property)
    pattern2 = rf'<meta\s[^>]*?content=["\']([^"\']*)["\'][^>]*?{re.escape(attr_match)}[^>]*?>'
    m2 = re.search(pattern2, html, re.IGNORECASE)
    if m2:
        return m2.group(1).strip()

    return ""


def _extract_author_tag(html: str) -> str:
    """Try to find author from common HTML patterns."""
    # <span class="author">Name</span> or <a rel="author">Name</a>
    for pattern in [
        r'<(?:span|a|div)[^>]*class="[^"]*author[^"]*"[^>]*>([^<]+)',
        r'<a[^>]*rel="author"[^>]*>([^<]+)',
        r'"author"\s*:\s*"([^"]+)"',  # JSON-LD
    ]:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            author = m.group(1).strip()
            if 3 < len(author) < 100:
                return author
    return ""


def _extract_full_text(html: str) -> str:
    """Extract full readable article text from HTML.

    Strategy:
    1. Find the main content container (<article>, <main>, or role="main")
    2. Strip noise tags (script, style, nav, footer, aside, ads)
    3. Extract all <p> tags with substantial text
    4. Join into clean readable paragraphs
    """
    # Strip noise tags globally first
    clean = _STRIP_TAGS.sub('', html)
    clean = _NOISE_PATTERNS.sub('', clean)

    # Try to find the main content container
    paragraphs = []

    for container_pattern in [
        r'<article[^>]*>(.*?)</article>',
        r'<main[^>]*>(.*?)</main>',
        r'<div[^>]*role="main"[^>]*>(.*?)</div>',
        r'<div[^>]*class="[^"]*(?:article|post|entry|content|story)[^"]*"[^>]*>(.*?)</div>',
    ]:
        match = re.search(container_pattern, clean, re.DOTALL | re.IGNORECASE)
        if match:
            container_html = match.group(1)
            paragraphs = _extract_paragraphs(container_html)
            if len(paragraphs) >= 2:
                break

    # Fallback: extract from body
    if len(paragraphs) < 2:
        body_match = re.search(r'<body[^>]*>(.*?)</body>', clean, re.DOTALL | re.IGNORECASE)
        if body_match:
            paragraphs = _extract_paragraphs(body_match.group(1))

    # Last resort: all <p> tags anywhere
    if len(paragraphs) < 2:
        paragraphs = _extract_paragraphs(clean)

    if not paragraphs:
        return ""

    return "\n\n".join(paragraphs)


def _extract_paragraphs(html: str) -> list[str]:
    """Extract all substantial paragraphs from HTML fragment."""
    raw_paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', html, re.DOTALL | re.IGNORECASE)
    result = []

    for p in raw_paragraphs:
        # Strip HTML tags
        text = re.sub(r'<[^>]+>', '', p).strip()
        # Normalize whitespace
        text = re.sub(r'\s+', ' ', text)
        # Decode common HTML entities
        text = (text
                .replace('&amp;', '&')
                .replace('&lt;', '<')
                .replace('&gt;', '>')
                .replace('&quot;', '"')
                .replace('&#39;', "'")
                .replace('&nbsp;', ' ')
                .replace('&#x27;', "'")
                .replace('&mdash;', '—')
                .replace('&ndash;', '–')
                .replace('&hellip;', '…')
                )
        # Skip short/noise paragraphs
        if len(text) < 40:
            continue
        # Skip navigation/UI text patterns
        if re.match(r'^(Share|Tweet|Email|Print|Subscribe|Sign up|Log in|Read more|Advertisement)', text, re.IGNORECASE):
            continue
        result.append(text)

    return result


def validate_image_bytes(data: bytes) -> Optional[str]:
    """Check if bytes are a valid image. Returns content type or None."""
    for magic, ctype in _IMAGE_MAGIC.items():
        if data[:len(magic)] == magic:
            return ctype
    return None
