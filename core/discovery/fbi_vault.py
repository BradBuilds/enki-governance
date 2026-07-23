"""
FBI Vault (vault.fbi.gov) search client.

The FBI Vault is the FBI's FOIA reading room: declassified files on
historical investigations (UFO, organized crime, civil rights cases,
notable persons). Many are direct PDF downloads.

The search endpoint:
    https://vault.fbi.gov/@@search?SearchableText=<query>

Each result has a relative URL to a folder containing one or more PDFs.

Like CREST, this is older infrastructure — we go slowly (5s default).
"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote_plus

import requests

from core.discovery.rate_limiter import get_limiter, back_off

log = logging.getLogger("enki.discovery.fbi_vault")

_BASE = "https://vault.fbi.gov"
# FBI Vault rebuilt 2026: old /@@search returns 404. The new endpoint
# tree is /fdps-1/@@search-fdps + /browse-files. Try both; whichever
# returns content gets parsed.
_SEARCH_URLS = [
    f"{_BASE}/fdps-1/@@search-fdps",
    f"{_BASE}/browse-files",
    f"{_BASE}/@@search",   # legacy fallback
]
_USER_AGENT = "EnkiResearch/0.1 (https://github.com/BradBuilds/enki-governance; document-research)"
_DEFAULT_TIMEOUT = 30


def search(query: str, *, max_results: int = 30) -> list[dict]:
    """Search FBI Vault. Returns normalized refs. Tries multiple URL
    patterns since FBI rebuilds the Vault every few years and the
    search endpoint moves."""
    if not query:
        return []
    headers = {"User-Agent": _USER_AGENT, "Accept": "text/html"}
    html = ""
    for url in _SEARCH_URLS:
        params = {"SearchableText": query, "b_size": min(max_results, 50)}
        get_limiter("fbi_vault").wait()
        try:
            r = requests.get(url, params=params, timeout=_DEFAULT_TIMEOUT, headers=headers)
            if r.status_code == 429:
                back_off("fbi_vault", multiplier=2.0)
                return []
            if r.status_code != 200:
                log.debug("FBI Vault %s returned %s", url, r.status_code)
                continue
            html = r.text or ""
            if html and len(html) > 5000:
                break
        except Exception as e:
            log.debug("FBI Vault %s failed: %s", url, e)
            continue
    if not html:
        log.warning("FBI Vault: no endpoint returned content for query=%r", query)
        return []

    # Result items vary by FBI Vault template. We try multiple patterns
    # and keep all valid-looking case-folder URLs (path doesn't end in
    # common-page markers like @@, /search, /portal_javascripts, etc.).
    refs: list[dict] = []
    seen: set[str] = set()

    # Pattern A: explicit url class anchors (Plone classic theme)
    pattern_a = re.compile(
        r'<a[^>]+class="[^"]*url[^"]*"[^>]+href="(/[^"]+)"[^>]*>(.*?)</a>',
        re.DOTALL,
    )
    # Pattern B: any anchor under a search-results region
    pattern_b = re.compile(
        r'<a[^>]+href="(/[^"]+)"[^>]*>\s*<span[^>]*>(.*?)</span>',
        re.DOTALL,
    )
    # Pattern C: generic h3/h2 wrapped anchors (newer themes)
    pattern_c = re.compile(
        r'<h[23][^>]*>\s*<a[^>]+href="(/[^"]+)"[^>]*>(.*?)</a>',
        re.DOTALL,
    )

    EXCLUDED_PREFIXES = ("/@@", "/search", "/portal", "/sitemap", "/login",
                          "/contact-us", "/policies", "/site-map", "/++", "#")

    for pat in (pattern_a, pattern_b, pattern_c):
        for m in pat.finditer(html):
            rel = m.group(1).strip()
            text = re.sub(r"<[^>]+>", " ", m.group(2)).strip()
            text = re.sub(r"\s+", " ", text)
            if any(rel.startswith(p) for p in EXCLUDED_PREFIXES):
                continue
            if rel in seen or len(rel) < 4:
                continue
            seen.add(rel)
            if not text:
                text = rel.rsplit("/", 1)[-1]
            if len(text) < 3:
                continue
            refs.append({
                "citation_text":  f"FBI Vault: {text}",
                "title":          text[:200],
                "authors":        [],
                "publication_year": None,
                "identifiers":    {
                    "fbi_vault_path": rel,
                    "url": f"{_BASE}{rel}",
                },
                "reference_type": "foia_release",
                "info_link":      f"{_BASE}{rel}",
                "discovered_via": "fbi_vault",
                "discovery_query": query,
                "free_preview_available": True,
            })
            if len(refs) >= max_results:
                return refs
    return refs
