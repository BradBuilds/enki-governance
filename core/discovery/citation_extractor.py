"""
Citation extractor — parse references out of an already-ingested
document, persist each as a new bibliographic_reference, and link
back to the source document via citation_emissions.

This closes the discovery cascade: a topic library acquires PDFs →
those PDFs cite more works → the new citations re-enter the
acquisition queue → recursive growth.

Strategy (regex-first, LLM optional):
  1. URLs in paragraph text          → reference with identifiers.url
  2. ISBNs in paragraph text         → reference with identifiers.isbn
  3. DOIs in paragraph text          → reference with identifiers.doi
  4. Bibliography-shaped paragraphs  → parse author/title/year heuristically
  5. Numbered footnotes              → parse same

No LLM call here; this should be fast and idempotent so it can re-run
after every new ingest.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Optional

sys.path.insert(0, "/app")

from core.discovery.purchase_links import build_links
from core.gdu.address import assign_address
from storage.postgres.db import get_connection

log = logging.getLogger("enki.discovery.citation_extractor")


# ─── Regex toolbox ────────────────────────────────────────────────────

# URLs (avoid trailing punctuation)
_URL_RE = re.compile(
    r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+(?<![.,;:!?)\]])",
)

# ISBN-10 / ISBN-13 (allow hyphens or spaces)
_ISBN_RE = re.compile(
    r"\bISBN[:\s]*((?:97[89][\s\-]?)?(?:\d[\s\-]?){9,12}\d|(?:\d[\s\-]?){9}[\dXx])\b",
    re.IGNORECASE,
)

# DOI
_DOI_RE = re.compile(
    r"\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)\b",
)

# Bibliography-shaped line: Author, A. (Year). Title.
_BIB_LINE_RE = re.compile(
    r"^"
    r"([A-Z][A-Za-z'’\-]+(?:\s+[A-Z][A-Za-z'’\-]+)?(?:,\s*[A-Z]\.?(?:\s*[A-Z]\.?)*)?)"  # Last, F.
    r"[,\s]\s*\(?(\d{4})\)?\.?\s+"     # year
    r"([^.]{8,200})\.?"                 # title (until first period)
)

# Numbered footnote: 1. Smith, John. Title. (Year)
_FOOTNOTE_RE = re.compile(
    r"^\s*\d+\.\s+"
    r"([A-Z][A-Za-z'’\-]+,\s*[A-Z][A-Za-z'’\-]+)?\.?\s*"   # author (optional)
    r"\"?([^.\"]{8,200})\"?\.?\s*"                          # title
    r"(?:\([^)]*\))?\s*"
    r"(?:\(?(\d{4})\)?)?"                                   # year (optional)
)


# ─── Reference normalization ──────────────────────────────────────────

def _clean_isbn(s: str) -> str:
    return re.sub(r"[\s\-]", "", s)


def _make_reference(
    *, library_id: int, citation_text: str,
    title: Optional[str], author: Optional[str], year: Optional[int],
    identifiers: dict, reference_type: str = "unknown",
) -> Optional[dict]:
    """Construct a reference dict the orchestrator persistence understands."""
    authors = [author] if author else []
    ref = {
        "citation_text":   citation_text[:300],
        "title":           title,
        "authors":         authors,
        "publication_year": year,
        "identifiers":     identifiers,
        "reference_type":  reference_type,
        "discovered_via":  "citation_extracted",
        "discovery_query": "(from ingested document)",
    }
    return ref


def _persist_citation(
    library_id: int,
    source_document_id: int,
    page_id: Optional[int],
    paragraph_id: Optional[int],
    citation_format: str,
    raw_citation_text: str,
    ref: dict,
) -> Optional[int]:
    """Persist a citation: bibliographic_references row + citation_emissions row.
    Returns reference_id (new or existing)."""
    identifiers = ref.get("identifiers") or {}
    dedup_key = (identifiers.get("doi") or identifiers.get("isbn")
                  or identifiers.get("url") or ref.get("title") or "")
    if not dedup_key:
        return None

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT reference_id FROM bibliographic_references
                 WHERE library_id = %s AND
                       COALESCE(identifiers->>'doi',
                                identifiers->>'isbn',
                                identifiers->>'url',
                                title) = %s
                """,
                (library_id, dedup_key),
            )
            existing = cur.fetchone()
            if existing:
                rid = existing[0]
            else:
                addr = assign_address(conn, "document", "bibliographic_reference")
                links = build_links(identifiers=identifiers,
                                    title=ref.get("title"),
                                    authors=ref.get("authors") or [],
                                    publication_year=ref.get("publication_year"),
                                    reference_type=ref.get("reference_type"))
                cur.execute(
                    """
                    INSERT INTO bibliographic_references
                        (base60_address, library_id, citation_text, title,
                         authors, publication_year, reference_type,
                         identifiers, discovered_via, discovery_query,
                         purchase_links)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb)
                    RETURNING reference_id
                    """,
                    (addr, library_id,
                     ref["citation_text"], ref.get("title"),
                     ref.get("authors") or [], ref.get("publication_year"),
                     ref.get("reference_type") or "unknown",
                     json.dumps(identifiers),
                     ref.get("discovered_via"), ref.get("discovery_query"),
                     json.dumps(links)),
                )
                rid = cur.fetchone()[0]

            # citation_emissions row
            cur.execute(
                """
                INSERT INTO citation_emissions
                    (source_document_id, reference_id, page_id, paragraph_id,
                     citation_text, citation_format)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_document_id, reference_id, citation_text) DO NOTHING
                """,
                (source_document_id, rid, page_id, paragraph_id,
                 raw_citation_text[:500], citation_format),
            )
        conn.commit()
    return rid


# ─── Per-paragraph extraction passes ──────────────────────────────────

def _extract_urls(text: str) -> list[dict]:
    out: list[dict] = []
    for m in _URL_RE.finditer(text):
        url = m.group(0)
        if len(url) < 12:
            continue
        out.append({
            "match_text": url,
            "format": "inline_url",
            "ref": {"identifiers": {"url": url}, "title": url,
                    "reference_type": "article"},
        })
    return out


def _extract_isbns(text: str) -> list[dict]:
    out: list[dict] = []
    for m in _ISBN_RE.finditer(text):
        isbn = _clean_isbn(m.group(1))
        if len(isbn) not in (10, 13):
            continue
        out.append({
            "match_text": m.group(0),
            "format": "isbn",
            "ref": {"identifiers": {"isbn": isbn},
                    "title": None, "reference_type": "book"},
        })
    return out


def _extract_dois(text: str) -> list[dict]:
    out: list[dict] = []
    for m in _DOI_RE.finditer(text):
        doi = m.group(1)
        out.append({
            "match_text": m.group(0),
            "format": "doi",
            "ref": {"identifiers": {"doi": doi},
                    "title": None, "reference_type": "paper"},
        })
    return out


def _extract_bibliography_lines(text: str) -> list[dict]:
    out: list[dict] = []
    # Look at line-by-line: bibliography paragraphs often have lines that
    # match Author, X. (YEAR). Title.
    for line in text.splitlines():
        line = line.strip()
        if not line or len(line) < 30 or len(line) > 400:
            continue
        m = _BIB_LINE_RE.match(line)
        if m:
            author, year_str, title = m.group(1), m.group(2), m.group(3).strip()
            try:
                year = int(year_str)
            except Exception:
                year = None
            out.append({
                "match_text": line,
                "format": "bibliography_line",
                "ref": {"identifiers": {},
                        "title": title[:200], "author": author,
                        "publication_year": year,
                        "reference_type": "unknown"},
            })
        else:
            m = _FOOTNOTE_RE.match(line)
            if m:
                author = (m.group(1) or "").strip(",. ") or None
                title  = (m.group(2) or "").strip()
                year   = int(m.group(3)) if m.group(3) else None
                if title and len(title) >= 8:
                    out.append({
                        "match_text": line,
                        "format": "footnote",
                        "ref": {"identifiers": {},
                                "title": title[:200], "author": author,
                                "publication_year": year,
                                "reference_type": "unknown"},
                    })
    return out


# ─── Entry points ─────────────────────────────────────────────────────

_SECTION_HEADER_RE = re.compile(
    r"^\s*(references|bibliography|further reading|works cited|notes|"
    r"endnotes|selected bibliography|sources|cited works|"
    r"primary sources|secondary sources)\s*:?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# A new section header that's NOT a citation section — signals we've
# left the bibliography region.
_NEW_SECTION_RE = re.compile(
    r"^\s*[A-Z][A-Z\s]{4,40}\s*$",
    re.MULTILINE,
)


def _is_in_bibliography_section(raw_text: str) -> bool:
    """Heuristic: does this paragraph live in (or read like) a citation
    section? Two signals: it matches the header pattern itself, or it
    looks bibliography-shaped (many capitalized words with year + period
    structure)."""
    if not raw_text:
        return False
    if _SECTION_HEADER_RE.search(raw_text):
        return True
    # Bibliography-shaped: contains a parenthetical year AND a period
    # AND multiple capitalized words (typical citation density).
    if "(" in raw_text and ")" in raw_text:
        years = re.findall(r"\b(19|20)\d{2}\b", raw_text)
        capwords = re.findall(r"\b[A-Z][a-z]+", raw_text)
        if len(years) >= 1 and len(capwords) >= 4 and "." in raw_text:
            return True
    return False


def extract_from_document(document_id: int, library_id: int) -> dict:
    """Extract every regex-detectable citation from a document. Persists
    new references + citation_emissions.

    Section-aware: URL/ISBN/DOI extraction runs on EVERY paragraph
    (those identifiers are unambiguous wherever they appear). But the
    bibliography-line + footnote patterns are restricted to paragraphs
    that look like they live in a citation section — otherwise numbered
    body paragraphs ("1. Smith reported that...") get falsely matched
    as footnotes.
    """
    stats = {"document_id": document_id, "paragraphs_scanned": 0,
             "matches": 0, "new_refs": 0, "biblio_paragraphs": 0,
             "by_format": {"inline_url": 0, "isbn": 0, "doi": 0,
                           "bibliography_line": 0, "footnote": 0}}

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT paragraph_id, page_id, raw_text FROM document_paragraphs
                 WHERE document_id = %s ORDER BY page_id NULLS LAST, order_index
                """,
                (document_id,),
            )
            paragraphs = cur.fetchall()

    seen_dedup_keys: set[str] = set()

    for paragraph_id, page_id, raw_text in paragraphs:
        if not raw_text or len(raw_text) < 20:
            continue
        stats["paragraphs_scanned"] += 1

        in_biblio = _is_in_bibliography_section(raw_text)
        if in_biblio:
            stats["biblio_paragraphs"] += 1

        # Always-on identifiers (unambiguous wherever they appear)
        always_on_finders = [
            (_extract_urls, "inline_url"),
            (_extract_isbns, "isbn"),
            (_extract_dois, "doi"),
        ]
        # Bibliography-restricted patterns
        biblio_only_finders = [
            (_extract_bibliography_lines, "bibliography_line"),
        ]
        finders = always_on_finders + (biblio_only_finders if in_biblio else [])

        for finder, fmt in finders:
            for m in finder(raw_text):
                ref = _make_reference(
                    library_id=library_id,
                    citation_text=m["match_text"],
                    title=m["ref"].get("title"),
                    author=m["ref"].get("author"),
                    year=m["ref"].get("publication_year"),
                    identifiers=m["ref"].get("identifiers") or {},
                    reference_type=m["ref"].get("reference_type") or "unknown",
                )
                if not ref:
                    continue
                identifiers = ref.get("identifiers") or {}
                dedup_key = (identifiers.get("doi") or identifiers.get("isbn")
                              or identifiers.get("url") or ref.get("title") or "")
                if not dedup_key or dedup_key in seen_dedup_keys:
                    continue
                rid_before = _ref_count_for_library(library_id)
                rid = _persist_citation(
                    library_id=library_id,
                    source_document_id=document_id,
                    page_id=page_id,
                    paragraph_id=paragraph_id,
                    citation_format=m["format"],
                    raw_citation_text=m["match_text"],
                    ref=ref,
                )
                if rid is None:
                    continue
                seen_dedup_keys.add(dedup_key)
                stats["matches"] += 1
                stats["by_format"][m["format"]] = stats["by_format"].get(m["format"], 0) + 1
                rid_after = _ref_count_for_library(library_id)
                if rid_after > rid_before:
                    stats["new_refs"] += 1
    return stats


def _ref_count_for_library(library_id: int) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM bibliographic_references WHERE library_id = %s",
                (library_id,),
            )
            return int(cur.fetchone()[0])


def extract_from_library(library_id: int) -> dict:
    """Run citation extraction over every document in a library."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT lm.member_id
                  FROM library_members lm
                 WHERE lm.library_id = %s AND lm.member_kind = 'document'
                """,
                (library_id,),
            )
            doc_ids = [r[0] for r in cur.fetchall()]
    totals = {"library_id": library_id, "documents": len(doc_ids),
              "matches": 0, "new_refs_total": 0, "per_doc": []}
    for doc_id in doc_ids:
        r = extract_from_document(doc_id, library_id)
        totals["matches"] += r["matches"]
        totals["new_refs_total"] += r["new_refs"]
        totals["per_doc"].append(r)
    return totals


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("library_id", type=int)
    args = p.parse_args()
    print(json.dumps(extract_from_library(args.library_id), indent=2, default=str))
