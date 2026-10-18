"""
CIA Reading Room (CREST) search client.

The CIA's Reading Room (https://www.cia.gov/readingroom) hosts
declassified documents — FOIA releases, historical reports, briefings.
This is gold for any topic touching US intelligence (UFO/UAP, Cold War,
covert ops, foreign leaders, JFK, MK-Ultra).

There's no clean public API; we scrape the search JSON endpoint:
    https://www.cia.gov/readingroom/search/site/<query>?page=N

Each result has a document_id like `CIA-RDP74-00297R000200030017-2`
which maps to a download URL.

Be polite — rate-limited to 5-second intervals by default (see
rate_limiter._DEFAULTS). The CIA Reading Room is cranky old infra.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import requests

from core.discovery.rate_limiter import get_limiter, back_off

log = logging.getLogger("enki.discovery.cia_reading_room")

_SEARCH_HTML_URL = "https://www.cia.gov/readingroom/search/site/{query}"
_USER_AGENT    = "EnkiResearch/0.1 (https://github.com/BradBuilds/enki-governance; document-research)"
_DEFAULT_TIMEOUT = 30


def search(query: str, *, max_results: int = 30) -> list[dict]:
    """Search the CIA Reading Room. Returns normalized refs.

    Strategy: fetch the HTML search results page and parse anchors
    matching /readingroom/document/<id> OR the newer /readingroom/docs/
    pattern. Be tolerant of HTML structure changes — we just need the
    document identifier + title text.
    """
    if not query:
        return []
    url = _SEARCH_HTML_URL.format(query=requests.utils.quote(query))
    headers = {"User-Agent": _USER_AGENT, "Accept": "text/html,*/*"}
    get_limiter("cia_reading_room").wait()
    # Use a Session with bounded redirects — CIA's search 2026 SPA
    # rebuild creates redirect loops on some queries. Cap at 5.
    sess = requests.Session()
    sess.max_redirects = 5
    try:
        r = sess.get(url, timeout=_DEFAULT_TIMEOUT, headers=headers,
                     allow_redirects=True)
        if r.status_code == 429:
            back_off("cia_reading_room", multiplier=2.0)
            return []
        if r.status_code != 200:
            log.debug("CREST search returned %s", r.status_code)
            return []
        html = r.text or ""
    except requests.exceptions.TooManyRedirects:
        log.debug("CREST search hit redirect loop for query=%r", query)
        return []
    except Exception as e:
        log.warning("CREST search failed: %s", e)
        return []

    # Tolerant pattern: any href containing /readingroom/document/ or
    # /readingroom/docs/. Document IDs can include letters/digits/hyphens.
    refs: list[dict] = []
    seen: set[str] = set()
    doc_pattern = re.compile(
        r'href="(/readingroom/(?:document|docs)/([A-Za-z0-9_\-]+))"[^>]*>(.*?)</a>',
        re.DOTALL | re.IGNORECASE,
    )
    for m in doc_pattern.finditer(html):
        rel_url = m.group(1)
        doc_id  = m.group(2)
        link_text = re.sub(r"<[^>]+>", " ", m.group(3)).strip()
        link_text = re.sub(r"\s+", " ", link_text)
        if doc_id in seen:
            continue
        seen.add(doc_id)
        # Filter out obvious nav links (pagination, etc.) — real
        # document IDs are long
        if len(doc_id) < 8:
            continue
        refs.append({
            "citation_text":  f"CIA Reading Room {doc_id}" + (f": {link_text[:120]}" if link_text else ""),
            "title":          link_text or doc_id,
            "authors":        [],
            "publication_year": None,
            "identifiers":    {
                "cia_doc_id": doc_id,
                "url": f"https://www.cia.gov{rel_url}",
            },
            "reference_type": "government_report",
            "info_link":      f"https://www.cia.gov{rel_url}",
            "discovered_via": "cia_reading_room",
            "discovery_query": query,
            "free_preview_available": True,
        })
        if len(refs) >= max_results:
            break
    return refs
