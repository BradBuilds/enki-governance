"""
Article worker — handles news articles and RSS/Atom entries.

Registers for:
    signal.content.news_article  — articles typed at connector time
    signal.content.rss_entry     — RSS/Atom entries (transport for articles)

Does NOT register for:
    signal.environmental.seismic / USGS earthquake payloads
    weather alerts / CAP alerts
    geospatial position reports
    generic documents / web pages

These are semantically different and get their own workers.

Signal payload shape expected:
    {
        "type": "signal.content.rss_entry" | "signal.content.news_article",
        "signal_rid": <int or None>,
        "payload": {
            "id": <str>,
            "title": <str>,
            "link": <str>,
            "summary": <str html-ish>,
            "published": <str rfc822 or iso>,
            "author": <str>,
            "image_url": <str>,
            "full_text": <str>,
            "first_paragraph": <str>,
            "og_description": <str>,
            "word_count": <int>,
            "geo": <dict or None>,
            ...
        },
        "source": {                # optional — feed metadata
            "slug": <str>,
            "url": <str>,
            "outlet": <str>,
            "language": <str>,
            "category": <str>,
        }
    }
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

from core.extraction.base import ExtractionWorker
from core.extraction.envelope import (
    add_gap,
    get_path,
    record_pass,
    set_path,
)
from core.extraction.extractors import (
    detect_language,
    extract_entities_cheap,
    extract_entities_deep,
    extract_places_cheap,
    extract_places_deep,
    extract_quotes,
    extract_topics_cheap,
    extract_topics_deep,
    is_garbage_title,
    translate_to_english,
    write_narrative_cheap,
    write_narrative_deep,
)
from core.extraction.registry import register_worker

log = logging.getLogger("enki.extraction.workers.article")

_HTML_TAG = re.compile(r"<[^>]+>")
_MULTI_WS = re.compile(r"\s+")


def _clean(s: Optional[str]) -> str:
    if not s:
        return ""
    return _MULTI_WS.sub(" ", _HTML_TAG.sub(" ", s)).strip()


def _parse_time(s: Optional[str]) -> Optional[str]:
    """Accept RFC-822 (feedparser default) or ISO 8601. Return ISO UTC."""
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def _slug(text: str) -> str:
    """Stable slug for node_id formation. Deterministic so re-runs produce
    the same node_id for the same input (idempotency requirement)."""
    if not text:
        text = "untitled"
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    return h


# Hostname → outlet display name. Hand-curated for the most common
# news/tech sources our connectors fetch. Returns None if no match.
_HOSTNAME_TO_OUTLET = {
    "bbc.com": "BBC News",
    "bbc.co.uk": "BBC News",
    "cnn.com": "CNN",
    "reuters.com": "Reuters",
    "apnews.com": "AP News",
    "npr.org": "NPR",
    "nytimes.com": "The New York Times",
    "washingtonpost.com": "Washington Post",
    "wsj.com": "Wall Street Journal",
    "theguardian.com": "The Guardian",
    "aljazeera.com": "Al Jazeera",
    "arstechnica.com": "Ars Technica",
    "techcrunch.com": "TechCrunch",
    "theverge.com": "The Verge",
    "wired.com": "Wired",
    "krebsonsecurity.com": "Krebs on Security",
    "cisa.gov": "CISA",
    "ycombinator.com": "Hacker News",
    "news.ycombinator.com": "Hacker News",
    "earthquake.usgs.gov": "USGS",
    "usgs.gov": "USGS",
    "weather.gov": "NWS",
    "nasa.gov": "NASA",
    "reddit.com": "Reddit",
    "rt.com": "RT",
    "tass.com": "TASS",
    "spiegel.de": "Der Spiegel",
    "lemonde.fr": "Le Monde",
    "dw.com": "Deutsche Welle",
    "kyivpost.com": "Kyiv Post",
    "defensenews.com": "Defense News",
    "theregister.com": "The Register",
}


def _outlet_from_url(url: str) -> Optional[str]:
    """Best-effort outlet name from a URL hostname. Strips common
    subdomain prefixes (www, news, m) before lookup."""
    if not url:
        return None
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        host = host.lower().lstrip(".")
        # Strip common prefixes
        for prefix in ("www.", "news.", "m.", "edition.", "feeds."):
            if host.startswith(prefix):
                host = host[len(prefix):]
                break
        # Direct match
        if host in _HOSTNAME_TO_OUTLET:
            return _HOSTNAME_TO_OUTLET[host]
        # Try parent domain match (handles "tech.yahoo.com" → "yahoo.com")
        parts = host.split(".")
        for i in range(len(parts) - 1):
            parent = ".".join(parts[i:])
            if parent in _HOSTNAME_TO_OUTLET:
                return _HOSTNAME_TO_OUTLET[parent]
        # Last resort: title-case the second-level domain
        if len(parts) >= 2:
            return parts[-2].replace("-", " ").title()
    except Exception:
        pass
    return None


def _text_bundle(payload: dict) -> str:
    """Concatenate the text fields the universal extractors will scan.

    Title + clean summary + full body. Newlines preserved between sections
    so regex-based quote extraction keeps sentence boundaries."""
    parts: list[str] = []
    t = payload.get("title") or ""
    if t:
        parts.append(t.strip())
    s = _clean(payload.get("summary") or payload.get("og_description") or "")
    if s:
        parts.append(s)
    body = payload.get("full_text") or ""
    if body:
        parts.append(body.strip())
    return "\n\n".join(parts)


@register_worker(
    "signal.content.news_article",
    "signal.content.rss_entry",
    # Legacy non-canonical type — pre-canonical signals from older
    # ingestion paths (e.g. hackernews recipe) still arrive with this
    # id. Aliasing here means the article worker handles them without
    # requiring a vocab pack bump or a per-row migration.
    "signal.content.news",
)
class ArticleWorker(ExtractionWorker):
    """Universal article worker — any text article regardless of transport."""

    def cheap_pass(self, signal: dict, envelope: dict) -> dict:
        import time
        t0 = time.monotonic()
        filled: list[str] = []

        payload = signal.get("payload") or {}
        source = signal.get("source") or {}

        # Stamp node_type/subtype for the completeness scorer.
        # The slot spec key is 'document.article' (see completenessvocab).
        envelope["node_type"] = "document"
        envelope["subtype"] = "article"

        # ─── identity ────────────────────────────────────────────────
        raw_title = payload.get("title")
        set_path(envelope, "identity.title_raw", raw_title)
        is_garbage, garbage_reason = is_garbage_title(raw_title)
        if is_garbage:
            add_gap(envelope, "identity.title_display")
            # Leave title_display empty — the gate will fire deep pass.
        else:
            set_path(envelope, "identity.title_display", raw_title.strip())
            set_path(envelope, "identity.title_source", "feed")
            filled += ["identity.title_display", "identity.title_source"]

        byline = (payload.get("author") or "").strip() or None
        set_path(envelope, "identity.byline_raw", byline)
        if byline:
            filled.append("identity.byline_raw")

        # ─── source block ────────────────────────────────────────────
        link = payload.get("link") or ""
        set_path(envelope, "source.url", link)

        # Outlet name resolution — try in this order:
        #   1. source.outlet (passed by backfill script from signal metadata)
        #   2. payload.feed_title (set by RSS connector — best signal)
        #   3. derive from URL hostname (e.g. "BBC News" from bbc.com)
        #   4. fall back to "unknown"
        outlet = (
            source.get("outlet")
            or payload.get("feed_title")
            or _outlet_from_url(link)
            or ""
        )
        set_path(envelope, "source.outlet_name", outlet)
        set_path(envelope, "source.language_declared", source.get("language"))
        fetched_at = signal.get("fetched_at") or payload.get("fetched_at")
        if fetched_at:
            set_path(envelope, "source.fetched_at", fetched_at)
            filled.append("source.fetched_at")
        # source.node_id gets formed from outlet slug for determinism
        slug = source.get("slug") or (outlet.lower().replace(" ", "_") if outlet else "unknown")
        set_path(envelope, "source.node_id", f"source:{slug}")
        filled += ["source.url", "source.outlet_name", "source.node_id"]

        # ─── temporal ────────────────────────────────────────────────
        ev = _parse_time(payload.get("published"))
        if ev:
            set_path(envelope, "temporal.event_time", ev)
            set_path(envelope, "temporal.precision", "second")
            filled += ["temporal.event_time", "temporal.precision"]
        else:
            add_gap(envelope, "temporal.event_time")

        # ingested_at kept from build_empty (now-stamp); no-op here.

        # ─── media ───────────────────────────────────────────────────
        img = payload.get("image_url")
        if img:
            set_path(envelope, "media.thumbnail", img)
            set_path(envelope, "media.thumbnail_source", "feed")
            filled += ["media.thumbnail", "media.thumbnail_source"]

        # ─── summary / narrative ─────────────────────────────────────
        set_path(envelope, "summary.feed_summary", payload.get("summary"))
        set_path(envelope, "summary.feed_summary_clean", _clean(payload.get("summary")))
        body = payload.get("full_text") or ""
        if body:
            set_path(envelope, "summary.body_original", body)
            filled.append("summary.body_original")

        narr = write_narrative_cheap(
            title=raw_title,
            feed_summary=payload.get("summary"),
            body=body,
            title_is_garbage=is_garbage,
        )
        if narr["headline"]:
            set_path(envelope, "summary.headline", narr["headline"])
            filled.append("summary.headline")
            # If title was garbage and we synthesized a headline from body,
            # also mirror it into identity.title_display so the card has
            # *something* while still marking the gap.
            if is_garbage:
                set_path(envelope, "identity.title_display", narr["headline"])
                set_path(envelope, "identity.title_source", narr["headline_source"])
                filled.append("identity.title_display")
        else:
            add_gap(envelope, "summary.headline")

        if narr["narrative"]:
            set_path(envelope, "summary.narrative", narr["narrative"])
            filled.append("summary.narrative")
        else:
            add_gap(envelope, "summary.narrative")

        # ─── language detection (Phase 11) ───────────────────────────
        # Heuristic detector — no LLM in cheap pass. Translation happens
        # in deep_pass when language is non-English AND body exists.
        body_for_lang = body or _clean(payload.get("summary") or "")
        declared_lang = source.get("language")
        detected_lang = detect_language(body_for_lang) or declared_lang or "en"
        set_path(envelope, "source.language_declared", declared_lang)
        set_path(envelope, "source.language_detected", detected_lang)
        set_path(envelope, "source.language_displayed", detected_lang)
        if detected_lang and detected_lang != "en":
            # Mark for deep_pass translation
            add_gap(envelope, "summary.body_translated")
            filled.append("source.language_detected")

        # ─── universal extractors on the text bundle ─────────────────
        text = _text_bundle(payload)

        # Entities (identifier regexes only at cheap pass)
        ents = extract_entities_cheap(text)
        if ents:
            envelope["entities"].extend(ents)
            filled.append("entities")

        # Places (gazetteer)
        places = extract_places_cheap(text)
        if places:
            place_text = [p["name_canonical"] for p in places]
            set_path(envelope, "spatial.place_text", place_text)
            set_path(envelope, "spatial.extraction_method", "gazetteer")
            # Take highest-priority hit as representative coordinates
            top = places[0]
            set_path(envelope, "spatial.coordinates", {"lat": top["lat"], "lon": top["lon"]})
            filled += ["spatial.place_text", "spatial.coordinates", "spatial.extraction_method"]
        # Feed-provided geo (takes precedence if present)
        feed_geo = payload.get("geo")
        if feed_geo and isinstance(feed_geo, dict) and "lat" in feed_geo:
            set_path(envelope, "spatial.coordinates", {
                "lat": feed_geo.get("lat"),
                "lon": feed_geo.get("lon"),
            })
            set_path(envelope, "spatial.extraction_method", "feed")
            filled.append("spatial.coordinates")

        # Topics (keyword bag)
        topics = extract_topics_cheap(text)
        if topics:
            envelope["topics"].extend([
                {"node_id": None, "label": t["label"], "weight": t["weight"]}
                for t in topics
            ])
            filled.append("topics")

        # Quotes
        quotes = extract_quotes(text)
        if quotes:
            set_path(envelope, "summary.quotes", quotes)
            filled.append("summary.quotes")

        # Domain classification from source category (rough bucket)
        cat = (source.get("category") or "").lower()
        if "news" in cat or "geopolitics" in cat:
            set_path(envelope, "summary.domain_classification", "politics")
        elif "tech" in cat:
            set_path(envelope, "summary.domain_classification", "tech")
        elif "defense" in cat:
            set_path(envelope, "summary.domain_classification", "defense")
        elif "weather" in cat:
            set_path(envelope, "summary.domain_classification", "weather")
        elif "seismic" in cat or "geo" in cat:
            set_path(envelope, "summary.domain_classification", "science")

        # ─── node_id (stable, deterministic) ─────────────────────────
        nid_seed = f"{slug}|{link or _slug(raw_title or '')}"
        set_path(envelope, "node_id", f"source:{_slug(nid_seed)}")
        filled.append("node_id")

        # Record the pass
        dur_ms = int((time.monotonic() - t0) * 1000)
        record_pass(
            envelope,
            stage="cheap",
            fields_filled=filled,
            model=None,
            duration_ms=dur_ms,
            success=True,
        )
        return envelope

    def deep_pass(
        self,
        signal: dict,
        envelope: dict,
        llm: Optional[Any] = None,
    ) -> dict:
        """LLM-backed pass. Phase 2: stub.

        When llm=None we still *log* the pass so observability is
        honest about why deep extraction didn't run. Phase 4 replaces
        the body of this method with a real Gemma call.
        """
        import time
        t0 = time.monotonic()

        if llm is None:
            record_pass(
                envelope,
                stage="deep",
                fields_filled=[],
                model=None,
                duration_ms=int((time.monotonic() - t0) * 1000),
                success=False,
                error="llm_unavailable_phase2_stub",
            )
            return envelope

        payload = signal.get("payload") or {}
        body = payload.get("full_text") or ""
        title = payload.get("title")

        filled: list[str] = []

        # ── Translation (Phase 11) ─────────────────────────────────
        # Only translate when the cheap pass detected a non-English
        # language AND body is non-trivial. The original body stays
        # in summary.body_original; the translation lands in
        # summary.body_translated and becomes the source the LLM
        # narrative synthesis reads.
        detected_lang = (envelope.get("source") or {}).get("language_detected") or "en"
        synth_body = body
        if detected_lang != "en" and body and len(body.strip()) > 100:
            translated = translate_to_english(body, detected_lang, llm=llm)
            if translated:
                set_path(envelope, "summary.body_translated", translated)
                set_path(envelope, "source.language_displayed", "en")
                filled.extend(["summary.body_translated", "source.language_displayed"])
                # Synthesize narrative from the translation, not the original
                synth_body = translated

        # LLM narrative synthesis (rewrites headline if garbage, writes narrative)
        narr = write_narrative_deep(title=title, body=synth_body, llm=llm)
        if narr.get("headline"):
            set_path(envelope, "summary.headline", narr["headline"])
            set_path(envelope, "identity.title_display", narr["headline"])
            set_path(envelope, "identity.title_source", narr["headline_source"] or "body_synthesis")
            filled += ["summary.headline", "identity.title_display"]
        if narr.get("narrative"):
            set_path(envelope, "summary.narrative", narr["narrative"])
            filled.append("summary.narrative")

        # LLM NER
        ents = extract_entities_deep(body, llm=llm)
        if ents:
            envelope["entities"].extend(ents)
            filled.append("entities")

        # LLM place recognition
        places = extract_places_deep(body, llm=llm)
        if places:
            ptext = list(get_path(envelope, "spatial.place_text", []))
            for p in places:
                name = p.get("name_canonical") or p.get("name_raw")
                if name and name not in ptext:
                    ptext.append(name)
            set_path(envelope, "spatial.place_text", ptext)
            filled.append("spatial.place_text")

        # LLM topic refinement
        topics = extract_topics_deep(body, llm=llm)
        if topics:
            envelope["topics"].extend(topics)
            filled.append("topics")

        # LLM fact extraction
        from core.extraction.extractors.fact_extractor import extract_facts_deep
        facts = extract_facts_deep(synth_body, title=title, llm=llm)
        if facts:
            set_path(envelope, "summary.key_facts", facts)
            filled.append("summary.key_facts")

        record_pass(
            envelope,
            stage="deep",
            fields_filled=filled,
            model=getattr(llm, "model_name", "unknown"),
            model_version=getattr(llm, "model_version", None),
            prompt_version="phase4_composite_v1",
            duration_ms=int((time.monotonic() - t0) * 1000),
            success=True,
        )
        return envelope
