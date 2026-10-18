"""
Google Books search client.

The public Google Books search endpoint (https://www.googleapis.com/books/v1/volumes)
returns a JSON list of volumes for a query. No API key needed for the
free quota (~1,000 unauthenticated calls/day from a single IP, more
than sufficient for ENKI's research cadence). If a key IS configured
via env, we use it for higher quota.

Output: a list of canonical reference dicts the acquirer can chew on.

Volume metadata of interest:
    title, authors, publisher, publishedDate (year),
    industryIdentifiers (ISBN_10/ISBN_13/OTHER),
    pageCount, language, infoLink, previewLink,
    saleInfo.saleability ("FREE", "FOR_SALE", "NOT_FOR_SALE"),
    accessInfo.epub.acsTokenLink (FREE PDF if available)
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

import requests

from core.discovery.rate_limiter import get_limiter, back_off

log = logging.getLogger("enki.discovery.google_books")

_BASE_URL = "https://www.googleapis.com/books/v1/volumes"
_DEFAULT_TIMEOUT = 20
_DEFAULT_MAX_RESULTS = 40   # Google Books cap per call
_USER_AGENT = "EnkiResearch/0.1 (https://github.com/BradBuilds/enki-governance; document-research)"


def _api_key() -> Optional[str]:
    return os.environ.get("GOOGLE_BOOKS_API_KEY") or None


def search(query: str, *, max_results: int = 40, lang: str = "en") -> list[dict]:
    """Search Google Books for a query string. Returns at most `max_results`
    normalized reference dicts.

    Each dict has the shape:
        {
          "citation_text": "<author> — <title> (<year>)",
          "title": "...",
          "authors": [...],
          "publisher": "...",
          "publication_year": 1995,
          "identifiers": {"isbn": "...", "google_books_id": "..."},
          "reference_type": "book",
          "info_link": "...",
          "preview_link": "...",
          "discovered_via": "google_books",
          "discovery_query": "<query>",
          "free_preview_available": True/False
        }
    """
    if not query:
        return []
    out: list[dict] = []
    start = 0
    key = _api_key()
    headers = {"User-Agent": _USER_AGENT}
    limiter = get_limiter("google_books")
    while start < max_results:
        n = min(_DEFAULT_MAX_RESULTS, max_results - start)
        params = {
            "q": query,
            "maxResults": n,
            "startIndex": start,
            "langRestrict": lang,
            "printType": "books",
        }
        if key:
            params["key"] = key
        limiter.wait()
        try:
            r = requests.get(_BASE_URL, params=params, timeout=_DEFAULT_TIMEOUT,
                             headers=headers)
            if r.status_code == 429:
                back_off("google_books", multiplier=2.0)
                log.warning("google_books rate-limited (HTTP 429), backed off")
                break
            r.raise_for_status()
            body = r.json()
        except Exception as e:
            log.warning("google_books search failed (start=%d): %s", start, e)
            break
        items = body.get("items") or []
        if not items:
            break
        for item in items:
            ref = _normalize(item, query)
            if ref:
                out.append(ref)
        start += n
        if len(items) < n:
            break
    return out


def _normalize(item: dict, query: str) -> Optional[dict]:
    info = item.get("volumeInfo") or {}
    sale = item.get("saleInfo") or {}
    access = item.get("accessInfo") or {}
    title = (info.get("title") or "").strip()
    if not title:
        return None
    authors = info.get("authors") or []
    publisher = info.get("publisher")
    published = info.get("publishedDate") or ""
    year: Optional[int] = None
    if published:
        try:
            year = int(published[:4])
        except Exception:
            year = None

    identifiers: dict = {"google_books_id": item.get("id")}
    for ii in info.get("industryIdentifiers") or []:
        ident_type = (ii.get("type") or "").lower()
        ident_val  = ii.get("identifier")
        if not ident_val:
            continue
        if "isbn" in ident_type:
            identifiers.setdefault("isbn", ident_val)
            identifiers[ident_type.lower()] = ident_val
        else:
            identifiers[ident_type.lower()] = ident_val

    citation = ", ".join(authors[:3]) + ("" if not authors else " — ") + title
    if year:
        citation += f" ({year})"
    citation = citation.strip(" —")

    free_preview = bool(access.get("pdf", {}).get("isAvailable")
                          or access.get("epub", {}).get("isAvailable"))
    saleability = sale.get("saleability") or "UNKNOWN"

    return {
        "citation_text":   citation,
        "title":           title,
        "authors":         authors,
        "publisher":       publisher,
        "publication_year": year,
        "identifiers":     identifiers,
        "reference_type":  "book",
        "info_link":       info.get("infoLink"),
        "preview_link":    info.get("previewLink"),
        "discovered_via":  "google_books",
        "discovery_query": query,
        "free_preview_available": free_preview,
        "saleability":     saleability,
    }
