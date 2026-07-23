"""
OpenLibrary search client.

OpenLibrary (https://openlibrary.org) is the Internet Archive's open
catalog of books. Free, no API key needed, generous rate limits, very
rich metadata (ISBN, author keys, work editions, borrow URLs).

Use cases:
  1. Search by topic — returns book editions with ISBNs
  2. Lookup by ISBN — returns the canonical work + edition details
  3. Borrow URL detection — when a book has an ebook available for
     free 1-hour or 14-day borrow

Endpoint: https://openlibrary.org/search.json?q=<query>&limit=N
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

from core.discovery.rate_limiter import get_limiter, back_off

log = logging.getLogger("enki.discovery.openlibrary")

_SEARCH_URL    = "https://openlibrary.org/search.json"
_USER_AGENT    = "EnkiResearch/0.1 (https://github.com/BradBuilds/enki-governance; document-research)"
_DEFAULT_TIMEOUT = 30


def search(query: str, *, max_results: int = 40) -> list[dict]:
    """Search OpenLibrary for the topic. Returns normalized refs."""
    if not query:
        return []
    params = {
        "q": query,
        "limit": min(max_results, 100),
        "fields": "key,title,author_name,first_publish_year,isbn,publisher,subject,ebook_count_i,has_fulltext,ia,language,number_of_pages_median",
    }
    headers = {"User-Agent": _USER_AGENT}
    get_limiter("openlibrary").wait()
    try:
        r = requests.get(_SEARCH_URL, params=params, timeout=_DEFAULT_TIMEOUT, headers=headers)
        if r.status_code == 429:
            back_off("openlibrary", multiplier=2.0)
            log.warning("openlibrary rate-limited")
            return []
        r.raise_for_status()
        body = r.json()
    except Exception as e:
        log.warning("openlibrary search failed: %s", e)
        return []
    docs = body.get("docs") or []
    return [_normalize(d, query) for d in docs[:max_results]]


def _normalize(doc: dict, query: str) -> dict:
    title = (doc.get("title") or "").strip() or "(untitled)"
    authors = doc.get("author_name") or []
    year = doc.get("first_publish_year")
    isbns = doc.get("isbn") or []
    pubs = doc.get("publisher") or []
    publisher = pubs[0] if pubs else None
    work_key = doc.get("key")    # /works/OL12345W
    archive_id = None
    ia_list = doc.get("ia") or []
    if ia_list:
        archive_id = ia_list[0]

    identifiers: dict = {}
    if isbns:
        identifiers["isbn"] = isbns[0]
        # Distinguish 10 vs 13
        for i in isbns:
            if len(i) == 13: identifiers["isbn_13"] = i
            elif len(i) == 10: identifiers["isbn_10"] = i
    if work_key:
        identifiers["ol_id"] = work_key.rsplit("/", 1)[-1] if "/" in work_key else work_key
    if archive_id:
        identifiers["archive_id"] = archive_id

    citation = ", ".join(authors[:3]) + (" — " if authors else "") + title
    if year:
        citation += f" ({year})"

    return {
        "citation_text":   citation,
        "title":           title,
        "authors":         authors,
        "publisher":       publisher,
        "publication_year": year,
        "identifiers":     identifiers,
        "reference_type":  "book",
        "info_link":       f"https://openlibrary.org{work_key}" if work_key else None,
        "discovered_via":  "openlibrary",
        "discovery_query": query,
        "free_preview_available": bool(doc.get("has_fulltext") or doc.get("ebook_count_i")),
    }
