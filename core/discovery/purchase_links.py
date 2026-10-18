"""
Build standardized acquisition / purchase / borrow links for a reference.

For every bibliographic_reference we know exists, we surface the
fastest paths to actually obtaining it — Amazon, AbeBooks (used /
out-of-print), AddALL (price compare), OpenLibrary (free borrow when
available), WorldCat (find in a nearby library), Internet Archive
borrow page, Better World Books, Google Books preview.

Design intent: keep a permanent record of each reference so a reader can
buy or borrow the book, and link it to the node that lists the book.

The reference node is permanent regardless of acquisition state — when
we can't auto-download (paywall, out of print, borrow-protected), the
node still serves as a research lead with concrete purchase/borrow
options for the analyst.
"""

from __future__ import annotations

from urllib.parse import quote_plus, urlencode
from typing import Optional


def build_links(identifiers: dict, title: Optional[str] = None,
                authors: Optional[list[str]] = None,
                publication_year: Optional[int] = None,
                reference_type: Optional[str] = None) -> list[dict]:
    """Build a sorted list of acquisition link options for a reference.

    Each entry is a dict:
        {"vendor": "...", "kind": "buy|borrow|preview|search",
         "url": "...", "cost_hint": "...", "priority": int}

    Priority ranks links by likely usefulness:
      1 = direct download (free + legal)
      2 = free borrow / preview
      3 = used / library
      4 = paid purchase
      5 = generic search fallback

    Caller can filter, sort, and present as a vendor-chip strip in the UI.
    """
    out: list[dict] = []
    ids = identifiers or {}
    isbn = ids.get("isbn") or ids.get("isbn_13") or ids.get("isbn_10")
    doi  = ids.get("doi")
    ol_id = ids.get("ol_id")
    archive_id = ids.get("archive_id")
    google_books_id = ids.get("google_books_id")
    direct_url = ids.get("url")

    # ── Priority 1 — DIRECT FREE DOWNLOAD ───────────────────────────
    if archive_id:
        out.append({
            "vendor": "Internet Archive (direct)",
            "kind": "borrow",  # archive.org's borrow is free
            "url": f"https://archive.org/details/{archive_id}",
            "cost_hint": "free; some require 14-day borrow",
            "priority": 1,
        })
    if direct_url and direct_url.lower().endswith(".pdf"):
        out.append({
            "vendor": "Direct PDF",
            "kind": "download",
            "url": direct_url,
            "cost_hint": "free direct download",
            "priority": 1,
        })

    # ── Priority 2 — FREE PREVIEW / BORROW ──────────────────────────
    if isbn:
        out.append({
            "vendor": "OpenLibrary borrow",
            "kind": "borrow",
            "url": f"https://openlibrary.org/isbn/{isbn}",
            "cost_hint": "free 1-hour or 14-day borrow when available",
            "priority": 2,
        })
    if google_books_id:
        out.append({
            "vendor": "Google Books preview",
            "kind": "preview",
            "url": f"https://books.google.com/books?id={google_books_id}",
            "cost_hint": "free partial preview",
            "priority": 2,
        })

    # ── Priority 3 — LIBRARY (find a real copy) ─────────────────────
    if isbn:
        out.append({
            "vendor": "WorldCat (library finder)",
            "kind": "borrow",
            "url": f"https://www.worldcat.org/isbn/{isbn}",
            "cost_hint": "free at participating libraries",
            "priority": 3,
        })
    elif title:
        out.append({
            "vendor": "WorldCat search",
            "kind": "search",
            "url": f"https://www.worldcat.org/search?q={quote_plus(title)}",
            "cost_hint": "free at participating libraries",
            "priority": 3,
        })

    # ── Priority 4 — PURCHASE ──────────────────────────────────────
    if isbn:
        out.extend([
            {"vendor": "Amazon",         "kind": "buy",
             "url": f"https://www.amazon.com/dp/{isbn}",
             "cost_hint": "varies; new + used + Kindle",
             "priority": 4},
            {"vendor": "AbeBooks (used)", "kind": "buy",
             "url": f"https://www.abebooks.com/servlet/SearchResults?isbn={isbn}",
             "cost_hint": "used; often cheapest",
             "priority": 4},
            {"vendor": "Better World Books", "kind": "buy",
             "url": f"https://www.betterworldbooks.com/search/results?q={isbn}",
             "cost_hint": "used; ~$5-15 typical",
             "priority": 4},
            {"vendor": "AddALL price compare", "kind": "buy",
             "url": f"https://used.addall.com/SuperRare/submitRare?isbn={isbn}",
             "cost_hint": "compares 40+ sellers",
             "priority": 4},
            {"vendor": "Bookshop.org",    "kind": "buy",
             "url": f"https://bookshop.org/p/books/{isbn}",
             "cost_hint": "indie bookseller direct",
             "priority": 4},
        ])

    if doi:
        out.append({
            "vendor": "DOI resolver",
            "kind": "search",
            "url": f"https://doi.org/{doi}",
            "cost_hint": "publisher page; often paywalled",
            "priority": 3,
        })

    # ── Priority 5 — GENERIC SEARCH FALLBACK ────────────────────────
    if title:
        q = title
        if authors:
            q += " " + " ".join(authors[:2])
        # Quoted to bias toward the actual title
        q_quoted = f'"{title}"'
        out.append({
            "vendor": "Google search (PDF)",
            "kind": "search",
            "url": f"https://www.google.com/search?{urlencode({'q': f'{q_quoted} filetype:pdf'})}",
            "cost_hint": "search across web for free PDF",
            "priority": 5,
        })
        out.append({
            "vendor": "DuckDuckGo (PDF)",
            "kind": "search",
            "url": f"https://duckduckgo.com/?{urlencode({'q': f'{q_quoted} filetype:pdf'})}",
            "cost_hint": "alternative web search",
            "priority": 5,
        })
        out.append({
            "vendor": "Anna's Archive",
            "kind": "search",
            "url": f"https://annas-archive.org/search?{urlencode({'q': q})}",
            "cost_hint": "aggregated library mirrors (legal gray)",
            "priority": 5,
        })

    # Stable order: priority then vendor name
    out.sort(key=lambda x: (x["priority"], x["vendor"]))
    return out


def summary_string(links: list[dict]) -> str:
    """Compact human-readable summary for logs."""
    counts: dict[str, int] = {}
    for l in links:
        counts[l["kind"]] = counts.get(l["kind"], 0) + 1
    if not counts:
        return "(no acquisition links)"
    return ", ".join(f"{n} {k}" for k, n in counts.items())
