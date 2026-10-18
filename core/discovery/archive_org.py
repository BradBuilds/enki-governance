"""
archive.org search + acquisition client.

Internet Archive (archive.org) hosts millions of public-domain books,
scanned government reports, declassified documents, and academic
papers. Their advanced search API returns metadata for matching items;
each item's identifier maps to a download URL.

Search API:
    https://archive.org/advancedsearch.php
        q=<query>&fl=identifier,title,creator,date,description,mediatype,format
        &rows=N&output=json

Download:
    https://archive.org/download/<identifier>/<filename>.pdf
    (item details: https://archive.org/details/<identifier>)

This client provides both discovery (search) and acquisition (best-PDF
download) so the acquirer can try archive.org as a primary source.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

import requests

from core.discovery.rate_limiter import get_limiter, back_off

log = logging.getLogger("enki.discovery.archive_org")

_SEARCH_URL    = "https://archive.org/advancedsearch.php"
_DETAILS_URL   = "https://archive.org/metadata/{identifier}"
_DOWNLOAD_BASE = "https://archive.org/download"
_USER_AGENT    = "EnkiResearch/0.1 (https://github.com/BradBuilds/enki-governance; document-research)"
_DEFAULT_TIMEOUT = 30


def search(query: str, *, max_results: int = 50,
           mediatype: Optional[str] = "texts") -> list[dict]:
    """Search archive.org for the query. Returns normalized reference dicts."""
    if not query:
        return []
    params = {
        "q": query if not mediatype else f"({query}) AND mediatype:{mediatype}",
        "fl[]": ["identifier", "title", "creator", "date", "description",
                 "mediatype", "language", "subject"],
        "rows": min(max_results, 50),
        "output": "json",
    }
    headers = {"User-Agent": _USER_AGENT}
    get_limiter("archive_org").wait()
    try:
        r = requests.get(_SEARCH_URL, params=params, timeout=_DEFAULT_TIMEOUT,
                         headers=headers)
        if r.status_code == 429:
            back_off("archive_org", multiplier=2.0)
            log.warning("archive_org rate-limited (HTTP 429), backed off")
            return []
        r.raise_for_status()
        body = r.json()
    except Exception as e:
        log.warning("archive.org search failed: %s", e)
        return []
    docs = (body.get("response") or {}).get("docs") or []
    out: list[dict] = []
    for d in docs:
        out.append(_normalize(d, query))
    return out


def _normalize(item: dict, query: str) -> dict:
    title = (item.get("title") or "").strip() or "(untitled)"
    creators = item.get("creator")
    if isinstance(creators, str):
        creators = [creators]
    elif not creators:
        creators = []
    date = (item.get("date") or "").strip()
    year: Optional[int] = None
    if date:
        try:
            year = int(date[:4])
        except Exception:
            year = None
    identifier = item.get("identifier")
    return {
        "citation_text":   ", ".join(creators[:3]) + (" — " if creators else "") + title
                           + (f" ({year})" if year else ""),
        "title":           title,
        "authors":         creators,
        "publication_year": year,
        "identifiers":     {"archive_id": identifier},
        "reference_type":  ("book" if (item.get("mediatype") == "texts") else "document"),
        "info_link":       f"https://archive.org/details/{identifier}",
        "discovered_via":  "archive_org",
        "discovery_query": query,
        "free_preview_available": True,
    }


def list_files(identifier: str) -> list[dict]:
    """List files inside an archive.org item. Used to find downloadable PDF."""
    headers = {"User-Agent": _USER_AGENT}
    url = _DETAILS_URL.format(identifier=identifier)
    get_limiter("archive_org").wait()
    try:
        r = requests.get(url, timeout=_DEFAULT_TIMEOUT, headers=headers)
        if r.status_code == 429:
            back_off("archive_org", multiplier=2.0)
            return []
        r.raise_for_status()
        body = r.json()
    except Exception as e:
        log.warning("archive.org list_files failed for %s: %s", identifier, e)
        return []
    files = body.get("files") or []
    return files


def best_pdf_url(identifier: str) -> Optional[str]:
    """Find the best PDF download URL for an archive.org item.

    Strategy: pick the largest .pdf file; fall back to text-derived
    formats if no PDF exists.
    """
    files = list_files(identifier)
    pdfs: list[tuple[int, str]] = []
    for f in files:
        name = f.get("name") or ""
        if name.lower().endswith(".pdf"):
            try:
                size = int(f.get("size") or 0)
            except Exception:
                size = 0
            pdfs.append((size, name))
    if not pdfs:
        return None
    # Prefer largest (usually the main scanned PDF)
    pdfs.sort(reverse=True)
    _, name = pdfs[0]
    return f"{_DOWNLOAD_BASE}/{identifier}/{name}"


def download(identifier: str, dest_dir: Path,
             *, max_bytes: int = 200_000_000) -> Optional[dict]:
    """Download the best PDF for an archive.org item. Returns dict with
    file_path / file_size / file_sha256 / url on success, None on
    failure."""
    import hashlib
    url = best_pdf_url(identifier)
    if not url:
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = url.rsplit("/", 1)[-1].replace("?", "_")
    target = dest_dir / fname
    headers = {"User-Agent": _USER_AGENT}
    sha = hashlib.sha256()
    size = 0
    get_limiter("archive_org").wait()
    try:
        with requests.get(url, stream=True, timeout=120, headers=headers) as r:
            if r.status_code == 429:
                back_off("archive_org", multiplier=2.0)
                return None
            r.raise_for_status()
            with open(target, "wb") as fp:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    fp.write(chunk)
                    sha.update(chunk)
                    size += len(chunk)
                    if size > max_bytes:
                        log.warning("archive.org download exceeded max_bytes for %s", identifier)
                        fp.close()
                        target.unlink(missing_ok=True)
                        return None
    except Exception as e:
        log.warning("archive.org download failed for %s: %s", identifier, e)
        return None
    return {
        "url":          url,
        "file_path":    str(target),
        "file_size":    size,
        "file_sha256":  sha.hexdigest(),
    }
