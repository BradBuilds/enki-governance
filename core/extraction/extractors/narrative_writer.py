"""
Narrative writer — populates summary.narrative and summary.headline.

Cheap pass strategy:
    - headline: use title_display if the garbage detector passed it;
      otherwise synthesize from first sentence of body (if available).
    - narrative: prefer feed_summary (stripped of HTML). If that's
      missing or thin, use first_paragraph / first 2 sentences of body.

Deep pass: stubbed. Phase 4 uses Gemma with a fixed prompt version to
produce a 2-3 sentence digest that is information-dense, present-tense,
and neutral in tone — never hallucinating facts not in the source.
"""

from __future__ import annotations

import re
from typing import Any, Optional

_HTML_TAG = re.compile(r"<[^>]+>")
_MULTI_WS = re.compile(r"\s+")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return _MULTI_WS.sub(" ", _HTML_TAG.sub(" ", s)).strip()


def _first_n_sentences(text: str, n: int = 2) -> str:
    text = text.strip()
    if not text:
        return ""
    parts = _SENT_SPLIT.split(text, maxsplit=n)
    return " ".join(parts[:n]).strip()


def write_narrative_cheap(
    *,
    title: Optional[str],
    feed_summary: Optional[str],
    body: Optional[str],
    title_is_garbage: bool,
) -> dict[str, str]:
    """Return {"headline": ..., "narrative": ..., "headline_source": ..., "narrative_source": ...}.

    Missing pieces come back as empty strings — the runner will stamp
    them into completeness.gaps. No field is ever None.
    """
    result = {
        "headline": "",
        "narrative": "",
        "headline_source": "feed",
        "narrative_source": "feed",
    }

    # Headline
    if title and not title_is_garbage:
        result["headline"] = title.strip()
        result["headline_source"] = "feed"
    elif body:
        # synthesize from first sentence of body (best effort for cheap pass)
        first_sentence = _first_n_sentences(body, 1)
        if first_sentence:
            # truncate to 280 char envelope limit
            result["headline"] = first_sentence[:280]
            result["headline_source"] = "body_synthesis"

    # Narrative
    clean_summary = _strip_html(feed_summary or "")
    if clean_summary and len(clean_summary) >= 40:
        result["narrative"] = clean_summary
        result["narrative_source"] = "feed"
    elif body:
        result["narrative"] = _first_n_sentences(body, 2)
        result["narrative_source"] = "body_first_sentences"

    return result


_NARRATIVE_PROMPT = """You are a precise news summarizer. Read the article body
and produce a JSON object with two fields:

  "headline": a single sentence (<= 120 chars), present tense, factual,
              no clickbait. If the article has no clear topic, use the
              first concrete fact.
  "narrative": a 2-3 sentence summary of the key facts. Faithful to
               the source — do not invent details, places, or attributions
               not present in the body.

Rules:
  - Output ONLY valid JSON: {"headline": "...", "narrative": "..."}
  - No preamble, no explanation, no markdown.
  - If the body is too short or empty, use the title as the headline
    and leave narrative empty.

Title: %TITLE%

Body:
%BODY%
"""


def write_narrative_deep(
    *,
    title: Optional[str],
    body: Optional[str],
    llm: Optional[Any] = None,
    prompt_version: str = "narrative_v1",
) -> dict[str, str]:
    """LLM-backed narrative synthesis.

    Calls the LLM with a strict-JSON prompt asking for {headline,
    narrative}. Returns empty strings on any failure (parse error,
    LLM unavailable, schema mismatch) so callers can degrade
    gracefully — the cheap pass output stays in place.
    """
    empty = {"headline": "", "narrative": "", "headline_source": "", "narrative_source": ""}
    if llm is None:
        return empty

    body_clean = (body or "").strip()
    if not body_clean:
        # Nothing to synthesize from
        return empty

    # Truncate body to keep prompt under context limit (rough heuristic:
    # ~3000 chars for a 5B model with 8K context)
    body_snippet = body_clean[:3000]
    title_clean = (title or "").strip()[:200]

    prompt = (_NARRATIVE_PROMPT
              .replace("%TITLE%", title_clean)
              .replace("%BODY%", body_snippet))

    result = llm.complete_json(prompt, max_tokens=400, prompt_version=prompt_version)
    if not result.success or not isinstance(result.parsed, dict):
        return empty

    headline = (result.parsed.get("headline") or "").strip()
    narrative = (result.parsed.get("narrative") or "").strip()
    return {
        "headline": headline,
        "narrative": narrative,
        "headline_source": "body_synthesis" if headline else "",
        "narrative_source": "body_synthesis" if narrative else "",
    }
