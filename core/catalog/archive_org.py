"""archive.org enumeration source for the declassified-archive catalog.

The agencies' live reading-rooms are unenumerable (SPA / 404 by 2026), but
archive.org mirrors them with a real JSON API. This module harvests, relevance-
gates, and organizes those mirrors into SOURCE collections, and builds a
content-addressed MANIFEST for any library (collection or cross-agency topic)
so it can be hashed + sealed BEFORE mining ("reference-until-mined").

Deterministic given archive.org's responses; no auth. Designed so other sources
(NARA catalog API, govinfo) implement the same small surface later.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.parse
import urllib.request

_ADV = "https://archive.org/advancedsearch.php"
_SCRAPE = "https://archive.org/services/search/v1/scrape"
_UA = {"User-Agent": "EnkiResearch/0.1 (enki-governance; archive-catalog)"}

# Institutions/source-classes we index. This is SOURCE identification (who holds
# records), not topic knowledge — the topics themselves are discovered, not
# listed here. Title-matched (precise) rather than match-anywhere (noisy).
SOURCE_TERMS = [
    "CIA", "FBI", "NSA", "declassified", "FOIA",
    "National Security Archive", "State Department", "Department of State",
    "Defense Technical Information", "Office of Strategic Services",
    "Congressional Record", "Congressional Hearings", "Congressional Research",
    "NARA", "National Archives", "Church Committee", "COINTELPRO",
    "President's Daily Brief", "JFK Assassination",
    "Central Intelligence Agency", "National Security Agency",
    "Director of National Intelligence",
    # NB: bare "intelligence" is deliberately NOT here — it matches 18th-19thC
    # periodicals ("...Interesting Intelligence"), podcasts, and unrelated senses.
]

# Collections that slip through a keyword net but aren't declassified archives:
# general libraries, old periodicals (which used "intelligence" to mean "news"),
# podcasts/shows, and archive.org user "favorites" lists.
_EXCLUDE = (
    "biodiversity", "suffragist", "newspaper", "directory", "censorship",
    "hippo", "yearbook", "genealog", "comic", "music", "radio program",
    "television", "magazine archive", "school", "university of",
    "favorites", "favourites", "podcast", "report with", "squared",
    "artificial intelligence", "literary", "essays", "omnibus", "expositor",
    "gazette", "herald", "tribune", "periodical", "weekly", "monthly",
    "journal of", "professional bulletin", "emotional intelligence",
    "business intelligence", "market intelligence", "episode",
)

# Rough agency tagging for organization (source provenance, not topic).
_AGENCY_TAGS = {
    "cia": "CIA", "central intelligence": "CIA",
    "fbi": "FBI", "lazar": "FBI", "cointelpro": "FBI",
    "nsa": "NSA", "national security agency": "NSA",
    "director of national intelligence": "ODNI", "odni": "ODNI",
    "state": "State Dept", "congress": "Congress", "house": "Congress",
    "senate": "Congress", "nara": "NARA", "national archives": "NARA",
    "jfk": "JFK", "kennedy": "JFK", "oss": "OSS", "strategic services": "OSS",
    "dtic": "DoD/DTIC", "defense technical": "DoD/DTIC", "defense": "DoD/DTIC",
    "national security archive": "NSArchive", "nsia": "NSArchive",
    "foia": "FOIA",
}


def _get(url: str) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=45) as r:
        return json.load(r)


def humanize(slug: str) -> str:
    """archive.org identifier -> readable title (lazarfoia -> 'Lazar Foia')."""
    s = re.sub(r"[_\-]+", " ", slug)
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)          # split camelCase
    s = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", s)          # split letters/digits
    return re.sub(r"\s+", " ", s).strip().title()


def _clean(text: str) -> str:
    """Decode HTML entities (&apos; &amp; &#39; …) and collapse whitespace."""
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def collection_about(identifier: str) -> dict:
    """A collection's human title + 'about' description, from archive.org
    metadata. Falls back to a humanized identifier when the source is bare."""
    try:
        md = _get(f"https://archive.org/metadata/{identifier}").get("metadata", {})
    except Exception:
        md = {}
    title = md.get("title") or humanize(identifier)
    if isinstance(title, list):
        title = title[0] if title else humanize(identifier)
    desc = md.get("description") or ""
    if isinstance(desc, list):
        desc = " ".join(desc)
    desc = re.sub(r"<[^>]+>", " ", desc)                 # strip HTML tags
    return {"title": _clean(str(title)), "about": _clean(desc)[:1200]}


def _agency_of(identifier: str, title: str) -> str:
    hay = f"{identifier} {title}".lower()
    for key, tag in _AGENCY_TAGS.items():
        if key in hay:
            return tag
    return "other"


def _excluded(identifier: str, title: str) -> bool:
    hay = f"{identifier} {title}".lower()
    return any(x in hay for x in _EXCLUDE)


def find_source_collections(min_items: int = 30) -> list[dict]:
    """Relevance-gated, agency-tagged gov/declassified source collections."""
    seen: dict[str, dict] = {}
    for term in SOURCE_TERMS:
        q = f'mediatype:collection AND title:("{term}")'
        url = (f"{_ADV}?q={urllib.parse.quote(q)}"
               "&fl[]=identifier&fl[]=title&fl[]=item_count"
               "&rows=60&sort[]=item_count+desc&output=json")
        try:
            docs = _get(url)["response"]["docs"]
        except Exception:
            continue
        for d in docs:
            ident, title = d.get("identifier"), d.get("title") or ""
            n = int(d.get("item_count") or 0)
            if not ident or ident in seen or n < min_items or _excluded(ident, title):
                continue
            seen[ident] = {
                "identifier": ident, "title": title, "item_count": n,
                "agency": _agency_of(ident, title), "matched": term,
            }
    return sorted(seen.values(), key=lambda x: -x["item_count"])


def collection_items(collection: str, cap: int = 5000) -> list[dict]:
    """All item identifiers (+ light metadata) in a collection, via the scrape
    API's cursor. Capped for huge collections; the manifest is over what we take."""
    out: list[dict] = []
    cursor = None
    base = (f"{_SCRAPE}?q={urllib.parse.quote(f'collection:{collection}')}"
            "&fields=identifier,title,date&count=1000")
    while len(out) < cap:
        url = base + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
        data = _get(url)
        out.extend(data.get("items", []))
        cursor = data.get("cursor")
        if not cursor or not data.get("items"):
            break
    return out[:cap]


def topic_manifest(topic_query: str, sources: list[str], cap: int = 5000) -> list[dict]:
    """Cross-agency manifest: items matching a topic across given source
    collections (this is a topic LIBRARY that spans agencies)."""
    out: list[dict] = []
    for coll in sources:
        q = f"collection:{coll} AND ({topic_query})"
        url = (f"{_SCRAPE}?q={urllib.parse.quote(q)}"
               "&fields=identifier,title,date&count=1000")
        try:
            data = _get(url)
        except Exception:
            continue
        for it in data.get("items", []):
            it["_source"] = coll
            out.append(it)
        if len(out) >= cap:
            break
    return out


def manifest_hash(items: list[dict]) -> dict:
    """Content-addressed identity of a library's COMPOSITION: sha256 over the
    sorted item identifiers. Tamper-evident against add/remove of documents,
    computable without downloading (the byte-level evidence_root is computed at
    mine time from content_sha256)."""
    ids = sorted({(it.get("identifier") or "").strip() for it in items if it.get("identifier")})
    h = hashlib.sha256("\n".join(ids).encode()).hexdigest()
    return {"manifest_sha256": h, "document_count": len(ids)}
