"""
Wikipedia bibliography parser.

Wikipedia articles end with "References", "Further reading", and
"Bibliography" sections that cite books, papers, and primary sources.
For a topic like "Roswell incident" the Wikipedia article cites 50-200
distinct works — a much higher-quality starting bibliography than any
keyword search.

Strategy:
  1. Fetch the article using Wikipedia's REST API (clean wikitext).
  2. Find the References / Bibliography / Further reading sections.
  3. Parse each citation:
       - {{cite book|...}}     templated citations (most accurate)
       - {{cite journal|...}}
       - {{cite news|...}}
       - free-text bibliography entries (heuristic parse)

Output: list of canonical reference dicts.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import requests

from core.discovery.rate_limiter import get_limiter, back_off

log = logging.getLogger("enki.discovery.wikipedia_bibliography")

_API_URL = "https://en.wikipedia.org/w/api.php"
_USER_AGENT = "EnkiResearch/0.1 (https://github.com/BradBuilds/enki-governance; document-research)"
_DEFAULT_TIMEOUT = 30

_REF_SECTION_NAMES = {
    "references", "bibliography", "further reading", "works cited",
    "selected bibliography", "sources", "cited works",
}


def fetch_wikitext(title: str) -> Optional[str]:
    """Fetch the wikitext of a Wikipedia article by title."""
    params = {
        "action": "parse",
        "page": title,
        "prop": "wikitext",
        "format": "json",
        "redirects": 1,
    }
    headers = {"User-Agent": _USER_AGENT}
    get_limiter("wikipedia").wait()
    try:
        r = requests.get(_API_URL, params=params, timeout=_DEFAULT_TIMEOUT,
                         headers=headers)
        if r.status_code == 429:
            back_off("wikipedia", multiplier=2.0)
            return None
        r.raise_for_status()
        body = r.json()
    except Exception as e:
        log.warning("wikipedia fetch_wikitext failed for %s: %s", title, e)
        return None
    parse = body.get("parse") or {}
    wikitext = parse.get("wikitext")
    if isinstance(wikitext, dict):
        return wikitext.get("*")
    return wikitext


def parse_references(wikitext: str, *, topic_for_query: str = "") -> list[dict]:
    """Parse references from wikitext. Returns canonical reference dicts."""
    if not wikitext:
        return []
    refs: list[dict] = []

    # ── 1. Templated citations (high quality) ──
    # Match: {{cite book|...}} {{cite journal|...}} {{cite news|...}}
    cite_pat = re.compile(r"\{\{[Cc]ite\s+(book|journal|news|web|report|encyclopedia)\s*\|(.*?)\}\}",
                           re.DOTALL)
    for m in cite_pat.finditer(wikitext):
        kind = m.group(1).lower()
        body = m.group(2)
        parsed = _parse_template_fields(body)
        if parsed.get("title") or parsed.get("chapter"):
            refs.append(_normalize_template(parsed, kind, topic_for_query))

    # ── 2. Free-text bibliography lines (heuristic) ──
    # Look for "Last, First" or *<text>(year)<text> patterns inside
    # references sections.
    section_text = _extract_reference_sections(wikitext)
    if section_text:
        for line in section_text.splitlines():
            line = line.strip(" *#:•—-").strip()
            if len(line) < 20 or len(line) > 500:
                continue
            # Skip lines that are clearly already-parsed templates
            if line.startswith("{{") or line.startswith("|") or line.startswith("=="):
                continue
            year_match = re.search(r"\((\d{4})\)|\b(19|20)\d{2}\b", line)
            if year_match:
                year = int(year_match.group(0).strip("()")[-4:])
            else:
                year = None
            refs.append({
                "citation_text":   line[:300],
                "title":           line[:200],
                "authors":         [],
                "publication_year": year,
                "identifiers":     {},
                "reference_type":  "unknown",
                "discovered_via":  "wikipedia",
                "discovery_query": f"wikipedia:{topic_for_query}" if topic_for_query else "wikipedia",
            })

    return refs


def _parse_template_fields(body: str) -> dict:
    """Parse {{cite book|title=...|author=...}} fields."""
    fields: dict[str, str] = {}
    # Split on | but not inside [[...]] or <ref>...</ref>
    parts = re.split(r"\|(?![^\[]*\]\])", body)
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            k = k.strip().lower()
            v = v.strip().rstrip("|").strip()
            # Strip wikilinks: [[A|B]] → B
            v = re.sub(r"\[\[([^\|\]]+)\|([^\]]+)\]\]", r"\2", v)
            v = re.sub(r"\[\[([^\]]+)\]\]", r"\1", v)
            if v:
                fields[k] = v
    return fields


def _normalize_template(fields: dict, kind: str, topic_for_query: str) -> dict:
    title = fields.get("title") or fields.get("chapter") or ""
    authors: list[str] = []
    if fields.get("author"):
        authors = [fields["author"]]
    elif fields.get("last"):
        first = fields.get("first") or ""
        last  = fields.get("last") or ""
        authors = [f"{last}, {first}".strip(", ")]
    elif fields.get("authors"):
        authors = [a.strip() for a in re.split(r"[;,]\s*", fields["authors"]) if a.strip()]
    publisher = fields.get("publisher")
    year_str = (fields.get("year") or fields.get("date") or "").strip()
    year: Optional[int] = None
    if year_str:
        y = re.search(r"\d{4}", year_str)
        if y:
            year = int(y.group(0))
    isbn = fields.get("isbn")
    doi  = fields.get("doi")
    url  = fields.get("url")
    identifiers: dict = {}
    if isbn: identifiers["isbn"] = isbn.replace("-", "").strip()
    if doi:  identifiers["doi"]  = doi.strip()
    if url:  identifiers["url"]  = url.strip()

    return {
        "citation_text":   ", ".join(authors[:3]) + (" — " if authors else "") + title +
                           (f" ({year})" if year else ""),
        "title":           title,
        "authors":         authors,
        "publisher":       publisher,
        "publication_year": year,
        "identifiers":     identifiers,
        "reference_type":  {"book": "book", "journal": "paper", "news": "article",
                            "report": "report", "web": "article",
                            "encyclopedia": "article"}.get(kind, "unknown"),
        "info_link":       url,
        "discovered_via":  "wikipedia",
        "discovery_query": f"wikipedia:{topic_for_query}" if topic_for_query else "wikipedia",
    }


def _extract_reference_sections(wikitext: str) -> str:
    """Pull out the text under References / Bibliography / Further reading."""
    out_chunks: list[str] = []
    headers = re.split(r"\n==+\s*([^=]+?)\s*==+\n", wikitext)
    # split yields [pre, h1, body1, h2, body2, ...]
    for i in range(1, len(headers) - 1, 2):
        header = (headers[i] or "").strip().lower()
        if header in _REF_SECTION_NAMES:
            out_chunks.append(headers[i + 1] or "")
    return "\n".join(out_chunks)


def fetch_and_parse(title: str) -> list[dict]:
    """Convenience: fetch + parse in one call."""
    wt = fetch_wikitext(title)
    if not wt:
        return []
    return parse_references(wt, topic_for_query=title)
