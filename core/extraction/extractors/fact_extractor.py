"""
Fact extractor — LLM-backed key fact extraction from article text.

Follows the same pattern as narrative_writer.py and entity_extractor.py:
strict JSON prompt, graceful degradation on failure, temperature=0
for deterministic output.

Facts are stored in envelope.summary.key_facts and surfaced in the
watch topic articles API and UI.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("enki.extraction.facts")


_FACTS_PROMPT = """Extract the key factual claims from this article. Return a JSON
object with one key "facts" — an array of fact objects:

  {"facts": [
    {"fact": "One clear sentence stating a verifiable fact", "kind": "event", "confidence": 0.9},
    ...
  ]}

Fact kinds:
  claim       — an assertion or statement by a person/org
  statistic   — a number, percentage, measurement, or quantity
  event       — something that happened (attack, meeting, launch, etc.)
  attribution — who said or did what (quotes, actions attributed to someone)
  date        — a temporal anchor (deadline, anniversary, scheduled event)

Rules:
  - Output ONLY valid JSON. No prose, no markdown, no preamble.
  - At most 8 facts. Prioritize the most concrete, verifiable claims.
  - Each fact must be a single, self-contained sentence.
  - confidence: your estimate of how well the article supports this (0.0-1.0).
  - Do NOT invent facts not present in the text.
  - Skip opinions, speculation, and vague statements.
  - If the text is too short or unclear, return {"facts": []}.

Title: %TITLE%

Body:
%BODY%
"""


def extract_facts_deep(
    text: str,
    *,
    title: Optional[str] = None,
    llm: Optional[Any] = None,
    prompt_version: str = "facts_v1",
) -> list[dict]:
    """LLM-backed key fact extraction.

    Returns envelope-shaped fact dicts:
        [{"fact": str, "kind": str, "confidence": float}]

    Returns empty list on any failure (LLM unavailable, JSON parse error,
    schema mismatch). The cheap pass stays in place.
    """
    if llm is None or not text:
        return []

    body_snippet = text.strip()[:3000]
    if len(body_snippet) < 50:
        return []

    title_clean = (title or "").strip()[:200]

    prompt = (_FACTS_PROMPT
              .replace("%TITLE%", title_clean)
              .replace("%BODY%", body_snippet))

    result = llm.complete_json(prompt, max_tokens=600, prompt_version=prompt_version)
    if not result.success or not isinstance(result.parsed, dict):
        return []

    raw_facts = result.parsed.get("facts") or []
    if not isinstance(raw_facts, list):
        return []

    out: list[dict] = []
    for f in raw_facts[:10]:
        if not isinstance(f, dict):
            continue
        fact_text = (f.get("fact") or "").strip()
        if len(fact_text) < 10:
            continue
        kind = (f.get("kind") or "claim").strip().lower()
        if kind not in ("claim", "statistic", "event", "attribution", "date"):
            kind = "claim"
        confidence = f.get("confidence", 0.7)
        if not isinstance(confidence, (int, float)):
            confidence = 0.7
        confidence = max(0.0, min(1.0, float(confidence)))
        out.append({
            "fact": fact_text,
            "kind": kind,
            "confidence": confidence,
        })

    return out
