"""
Wikipedia REST provider — secondary source for narrative + thumbnail.

Wikidata gives us structured facts; Wikipedia gives us a one-paragraph
summary and an image. This provider runs AFTER Wikidata (priority 20)
and is primarily used to ENRICH an already-validated entity with
description + thumbnail, rather than to classify it — but if Wikidata
returns no hit and Wikipedia does, we still accept it with a lower
confidence because Wikipedia's structured data is weaker.

API: https://en.wikipedia.org/api/rest_v1/page/summary/{title}
Rate limit: ~500 req/sec per IP for unauthenticated; very generous.
"""
from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import quote

import requests

from core.entity_validation.provider import Provider
from core.entity_validation.types import Candidate, Match

log = logging.getLogger("enki.validation.wikipedia")

_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
_USER_AGENT = "Enki/1.0 entity-validator (contact: ops@example.com)"

# Keywords to infer type from Wikipedia's "description" or "extract" text
# when used as primary (no Wikidata hit). Reuses the same taxonomy as the
# Wikidata provider's description keywords so results are consistent.
_TYPE_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("strait", "strait"), ("canal", "canal"), ("bay", "bay"),
    ("gulf", "gulf"), ("peninsula", "peninsula"), ("archipelago", "archipelago"),
    ("mountain range", "mountain_range"), ("mountain", "mountain"),
    ("desert", "desert"), ("river", "river"), ("lake", "lake"),
    ("sea", "sea"), ("ocean", "ocean"), ("island", "island"),
    ("airport", "airport"), ("seaport", "seaport"),
    ("manufacturer", "organization"), ("company", "organization"),
    ("corporation", "organization"),
    ("international organization", "organization"),
    ("intergovernmental", "organization"),
    ("military alliance", "military_unit"),
    ("regiment", "military_unit"), ("division", "military_unit"),
    ("army", "military_unit"), ("navy", "military_unit"),
    ("air force", "military_unit"),
    ("helicopter", "helicopter"), ("rotorcraft", "helicopter"),
    ("aircraft", "aircraft"), ("airliner", "aircraft"), ("bomber", "aircraft"),
    ("warship", "vessel"), ("submarine", "vessel"), ("ship", "vessel"),
    ("missile", "weapon"), ("satellite", "spacecraft"),
    ("spacecraft", "spacecraft"),
    ("city in", "city"), ("capital of", "city"), ("capital city", "city"),
    ("country in", "country"), ("sovereign state", "country"),
    ("province", "region"), ("oblast", "region"),
    ("politician", "person"), ("journalist", "person"),
)
_TYPE_PATTERNS = tuple(
    (re.compile(r"\b" + re.escape(p) + r"\b", re.IGNORECASE), t)
    for p, t in _TYPE_KEYWORDS
)


class WikipediaProvider(Provider):
    name = "wikipedia"
    priority = 20                       # runs after Wikidata
    timeout_seconds = 8.0
    supports_kinds = ("entity", "place", "topic")

    def __init__(self, timeout_seconds: float = 8.0):
        self.timeout_seconds = timeout_seconds
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
        })

    def validate(self, candidate: Candidate) -> Optional[Match]:
        title = candidate.raw_text.strip().replace(" ", "_")
        if not title:
            return None

        url = _SUMMARY_URL.format(title=quote(title, safe="_"))
        try:
            resp = self._session.get(url, timeout=self.timeout_seconds)
        except Exception as e:
            log.debug("wikipedia fetch %s failed: %s", title, e)
            return None

        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            log.debug("wikipedia %s returned %d", title, resp.status_code)
            return None

        try:
            data = resp.json()
        except Exception:
            return None

        # Skip disambiguation and "no description" pages
        page_type = data.get("type")
        if page_type in ("disambiguation", "no-extract"):
            return None

        display_title = data.get("title") or candidate.raw_text
        description = data.get("description") or ""
        extract = data.get("extract") or ""
        thumb = (data.get("thumbnail") or {}).get("source")
        page_url = (data.get("content_urls", {}).get("desktop", {}) or {}).get("page") \
                   or f"https://en.wikipedia.org/wiki/{quote(title, safe='_')}"

        mapped_type = self._infer_type(description, extract, candidate.kind)
        if mapped_type is None:
            return None

        # Kind guard (same asymmetric rule as Wikidata provider)
        if candidate.kind == "place" and mapped_type not in {
            "city", "country", "region", "river", "lake", "sea",
            "strait", "canal", "bay", "gulf", "peninsula",
            "island", "archipelago", "mountain", "mountain_range",
            "desert", "ocean", "airport", "seaport",
        }:
            return None

        return Match(
            source="wikipedia",
            source_id=data.get("titles", {}).get("canonical") or title,
            source_url=page_url,
            canonical_name=display_title,
            canonical_type=mapped_type,
            aliases=[],
            identifiers={"wikipedia_title": display_title},
            confidence=0.80,                # lower than Wikidata direct
            description=description or extract[:300],
            thumbnail_url=thumb,
            extra={
                "extract": extract[:500],
                "wikipedia_pageid": data.get("pageid"),
            },
        )

    def _infer_type(self, description: str, extract: str, kind: str) -> Optional[str]:
        # Try description first (short, specific); fall back to extract.
        for text in (description, extract):
            if not text:
                continue
            for pattern, mapped in _TYPE_PATTERNS:
                if pattern.search(text):
                    return mapped
        return None
